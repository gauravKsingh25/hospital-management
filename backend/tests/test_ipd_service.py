"""Inpatient service: from an admission to a discharged, billed, documented stay.

Four tests carry most of the weight, and each guards a failure a real ward has:

* `test_discharging_sends_the_bed_to_cleaning_not_straight_to_available` — the
  bed board lying, and a patient walked to an unmade bed.
* `test_a_death_on_the_ward_frees_the_bed_and_stops_the_chart` — the invariant
  that there is one way to record a death, not two.
* `test_a_dose_nobody_recorded_becomes_missed_rather_than_disappearing` — the
  whole reason a medication chart materialises doses in advance.
* `test_accruing_the_same_night_twice_produces_one_charge` — double billing a
  patient for one bed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core import database as db
from app.core.exceptions import ConflictError, IllegalStateTransitionError
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.billing import service as billing_service
from app.modules.billing.models import ChargeCategory, ChargeStatus
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter, EncounterStatus, EncounterType, NoteType
from app.modules.clinical.schemas import DeathRecord, DiagnosisCreate, LamaRecord, NoteCreate
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service
from app.modules.ipd.models import (
    Admission,
    AdmissionStatus,
    BedAssignment,
    BedClass,
    BedStatus,
    DischargeType,
    DoseStatus,
    MedicationAdministration,
    SummaryStatus,
)
from app.modules.ipd.schemas import (
    AdmissionCreate,
    BedCreate,
    DischargeRequest,
    DoseRecord,
    MedicationOrderCreate,
    WardCreate,
)
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


async def _user(session: AsyncSession, tenant: Hospital, role: str) -> User:
    from tests.conftest import _make_user

    user = await _make_user(session, role_code=role, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.DOCTOR)


@pytest.fixture
async def nurse_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.NURSE)


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Ramesh Yadav", phone="9812345670", gender=Gender.MALE, age_years=58
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def ward(session: AsyncSession, tenant: Hospital):  # type: ignore[no-untyped-def]
    record = await service.create_ward(
        session,
        WardCreate(code="MW1", name="Male Medical", bed_class=BedClass.GENERAL),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def beds(session: AsyncSession, tenant: Hospital, ward):  # type: ignore[no-untyped-def]
    made = []
    for code in ("MW1-01", "MW1-02"):
        made.append(
            await service.create_bed(
                session,
                BedCreate(ward_id=ward.id, code=code, tariff_item_code="ROOM-GEN"),
                hospital_id=tenant.id,
            )
        )
    await session.commit()
    return made


@pytest.fixture
async def encounter(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> Encounter:
    record = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    return record


@pytest.fixture
async def admission(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, beds, doctor_user: User
) -> Admission:
    record = await service.admit_patient(
        session,
        AdmissionCreate(
            encounter_id=encounter.id,
            bed_id=beds[0].id,
            provisional_diagnosis="Community-acquired pneumonia",
        ),
        hospital_id=tenant.id,
        actor=doctor_user,
    )
    await session.commit()
    return record


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------
async def test_admitting_takes_the_bed_and_converts_the_visit(
    session: AsyncSession, tenant: Hospital, admission: Admission, encounter: Encounter, beds
) -> None:
    """One action does all three things, or none of them."""
    await session.refresh(encounter)
    bed = await service.get_bed(session, beds[0].id, hospital_id=tenant.id)
    assignment = await service.current_assignment(session, admission.id)

    assert encounter.status is EncounterStatus.ADMITTED
    assert encounter.encounter_type is EncounterType.IPD
    assert bed.status is BedStatus.OCCUPIED
    assert assignment is not None and assignment.bed_id == bed.id
    assert admission.admission_number.startswith("IPD-")
    # The bed class is snapshotted, not looked up later.
    assert assignment.bed_class is BedClass.GENERAL


async def test_a_second_patient_cannot_be_put_in_an_occupied_bed(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, doctor_user: User
) -> None:
    other, _ = await patients_service.register_patient(
        session,
        PatientRegister(full_name="Vikram S", phone="9812345671", gender=Gender.MALE, age_years=40),
        hospital_id=tenant.id,
    )
    second = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=other.id, actor=doctor_user
    )
    await session.commit()

    with pytest.raises(ConflictError) as exc:
        await service.admit_patient(
            session,
            AdmissionCreate(encounter_id=second.id, bed_id=beds[0].id),
            hospital_id=tenant.id,
            actor=doctor_user,
        )
    assert exc.value.code == "bed_unavailable"


async def test_the_same_visit_cannot_be_admitted_twice(
    session: AsyncSession,
    tenant: Hospital,
    admission: Admission,
    encounter: Encounter,
    beds,
    doctor_user: User,
) -> None:
    """One stay, one bill. A second admission on one visit would produce two."""
    with pytest.raises(ConflictError) as exc:
        await service.admit_patient(
            session,
            AdmissionCreate(encounter_id=encounter.id, bed_id=beds[1].id),
            hospital_id=tenant.id,
            actor=doctor_user,
        )
    assert exc.value.code == "already_admitted"


async def test_a_single_sex_ward_is_enforced_not_merely_displayed(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    """Putting a man in a female ward is a complaint, sometimes an incident."""
    female_ward = await service.create_ward(
        session,
        WardCreate(code="FW1", name="Female Medical", gender_policy="FEMALE"),
        hospital_id=tenant.id,
    )
    bed = await service.create_bed(
        session, BedCreate(ward_id=female_ward.id, code="FW1-01"), hospital_id=tenant.id
    )
    await session.commit()

    with pytest.raises(ConflictError) as exc:
        await service.admit_patient(
            session,
            AdmissionCreate(encounter_id=encounter.id, bed_id=bed.id),
            hospital_id=tenant.id,
            actor=doctor_user,
        )
    assert exc.value.code == "ward_gender_policy"


async def test_an_unknown_gender_is_not_blocked_at_the_ward_door(
    session: AsyncSession, tenant: Hospital, doctor_user: User
) -> None:
    """An unconscious admission must not be refused over a data-entry field."""
    unknown, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Unknown Male", phone="9812345679", gender=Gender.UNKNOWN, age_years=50
        ),
        hospital_id=tenant.id,
    )
    visit = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=unknown.id, actor=doctor_user
    )
    female_ward = await service.create_ward(
        session,
        WardCreate(code="FW2", name="Female Ward 2", gender_policy="FEMALE"),
        hospital_id=tenant.id,
    )
    bed = await service.create_bed(
        session, BedCreate(ward_id=female_ward.id, code="FW2-01"), hospital_id=tenant.id
    )
    await session.commit()

    record = await service.admit_patient(
        session,
        AdmissionCreate(encounter_id=visit.id, bed_id=bed.id),
        hospital_id=tenant.id,
        actor=doctor_user,
    )
    assert record.status is AdmissionStatus.ADMITTED


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------
async def test_a_transfer_closes_one_occupancy_and_opens_the_next(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, nurse_user: User
) -> None:
    """And the vacated bed goes to cleaning, exactly as on discharge."""
    await service.transfer_patient(
        session,
        admission,
        to_bed=beds[1],
        reason="Stepped down from monitoring",
        actor=nurse_user,
    )
    await session.commit()

    old = await service.get_bed(session, beds[0].id, hospital_id=tenant.id)
    new = await service.get_bed(session, beds[1].id, hospital_id=tenant.id)
    assert old.status is BedStatus.CLEANING
    assert new.status is BedStatus.OCCUPIED

    rows = (
        (
            await session.execute(
                select(BedAssignment).where(col(BedAssignment.admission_id) == admission.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert sum(1 for row in rows if row.released_at is None) == 1


async def test_the_bed_history_survives_the_transfer(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, nurse_user: User
) -> None:
    """Infection control asks which bed the patient was in on day three."""
    await service.transfer_patient(session, admission, to_bed=beds[1], actor=nurse_user)
    await session.commit()

    history = (
        (
            await session.execute(
                select(BedAssignment)
                .where(col(BedAssignment.admission_id) == admission.id)
                .order_by(col(BedAssignment.assigned_at))
            )
        )
        .scalars()
        .all()
    )
    assert [row.bed_id for row in history] == [beds[0].id, beds[1].id]
    assert history[0].released_at is not None


# ---------------------------------------------------------------------------
# Discharge — the bed lifecycle
# ---------------------------------------------------------------------------
async def test_discharging_sends_the_bed_to_cleaning_not_straight_to_available(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, doctor_user: User
) -> None:
    """CLAUDE.md §7b, and the single most consequential rule in this module.

    A bed that looks free the instant a patient leaves is a bed the admissions
    desk sends the next patient to, and they arrive to find it unmade.
    """
    await service.discharge_patient(
        session,
        admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=doctor_user,
    )
    await session.commit()

    bed = await service.get_bed(session, beds[0].id, hospital_id=tenant.id)
    assert bed.status is BedStatus.CLEANING
    assert bed.released_at is not None

    # And only housekeeping's action makes it offerable again.
    await service.mark_bed_cleaned(session, bed)
    await session.commit()
    assert bed.status is BedStatus.AVAILABLE
    assert bed.released_at is None


async def test_discharging_closes_the_visit_through_the_state_machine(
    session: AsyncSession,
    tenant: Hospital,
    admission: Admission,
    encounter: Encounter,
    doctor_user: User,
) -> None:
    await service.discharge_patient(
        session,
        admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=doctor_user,
    )
    await session.commit()
    await session.refresh(encounter)

    assert admission.status is AdmissionStatus.DISCHARGED
    assert admission.discharge_type is DischargeType.RECOVERED
    assert encounter.status is EncounterStatus.COMPLETED


async def test_a_discharged_stay_cannot_be_discharged_again(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    await service.discharge_patient(
        session,
        admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=doctor_user,
    )
    await session.commit()

    with pytest.raises(ConflictError) as exc:
        await service.discharge_patient(
            session,
            admission,
            DischargeRequest(discharge_type=DischargeType.RECOVERED),
            actor=doctor_user,
        )
    assert exc.value.code == "admission_closed"


async def test_initiating_discharge_keeps_the_bed_and_compiles_the_summary(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, doctor_user: User
) -> None:
    """The gap between the doctor's signature and the patient leaving is real.

    The bill has to be settled and medicines collected — often hours. Freeing
    the bed at the signature is how it gets double-booked.
    """
    await service.initiate_discharge(session, admission, actor=doctor_user)
    await session.commit()

    bed = await service.get_bed(session, beds[0].id, hospital_id=tenant.id)
    assert admission.status is AdmissionStatus.DISCHARGE_INITIATED
    assert bed.status is BedStatus.OCCUPIED

    summary = await service.get_summary(session, admission.id)
    assert summary is not None and summary.status is SummaryStatus.DRAFT


async def test_a_cancelled_admission_frees_the_bed_and_owes_no_bed_day(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, doctor_user: User
) -> None:
    """Admitted in error is not the same as discharged: nothing happened."""
    await service.cancel_admission(
        session, admission, reason="Wrong patient selected", actor=doctor_user
    )
    await session.commit()

    bed = await service.get_bed(session, beds[0].id, hospital_id=tenant.id)
    assert admission.status is AdmissionStatus.CANCELLED
    assert bed.status is BedStatus.CLEANING
    assert admission.bed_days_charged == 0


# ---------------------------------------------------------------------------
# Death and LAMA — the one-way-to-record-it invariant
# ---------------------------------------------------------------------------
async def test_a_death_on_the_ward_frees_the_bed_and_stops_the_chart(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, doctor_user: User
) -> None:
    """CLAUDE.md §6 and §14, from the ward's side.

    The death is recorded against the *Encounter* — the only place that captures
    the certifying doctor and the cause. `ipd` follows via its subscriber. This
    test proves the following actually happens, which is what lets the module
    refuse to offer a second, thinner way to record a death.
    """
    await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Piperacillin-Tazobactam", dose="4.5 g", frequency="TDS", times_per_day=3
        ),
        actor=doctor_user,
    )
    await session.commit()

    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=tenant.id
    )
    await clinical_service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=utc_now(),
            death_certified_by_id=doctor_user.id,
            cause_of_death="Septic shock secondary to pneumonia",
        ),
        actor=doctor_user,
    )
    await session.commit()
    await session.refresh(admission)

    bed = await service.get_bed(session, beds[0].id, hospital_id=tenant.id)
    assert admission.status is AdmissionStatus.DISCHARGED
    assert admission.discharge_type is DischargeType.DECEASED
    assert bed.status is BedStatus.CLEANING

    orders, _ = await service.list_medication_orders(
        session,
        PageParams(limit=50),
        hospital_id=tenant.id,
        admission_id=admission.id,
        active_only=True,
    )
    assert orders == []


async def test_a_self_discharge_on_the_ward_closes_the_admission_too(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=tenant.id
    )
    await clinical_service.record_lama(
        session,
        encounter,
        LamaRecord(lama_reason="Family took the patient home", form_signed=True),
        actor=doctor_user,
    )
    await session.commit()
    await session.refresh(admission)

    assert admission.status is AdmissionStatus.DISCHARGED
    assert admission.discharge_type is DischargeType.LAMA


def test_the_discharge_endpoint_refuses_to_record_a_death() -> None:
    """A death is never entered as a discharge type — the schema refuses it.

    This is the guard that keeps "one way to record a death" true. Accepting it
    here would create a path with no certifying doctor and no cause.
    """
    with pytest.raises(ValueError, match="recorded against the visit"):
        DischargeRequest(discharge_type=DischargeType.DECEASED)

    with pytest.raises(ValueError, match="recorded against the visit"):
        DischargeRequest(discharge_type=DischargeType.LAMA)


async def test_a_visit_opened_before_the_death_still_cannot_be_admitted(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds, doctor_user: User
) -> None:
    """The gap `admit_patient`'s deceased guard actually closes.

    `open_encounter` already refuses a deceased patient, so the only way to
    reach an admission with one is a visit opened *before* the death — a second
    encounter sitting in REGISTERED when the patient dies on another. Without
    the guard that visit is still admittable, and an admission for a dead
    patient is what eventually sends their family a follow-up reminder.
    """
    stale = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=admission.patient_id, actor=doctor_user
    )
    await session.commit()

    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=tenant.id
    )
    await clinical_service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=utc_now(),
            death_certified_by_id=doctor_user.id,
            cause_of_death="Cardiac arrest",
        ),
        actor=doctor_user,
    )
    await session.commit()

    with pytest.raises(ConflictError) as exc:
        await service.admit_patient(
            session,
            AdmissionCreate(encounter_id=stale.id, bed_id=beds[1].id),
            hospital_id=tenant.id,
            actor=doctor_user,
        )
    assert exc.value.code == "patient_deceased"


# ---------------------------------------------------------------------------
# The medication chart
# ---------------------------------------------------------------------------
async def test_prescribing_materialises_the_doses_that_are_due(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """A chart of what *will* be due, not a log of what was given."""
    order = await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Amoxicillin",
            dose="500 mg",
            frequency="TDS",
            times_per_day=3,
            dose_times=["08:00", "14:00", "20:00"],
        ),
        actor=doctor_user,
    )
    await session.commit()

    doses, total = await service.list_doses(
        session, PageParams(limit=100), hospital_id=tenant.id, admission_id=admission.id
    )
    assert total > 0
    assert all(dose.status is DoseStatus.DUE for dose in doses)
    assert all(dose.medication_order_id == order.id for dose in doses)
    # Snapshotted from the order.
    assert {dose.drug_name for dose in doses} == {"Amoxicillin"}


async def test_materialising_twice_does_not_double_the_chart(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """What lets the daily sweep top up every order without checking first."""
    order = await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Pantoprazole",
            dose="40 mg",
            frequency="OD",
            times_per_day=1,
            dose_times=["08:00"],
        ),
        actor=doctor_user,
    )
    await session.commit()
    _, before = await service.list_doses(
        session, PageParams(limit=100), hospital_id=tenant.id, admission_id=admission.id
    )

    added = await service.materialise_doses(session, order)
    await session.commit()
    _, after = await service.list_doses(
        session, PageParams(limit=100), hospital_id=tenant.id, admission_id=admission.id
    )

    assert added == 0
    assert before == after


async def test_an_as_needed_drug_generates_no_scheduled_slots(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """A PRN that was never needed is not a missed dose."""
    await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Paracetamol",
            dose="1 g",
            frequency="SOS",
            is_prn=True,
            prn_indication="for temperature above 38",
        ),
        actor=doctor_user,
    )
    await session.commit()

    _, total = await service.list_doses(
        session, PageParams(limit=100), hospital_id=tenant.id, admission_id=admission.id
    )
    assert total == 0


async def test_signing_for_a_dose_records_who_gave_it(
    session: AsyncSession,
    tenant: Hospital,
    admission: Admission,
    doctor_user: User,
    nurse_user: User,
) -> None:
    await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Metformin",
            dose="500 mg",
            frequency="BD",
            times_per_day=2,
            dose_times=["08:00", "20:00"],
        ),
        actor=doctor_user,
    )
    await session.commit()
    doses, _ = await service.list_doses(
        session, PageParams(limit=10), hospital_id=tenant.id, admission_id=admission.id
    )

    recorded = await service.record_dose(
        session, doses[0], DoseRecord(status=DoseStatus.GIVEN), actor=nurse_user
    )
    await session.commit()

    assert recorded.status is DoseStatus.GIVEN
    assert recorded.administered_by_id == nurse_user.id
    # Snapshotted, so a chart printed years later still names the nurse.
    assert recorded.administered_by_name == nurse_user.full_name
    assert recorded.administered_at is not None


async def test_a_recorded_dose_cannot_be_quietly_rewritten(
    session: AsyncSession,
    tenant: Hospital,
    admission: Admission,
    doctor_user: User,
    nurse_user: User,
) -> None:
    """ "It says given but it was held" is a correction with a trail, not an edit."""
    await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Aspirin", dose="75 mg", frequency="OD", times_per_day=1, dose_times=["08:00"]
        ),
        actor=doctor_user,
    )
    await session.commit()
    doses, _ = await service.list_doses(
        session, PageParams(limit=10), hospital_id=tenant.id, admission_id=admission.id
    )
    await service.record_dose(
        session, doses[0], DoseRecord(status=DoseStatus.GIVEN), actor=nurse_user
    )
    await session.commit()

    with pytest.raises(ConflictError) as exc:
        await service.record_dose(
            session,
            doses[0],
            DoseRecord(status=DoseStatus.HELD, reason="Nil by mouth"),
            actor=nurse_user,
        )
    assert exc.value.code == "dose_already_recorded"


def test_recording_a_dose_as_not_given_requires_a_reason() -> None:
    """A blank reason on a missed antibiotic is the gap a review cannot close."""
    with pytest.raises(ValueError, match="needs a reason"):
        DoseRecord(status=DoseStatus.REFUSED)

    with pytest.raises(ValueError, match="saying what happened"):
        DoseRecord(status=DoseStatus.DUE)


async def test_a_dose_nobody_recorded_becomes_missed_rather_than_disappearing(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """The point of materialising doses in advance.

    A chart where nobody wrote anything looks identical to a chart where nothing
    was due, and only one of those is safe. `auto_missed` keeps the sweep's
    finding apart from a nurse's — the first is a process failure, the second is
    documented care.
    """
    order = await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Enoxaparin",
            dose="40 mg",
            frequency="OD",
            times_per_day=1,
            dose_times=["08:00"],
        ),
        actor=doctor_user,
    )
    # A slot that fell due yesterday and was never signed for.
    overdue = MedicationAdministration(
        hospital_id=tenant.id,
        medication_order_id=order.id,
        admission_id=admission.id,
        patient_id=admission.patient_id,
        drug_name=order.drug_name,
        dose=order.dose,
        route=order.route,
        due_at=utc_now() - timedelta(hours=8),
    )
    session.add(overdue)
    await session.commit()

    marked = await service.mark_missed_doses(session, hospital_id=tenant.id)
    await session.commit()
    await session.refresh(overdue)

    assert marked >= 1
    assert overdue.status is DoseStatus.MISSED
    assert overdue.auto_missed is True
    assert overdue.reason


async def test_the_grace_window_protects_a_drug_round_running_late(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """A busy round twenty minutes behind is not a missed dose."""
    order = await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Ramipril", dose="5 mg", frequency="OD", times_per_day=1, dose_times=["08:00"]
        ),
        actor=doctor_user,
    )
    recent = MedicationAdministration(
        hospital_id=tenant.id,
        medication_order_id=order.id,
        admission_id=admission.id,
        patient_id=admission.patient_id,
        drug_name=order.drug_name,
        dose=order.dose,
        route=order.route,
        due_at=utc_now() - timedelta(minutes=20),
    )
    session.add(recent)
    await session.commit()

    await service.mark_missed_doses(session, hospital_id=tenant.id, grace=timedelta(hours=2))
    await session.commit()
    await session.refresh(recent)

    assert recent.status is DoseStatus.DUE


async def test_stopping_a_drug_cancels_future_doses_but_not_the_record_of_past_ones(
    session: AsyncSession,
    tenant: Hospital,
    admission: Admission,
    doctor_user: User,
    nurse_user: User,
) -> None:
    """ "Scheduled then stopped" and "never prescribed" are different facts."""
    order = await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Ceftriaxone",
            dose="1 g",
            frequency="BD",
            times_per_day=2,
            dose_times=["08:00", "20:00"],
        ),
        actor=doctor_user,
    )
    await session.commit()

    given = (
        (
            await session.execute(
                select(MedicationAdministration)
                .where(col(MedicationAdministration.medication_order_id) == order.id)
                .order_by(col(MedicationAdministration.due_at))
            )
        )
        .scalars()
        .first()
    )
    assert given is not None
    await service.record_dose(session, given, DoseRecord(status=DoseStatus.GIVEN), actor=nurse_user)
    await session.commit()

    await service.stop_medication(session, order, reason="Switched to oral", actor=doctor_user)
    await session.commit()
    await session.refresh(given)

    assert order.is_active is False
    assert given.status is DoseStatus.GIVEN  # history is not rewritten

    future = (
        (
            await session.execute(
                select(MedicationAdministration).where(
                    col(MedicationAdministration.medication_order_id) == order.id,
                    col(MedicationAdministration.due_at) > utc_now(),
                )
            )
        )
        .scalars()
        .all()
    )
    # Every slot not yet given is cancelled, and none is left sitting DUE where
    # it would later be swept up as a missed dose the patient was never owed.
    assert future
    assert not [dose for dose in future if dose.status is DoseStatus.DUE]
    assert [dose for dose in future if dose.status is DoseStatus.CANCELLED]
    # The dose already signed for keeps its record, even though it falls later
    # today: stopping a drug does not rewrite what was given.
    assert given.status is DoseStatus.GIVEN


async def test_discharge_stops_the_chart_but_leaves_an_unrecorded_past_dose_outstanding(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """Going home at noon does not unmake a missed 6am antibiotic."""
    order = await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Azithromycin",
            dose="500 mg",
            frequency="OD",
            times_per_day=1,
            dose_times=["08:00"],
        ),
        actor=doctor_user,
    )
    stale = MedicationAdministration(
        hospital_id=tenant.id,
        medication_order_id=order.id,
        admission_id=admission.id,
        patient_id=admission.patient_id,
        drug_name=order.drug_name,
        dose=order.dose,
        route=order.route,
        due_at=utc_now() - timedelta(hours=6),
    )
    session.add(stale)
    await session.commit()

    await service.discharge_patient(
        session,
        admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=doctor_user,
    )
    await session.commit()
    await session.refresh(stale)

    assert stale.status is DoseStatus.DUE


# ---------------------------------------------------------------------------
# Bed-day accrual
# ---------------------------------------------------------------------------
async def test_a_night_in_a_bed_becomes_a_room_charge(
    session: AsyncSession, tenant: Hospital, admission: Admission
) -> None:
    """CLAUDE.md §7b: charges flow automatically. Nobody types the room rate."""
    result = await service.accrue_bed_days(session, hospital_id=tenant.id)
    await session.commit()

    assert result["bed_days_accrued"] >= 1

    charges, _ = await billing_service.list_charges(
        session, PageParams(limit=50), hospital_id=tenant.id, encounter_id=admission.encounter_id
    )
    room = [charge for charge in charges if charge.category is ChargeCategory.ROOM]
    assert room, "the bed-day did not reach the bill"
    assert room[0].source_module == "ipd"


async def test_accruing_the_same_night_twice_produces_one_charge(
    session: AsyncSession, tenant: Hospital, admission: Admission
) -> None:
    """A sweep that restarts, or runs twice after a deploy, must not double-bill.

    The deterministic accrual key is what guarantees it — the second pass keys
    onto the same charge rather than creating a second one.
    """
    await service.accrue_bed_days(session, hospital_id=tenant.id)
    await session.commit()

    # Force a re-emission of an already-billed night.
    admission.bed_days_charged = 0
    session.add(admission)
    await session.commit()

    await service.accrue_bed_days(session, hospital_id=tenant.id)
    await session.commit()

    charges, _ = await billing_service.list_charges(
        session, PageParams(limit=50), hospital_id=tenant.id, encounter_id=admission.encounter_id
    )
    room = [
        charge
        for charge in charges
        if charge.category is ChargeCategory.ROOM and charge.status is not ChargeStatus.CANCELLED
    ]
    assert len(room) == 1


def test_the_accrual_key_is_stable_for_the_same_admission_and_date() -> None:
    """It is a UUIDv5, so it must not drift between processes or releases."""
    import uuid
    from datetime import date

    admission_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    day = date(2026, 8, 12)
    assert service.accrual_key(admission_id, day) == service.accrual_key(admission_id, day)
    assert service.accrual_key(admission_id, day) != service.accrual_key(
        admission_id, date(2026, 8, 13)
    )


async def test_a_cancelled_admission_accrues_nothing(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    await service.cancel_admission(session, admission, reason="Duplicate", actor=doctor_user)
    await session.commit()

    result = await service.accrue_bed_days(session, hospital_id=tenant.id)
    await session.commit()
    assert result["bed_days_accrued"] == 0


# ---------------------------------------------------------------------------
# The discharge summary
# ---------------------------------------------------------------------------
async def test_the_summary_is_compiled_from_what_the_hospital_already_recorded(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """CLAUDE.md §7: the doctor's surface is review and sign, not write."""
    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=tenant.id
    )
    await clinical_service.record_diagnosis(
        session,
        encounter,
        DiagnosisCreate(description="Community-acquired pneumonia", code="J18.9", is_primary=True),
        actor=doctor_user,
    )
    await clinical_service.write_note(
        session,
        encounter,
        NoteCreate(note_type=NoteType.PROGRESS, content="Afebrile by day three, chest clearing."),
        actor=doctor_user,
    )
    await service.prescribe(
        session,
        admission,
        MedicationOrderCreate(
            drug_name="Amoxicillin",
            dose="500 mg",
            frequency="TDS",
            times_per_day=3,
            dose_times=["08:00", "14:00", "20:00"],
        ),
        actor=doctor_user,
    )
    await session.commit()

    summary = await service.compile_summary(session, admission, actor=doctor_user)
    await session.commit()

    assert "Community-acquired pneumonia" in (summary.diagnoses or "")
    assert "chest clearing" in (summary.course_in_hospital or "")
    assert "Amoxicillin" in (summary.discharge_medications or "")
    # Provenance is kept, so "blank because nobody wrote one?" stays answerable.
    assert summary.compiled["_provenance"]["diagnoses"] == 1


async def test_a_summary_cannot_be_signed_without_a_diagnosis(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """A discharge summary with no diagnosis is not a discharge summary."""
    from app.core.exceptions import ValidationError

    summary = await service.compile_summary(session, admission, actor=doctor_user)
    await session.commit()

    with pytest.raises(ValidationError) as exc:
        await service.sign_summary(session, summary, actor=doctor_user)
    assert exc.value.code == "summary_incomplete"


async def test_signing_freezes_the_summary_and_snapshots_the_signatory(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    from app.modules.ipd.schemas import SummaryUpdate

    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=tenant.id
    )
    await clinical_service.record_diagnosis(
        session,
        encounter,
        DiagnosisCreate(description="Pneumonia", is_primary=True),
        actor=doctor_user,
    )
    await session.commit()

    summary = await service.compile_summary(session, admission, actor=doctor_user)
    signed = await service.sign_summary(
        session, summary, actor=doctor_user, registration_number="MCI-12345"
    )
    await session.commit()

    assert signed.status is SummaryStatus.FINAL
    assert signed.signed_by_name == doctor_user.full_name
    assert signed.signatory_registration_number == "MCI-12345"

    with pytest.raises(ConflictError) as exc:
        await service.update_summary(session, signed, SummaryUpdate(examination="edited"))
    assert exc.value.code == "summary_signed"


async def test_recompiling_never_clobbers_what_a_human_typed(
    session: AsyncSession, tenant: Hospital, admission: Admission, doctor_user: User
) -> None:
    """A second compile must not wipe an edit with an empty section."""
    from app.modules.ipd.schemas import SummaryUpdate

    summary = await service.compile_summary(session, admission, actor=doctor_user)
    await service.update_summary(
        session, summary, SummaryUpdate(warning_signs="Return if breathless or febrile.")
    )
    await session.commit()

    recompiled = await service.compile_summary(session, admission, actor=doctor_user)
    await session.commit()

    assert recompiled.warning_signs == "Return if breathless or febrile."


# ---------------------------------------------------------------------------
# The bed board
# ---------------------------------------------------------------------------
async def test_the_board_shows_who_is_in_each_bed(
    session: AsyncSession, tenant: Hospital, admission: Admission, patient: Patient, ward
) -> None:
    board = await service.build_board(session, hospital_id=tenant.id)
    assert len(board) == 1

    cells = {cell["code"]: cell for cell in board[0]["beds"]}
    assert cells["MW1-01"]["patient_name"] == patient.full_name
    assert cells["MW1-01"]["uhid"] == patient.uhid
    assert cells["MW1-02"].get("patient_id") is None
    assert board[0]["occupied"] == 1
    assert board[0]["available"] == 1


async def test_occupancy_excludes_out_of_service_beds_from_the_denominator(
    session: AsyncSession, tenant: Hospital, admission: Admission, beds
) -> None:
    """A ward closed for renovation has not made the hospital 100% full."""
    await service.take_bed_out_of_service(session, beds[1], reason="Broken bed rail")
    await session.commit()

    stats = await service.occupancy(session, hospital_id=tenant.id)
    assert stats["total_beds"] == 2
    assert stats["out_of_service"] == 1
    assert stats["occupied"] == 1
    assert stats["occupancy_rate"] == 1.0


async def test_a_bed_status_is_never_set_outside_the_transition_table(
    session: AsyncSession, tenant: Hospital, beds
) -> None:
    """The invariant, asserted through the public surface."""
    with pytest.raises(IllegalStateTransitionError):
        await service.mark_bed_cleaned(session, beds[0])  # AVAILABLE -> AVAILABLE
