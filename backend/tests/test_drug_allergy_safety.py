"""A recorded drug allergy stops the drug (SAFE-01).

The audit's finding: a patient carried a CRITICAL "Aspirin — anaphylaxis"
alert, a doctor ordered Aspirin 300 mg, and the order went through with no
warning — and the drug round never showed the allergy to the nurse either.

These tests pin the three points where the allergy now stands in the way:
prescribing on the ward chart, ordering from the OPD, and the nurse signing
for a dose — including the allergy recorded *after* the drug was charted,
which is the one a check at prescribing alone would miss.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as SchemaError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError
from app.core.pagination import PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter, OrderType
from app.modules.clinical.schemas import OrderCreate
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service
from app.modules.ipd.models import Admission, BedClass, DoseStatus, MedicationAdministration
from app.modules.ipd.schemas import (
    AdmissionCreate,
    BedCreate,
    DoseRecord,
    MedicationOrderCreate,
    WardCreate,
)
from app.modules.patients import service as patients_service
from app.modules.patients.models import AlertSeverity, AlertType, Gender, Patient
from app.modules.patients.schemas import AlertCreate, PatientRegister
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


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
async def doctor(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.DOCTOR)


@pytest.fixture
async def nurse(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.NURSE)


async def _register(session: AsyncSession, tenant: Hospital, name: str, phone: str) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(full_name=name, phone=phone, gender=Gender.MALE, age_years=58),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    return await _register(session, tenant, "Ramesh Yadav", "9812345670")


async def _allergy(
    session: AsyncSession,
    patient: Patient,
    label: str = "Aspirin — anaphylaxis",
    alert_type: AlertType = AlertType.DRUG_ALLERGY,
):  # type: ignore[no-untyped-def]
    alert = await patients_service.record_alert(
        session,
        patient,
        AlertCreate(alert_type=alert_type, label=label, severity=AlertSeverity.CRITICAL),
    )
    await session.commit()
    return alert


@pytest.fixture
async def encounter(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
) -> Encounter:
    record = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor
    )
    await session.commit()
    return record


@pytest.fixture
async def admission(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor: User
) -> Admission:
    ward = await service.create_ward(
        session,
        WardCreate(code="MW1", name="Male Medical", bed_class=BedClass.GENERAL),
        hospital_id=tenant.id,
    )
    bed = await service.create_bed(
        session,
        BedCreate(ward_id=ward.id, code="MW1-01", tariff_item_code="ROOM-GEN"),
        hospital_id=tenant.id,
    )
    record = await service.admit_patient(
        session,
        AdmissionCreate(encounter_id=encounter.id, bed_id=bed.id),
        hospital_id=tenant.id,
        actor=doctor,
    )
    await session.commit()
    return record


def _aspirin(**extra: object) -> MedicationOrderCreate:
    return MedicationOrderCreate(
        drug_name="Aspirin",
        dose="300 mg",
        frequency="OD",
        times_per_day=1,
        dose_times=["08:00"],
        **extra,  # type: ignore[arg-type]
    )


async def _first_dose(
    session: AsyncSession, tenant: Hospital, admission: Admission
) -> MedicationAdministration:
    doses, _ = await service.list_doses(
        session, PageParams(limit=10), hospital_id=tenant.id, admission_id=admission.id
    )
    return doses[0]


# ---------------------------------------------------------------------------
# Prescribing on the ward chart
# ---------------------------------------------------------------------------
async def test_a_drug_the_patient_is_allergic_to_is_refused(
    session: AsyncSession, patient: Patient, admission: Admission, doctor: User
) -> None:
    await _allergy(session, patient)

    with pytest.raises(ConflictError) as raised:
        await service.prescribe(session, admission, _aspirin(), actor=doctor)

    assert raised.value.code == "allergy_conflict"
    [allergy] = raised.value.details["allergies"]
    assert allergy["label"] == "Aspirin — anaphylaxis"
    assert allergy["severity"] == "CRITICAL"


async def test_a_drug_in_the_allergic_class_is_refused_too(
    session: AsyncSession, patient: Patient, admission: Admission, doctor: User
) -> None:
    await _allergy(session, patient, "Penicillin")

    with pytest.raises(ConflictError) as raised:
        await service.prescribe(
            session,
            admission,
            MedicationOrderCreate(
                drug_name="Amoxicillin", dose="500 mg", frequency="TDS", times_per_day=3
            ),
            actor=doctor,
        )
    assert raised.value.code == "allergy_conflict"


async def test_the_doctor_may_override_with_a_reason_and_it_is_kept(
    session: AsyncSession, patient: Patient, admission: Admission, doctor: User
) -> None:
    """Sometimes the benefit wins — a desensitisation protocol, a label that
    turns out to be an intolerance. The decision is the doctor's, on the record."""
    alert = await _allergy(session, patient)

    order = await service.prescribe(
        session,
        admission,
        _aspirin(allergy_override_reason="Intolerance only (dyspepsia); cardiology advised."),
        actor=doctor,
    )
    await session.commit()

    assert order.allergy_override_reason == "Intolerance only (dyspepsia); cardiology advised."
    assert order.allergy_acknowledged_ids == [str(alert.id)]


def test_an_override_reason_must_say_something() -> None:
    with pytest.raises(SchemaError):
        _aspirin(allergy_override_reason="ok")


async def test_an_unneeded_override_is_not_recorded(
    session: AsyncSession, admission: Admission, doctor: User
) -> None:
    """No allergy, no override — the chart must not claim one was made."""
    order = await service.prescribe(
        session,
        admission,
        _aspirin(allergy_override_reason="Just in case, reviewed."),
        actor=doctor,
    )
    assert order.allergy_override_reason is None
    assert order.allergy_acknowledged_ids == []


async def test_a_resolved_allergy_no_longer_stands_in_the_way(
    session: AsyncSession, patient: Patient, admission: Admission, doctor: User
) -> None:
    alert = await _allergy(session, patient)
    await patients_service.resolve_alert(session, alert, reason="Challenge test negative")
    await session.commit()

    order = await service.prescribe(session, admission, _aspirin(), actor=doctor)
    assert order.drug_name == "Aspirin"


async def test_only_a_drug_allergy_blocks_a_drug(
    session: AsyncSession, patient: Patient, admission: Admission, doctor: User
) -> None:
    await _allergy(session, patient, "Aspirin", alert_type=AlertType.FOOD_ALLERGY)

    order = await service.prescribe(session, admission, _aspirin(), actor=doctor)
    assert order.drug_name == "Aspirin"


async def test_an_allergy_moved_by_a_merge_still_counts(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    admission: Admission,
    doctor: User,
) -> None:
    """The stay points at the record that lost a merge; its alerts moved to
    the survivor. Reading only the stay's own patient id would find nothing."""
    survivor = await _register(session, tenant, "Ramesh Yadav", "9700000000")
    await patients_service.merge_patients(
        session, survivor=survivor, duplicate=patient, reason="Same man"
    )
    await session.commit()
    await _allergy(session, survivor)

    with pytest.raises(ConflictError) as raised:
        await service.prescribe(session, admission, _aspirin(), actor=doctor)
    assert raised.value.code == "allergy_conflict"


# ---------------------------------------------------------------------------
# Signing for the dose
# ---------------------------------------------------------------------------
async def test_an_allergy_recorded_after_charting_stops_the_dose(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    admission: Admission,
    doctor: User,
    nurse: User,
) -> None:
    """The case a check at prescribing alone misses: the drug was charted on
    Monday, the family mentions the reaction on Tuesday."""
    await service.prescribe(session, admission, _aspirin(), actor=doctor)
    await session.commit()
    await _allergy(session, patient)
    dose = await _first_dose(session, tenant, admission)

    with pytest.raises(ConflictError) as raised:
        await service.record_dose(session, dose, DoseRecord(), actor=nurse)
    assert raised.value.code == "allergy_conflict"
    assert dose.status is DoseStatus.DUE

    # Holding it, with a reason, is exactly what the nurse should do.
    held = await service.record_dose(
        session,
        dose,
        DoseRecord(status=DoseStatus.HELD, reason="Allergy reported — doctor informed"),
        actor=nurse,
    )
    assert held.status is DoseStatus.HELD


async def test_a_dose_the_doctor_overrode_for_can_be_given(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    admission: Admission,
    doctor: User,
    nurse: User,
) -> None:
    await _allergy(session, patient)
    await service.prescribe(
        session,
        admission,
        _aspirin(allergy_override_reason="Benefit outweighs risk, cardiology."),
        actor=doctor,
    )
    await session.commit()
    dose = await _first_dose(session, tenant, admission)

    given = await service.record_dose(session, dose, DoseRecord(), actor=nurse)
    assert given.status is DoseStatus.GIVEN


async def test_a_new_allergy_after_an_override_still_stops_the_dose(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    admission: Admission,
    doctor: User,
    nurse: User,
) -> None:
    """The override covered the allergies the doctor was shown — not one
    recorded since."""
    await _allergy(session, patient)
    await service.prescribe(
        session,
        admission,
        _aspirin(allergy_override_reason="Benefit outweighs risk, cardiology."),
        actor=doctor,
    )
    await session.commit()
    await _allergy(session, patient, "NSAIDs — bronchospasm")
    dose = await _first_dose(session, tenant, admission)

    with pytest.raises(ConflictError) as raised:
        await service.record_dose(session, dose, DoseRecord(), actor=nurse)
    assert raised.value.code == "allergy_conflict"


# ---------------------------------------------------------------------------
# Ordering a drug from the OPD
# ---------------------------------------------------------------------------
async def test_an_opd_drug_order_is_checked_too(
    session: AsyncSession, patient: Patient, encounter: Encounter, doctor: User
) -> None:
    await _allergy(session, patient)

    with pytest.raises(ConflictError) as raised:
        await clinical_service.place_order(
            session,
            encounter,
            OrderCreate(order_type=OrderType.PHARMACY, item_name="Ecosprin 75"),
            actor=doctor,
        )
    assert raised.value.code == "allergy_conflict"

    order = await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(
            order_type=OrderType.PHARMACY,
            item_name="Ecosprin 75",
            allergy_override_reason="Post-stent; cardiology accepts the risk.",
        ),
        actor=doctor,
    )
    assert order.allergy_override_reason == "Post-stent; cardiology accepts the risk."


async def test_a_lab_test_is_not_a_drug(
    session: AsyncSession, patient: Patient, encounter: Encounter, doctor: User
) -> None:
    """A salicylate level is a blood test, not a dose of aspirin."""
    await _allergy(session, patient)

    order = await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.LAB, item_name="Aspirin (salicylate) level"),
        actor=doctor,
    )
    assert order.allergy_override_reason is None
