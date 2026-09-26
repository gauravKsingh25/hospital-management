"""Clinical service: the visit lifecycle, the closure gate, and the edge cases.

CLAUDE.md §15 asks for tests on every state transition and especially on death,
referral, LAMA and the timeout auto-close. Those four have a test each below,
plus the rules that are easy to break without noticing: that a death flags the
patient record, that clearing the last order closes the visit by itself, and
that the auto-close net does not fabricate a consultation that never happened.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.events import event_bus
from app.core.exceptions import (
    ConflictError,
    IllegalStateTransitionError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.clinical import service
from app.modules.clinical.events import EncounterClosed, PatientDeceased
from app.modules.clinical.models import (
    Encounter,
    EncounterStatus,
    NoteType,
    OrderType,
)
from app.modules.clinical.schemas import (
    DeathRecord,
    DiagnosisCreate,
    LamaRecord,
    NoteCreate,
    NoteTemplateCreate,
    OrderCreate,
    ReferralRecord,
    VitalsCreate,
)
from app.modules.clinical.state_machine import transition
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration

S = EncounterStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


@pytest.fixture
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    from tests.conftest import _make_user

    user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def nurse_user(session: AsyncSession, tenant: Hospital) -> User:
    from tests.conftest import _make_user

    user = await _make_user(session, role_code=Roles.NURSE, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Sunita Devi", phone="9876543210", gender=Gender.FEMALE, age_years=34
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def encounter(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> Encounter:
    record = await service.open_encounter(
        session,
        hospital_id=tenant.id,
        patient_id=patient.id,
        actor=doctor_user,
        chief_complaint="Fever for three days",
    )
    await session.commit()
    return record


async def _order(
    session: AsyncSession,
    encounter: Encounter,
    actor: User,
    *,
    order_type: OrderType = OrderType.LAB,
    review_in_visit: bool = False,
    blocks_closure: bool = True,
) -> uuid.UUID:
    order = await service.place_order(
        session,
        encounter,
        OrderCreate(
            order_type=order_type,
            item_name="Complete Blood Count",
            review_in_visit=review_in_visit,
            blocks_closure=blocks_closure,
        ),
        actor=actor,
    )
    await session.commit()
    return order.id


# ---------------------------------------------------------------------------
# Opening a visit
# ---------------------------------------------------------------------------
async def test_open_encounter_starts_registered_and_numbers_itself(
    encounter: Encounter,
) -> None:
    assert encounter.status is S.REGISTERED
    assert encounter.encounter_number.startswith("ENC-")
    assert encounter.closed_at is None


async def test_encounter_numbers_are_sequential_within_a_hospital(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    first = await service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    second = await service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    assert int(first.encounter_number.split("-")[-1]) + 1 == int(
        second.encounter_number.split("-")[-1]
    )


async def test_opening_writes_the_first_timeline_row(
    session: AsyncSession, encounter: Encounter
) -> None:
    """The timeline must start at the beginning, not mid-story."""
    events = await service.get_timeline(session, encounter.id)
    assert len(events) == 1
    assert events[0].from_status is None
    assert events[0].to_status is S.REGISTERED


async def test_a_second_check_in_reuses_the_same_chart(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """Two open encounters for one booking is how orders reach the wrong visit."""
    from app.modules.scheduling import service as scheduling_service
    from app.modules.scheduling.schemas import DoctorCreate

    doctor = await scheduling_service.create_doctor(
        session,
        DoctorCreate(user_id=doctor_user.id),
        hospital_id=tenant.id,
        display_name="Dr Rao",
    )
    appointment, _ = await scheduling_service.quick_opd(
        session,
        hospital_id=tenant.id,
        patient_id=patient.id,
        doctor_id=doctor.id,
        actor_id=doctor_user.id,
    )
    await session.commit()

    first = await service.open_encounter(
        session,
        hospital_id=tenant.id,
        patient_id=patient.id,
        actor=doctor_user,
        appointment_id=appointment.id,
    )
    await session.commit()
    second = await service.open_encounter(
        session,
        hospital_id=tenant.id,
        patient_id=patient.id,
        actor=doctor_user,
        appointment_id=appointment.id,
    )
    await session.commit()

    assert second.id == first.id
    assert second.encounter_number == first.encounter_number


async def test_a_deceased_patient_cannot_start_a_new_visit(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """Guards the §14 invariant at its most likely leak: a new visit reopening
    a dead patient's record and re-arming follow-up reminders."""
    await patients_service.mark_deceased(session, patient, occurred_at=utc_now())
    await session.commit()

    with pytest.raises(ConflictError) as error:
        await service.open_encounter(
            session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
        )
    assert error.value.code == "patient_deceased"


# ---------------------------------------------------------------------------
# The doctor's "complete visit" action — CLAUDE.md §6
# ---------------------------------------------------------------------------
async def test_complete_with_nothing_pending_closes_the_visit(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    await service.start_consultation(session, encounter, actor=doctor_user)
    updated = await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    assert updated.status is S.COMPLETED
    assert updated.closed_at is not None
    assert updated.closed_automatically is False


async def test_complete_with_a_counter_item_waits_for_clearance(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    await service.start_consultation(session, encounter, actor=doctor_user)
    await _order(session, encounter, doctor_user, order_type=OrderType.PHARMACY)

    updated = await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    assert updated.status is S.PENDING_CLEARANCE
    assert updated.closed_at is None
    # The doctor is finished even though the visit is not.
    assert updated.consultation_completed_at is not None


async def test_complete_with_a_review_item_waits_for_results(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """`review_in_visit` is what separates AWAITING_RESULTS from PENDING_CLEARANCE."""
    await service.start_consultation(session, encounter, actor=doctor_user)
    await _order(session, encounter, doctor_user, review_in_visit=True)

    updated = await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    assert updated.status is S.AWAITING_RESULTS


async def test_a_non_blocking_order_does_not_hold_the_visit_open(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """A test to be done before next month's visit must not block today's."""
    await service.start_consultation(session, encounter, actor=doctor_user)
    await _order(session, encounter, doctor_user, blocks_closure=False)

    updated = await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    assert updated.status is S.COMPLETED


async def test_completing_twice_with_the_same_items_open_is_a_no_op(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """ "You already told us" is a true statement and a useless error."""
    await service.start_consultation(session, encounter, actor=doctor_user)
    await _order(session, encounter, doctor_user, order_type=OrderType.PHARMACY)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    again = await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()
    assert again.status is S.PENDING_CLEARANCE


# ---------------------------------------------------------------------------
# Clearance closes the visit — CLAUDE.md §6
# ---------------------------------------------------------------------------
async def test_clearing_the_last_item_closes_the_visit_by_itself(
    session: AsyncSession, encounter: Encounter, doctor_user: User, tenant: Hospital
) -> None:
    """Nobody has to remember to go back and close it."""
    await service.start_consultation(session, encounter, actor=doctor_user)
    order_id = await _order(session, encounter, doctor_user, order_type=OrderType.PHARMACY)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    order = await service.get_order(session, order_id, hospital_id=tenant.id)
    _, updated = await service.complete_order(
        session, order, actor=doctor_user, hospital_id=tenant.id
    )
    await session.commit()

    assert updated.status is S.COMPLETED


async def test_clearing_one_of_two_items_leaves_the_visit_open(
    session: AsyncSession, encounter: Encounter, doctor_user: User, tenant: Hospital
) -> None:
    await service.start_consultation(session, encounter, actor=doctor_user)
    first = await _order(session, encounter, doctor_user, order_type=OrderType.PHARMACY)
    await _order(session, encounter, doctor_user, order_type=OrderType.LAB)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    order = await service.get_order(session, first, hospital_id=tenant.id)
    _, updated = await service.complete_order(
        session, order, actor=doctor_user, hospital_id=tenant.id
    )
    await session.commit()

    assert updated.status is S.PENDING_CLEARANCE


async def test_results_coming_back_return_the_patient_to_the_doctor(
    session: AsyncSession, encounter: Encounter, doctor_user: User, tenant: Hospital
) -> None:
    """AWAITING_RESULTS closing goes back to IN_CONSULTATION, not COMPLETED —
    the doctor asked to see them, so the visit is not over until they say."""
    await service.start_consultation(session, encounter, actor=doctor_user)
    order_id = await _order(session, encounter, doctor_user, review_in_visit=True)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    order = await service.get_order(session, order_id, hospital_id=tenant.id)
    _, updated = await service.complete_order(
        session, order, actor=doctor_user, hospital_id=tenant.id
    )
    await session.commit()

    assert updated.status is S.IN_CONSULTATION


async def test_cancelling_an_order_also_releases_the_visit(
    session: AsyncSession, encounter: Encounter, doctor_user: User, tenant: Hospital
) -> None:
    await service.start_consultation(session, encounter, actor=doctor_user)
    order_id = await _order(session, encounter, doctor_user, order_type=OrderType.PHARMACY)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    order = await service.get_order(session, order_id, hospital_id=tenant.id)
    _, updated = await service.cancel_order(
        session,
        order,
        reason="Patient could not afford it.",
        actor=doctor_user,
        hospital_id=tenant.id,
    )
    await session.commit()

    assert updated.status is S.COMPLETED


async def test_pending_items_explains_itself_in_words(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """Reception has to tell a waiting patient what is left, not read an id."""
    await _order(session, encounter, doctor_user, order_type=OrderType.LAB)
    pending = await service.pending_items(session, encounter)

    assert pending.total == 1
    assert pending.by_type == {"LAB": 1}
    assert "Complete Blood Count" in pending.descriptions[0]


# ---------------------------------------------------------------------------
# Edge case: death (CLAUDE.md §6, §14)
# ---------------------------------------------------------------------------
async def test_recording_a_death_flags_the_patient_record(
    session: AsyncSession, encounter: Encounter, doctor_user: User, patient: Patient
) -> None:
    """The §14 invariant: this is what stops a follow-up SMS reaching the family."""
    moment = utc_now() - timedelta(minutes=5)
    updated = await service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=moment,
            death_certified_by_id=doctor_user.id,
            cause_of_death="Cardiac arrest",
            death_place="Casualty",
        ),
        actor=doctor_user,
    )
    await session.commit()

    assert updated.status is S.DECEASED
    assert updated.cause_of_death == "Cardiac arrest"
    assert updated.death_certified_by_id == doctor_user.id

    refreshed = await patients_service.get_patient(session, patient.id)
    assert refreshed.is_deceased is True
    assert refreshed.is_active is False


async def test_a_death_without_a_cause_is_refused(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """A death with no cause is not a record; it is a rumour."""
    with pytest.raises(ValidationError) as error:
        await transition(
            session,
            encounter,
            S.DECEASED,
            actor=doctor_user,
            metadata={"deceased_at": utc_now(), "death_certified_by_id": doctor_user.id},
        )
    assert error.value.code == "transition_metadata_required"
    assert "cause_of_death" in error.value.details["missing"]


async def test_a_death_publishes_both_events_and_asks_for_settlement(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """A deceased patient usually still has a bill (CLAUDE.md §6)."""
    seen: list[object] = []
    event_bus.subscribe(PatientDeceased, seen.append)
    event_bus.subscribe(EncounterClosed, seen.append)
    try:
        await service.record_death(
            session,
            encounter,
            DeathRecord(
                deceased_at=utc_now(),
                death_certified_by_id=doctor_user.id,
                cause_of_death="Septic shock",
            ),
            actor=doctor_user,
        )
        await session.commit()
    finally:
        event_bus.clear()

    deceased = [item for item in seen if isinstance(item, PatientDeceased)]
    closed = [item for item in seen if isinstance(item, EncounterClosed)]
    assert len(deceased) == 1
    assert len(closed) == 1
    assert closed[0].requires_settlement is True
    assert closed[0].final_status == "DECEASED"


async def test_a_death_can_be_recorded_from_a_ward(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """Most in-hospital deaths happen after admission, not in a clinic room."""
    await service.admit(session, encounter, actor=doctor_user)
    await session.commit()

    updated = await service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=utc_now(),
            death_certified_by_id=doctor_user.id,
            cause_of_death="Multi-organ failure",
        ),
        actor=doctor_user,
    )
    await session.commit()
    assert updated.status is S.DECEASED


# ---------------------------------------------------------------------------
# Edge case: referral out
# ---------------------------------------------------------------------------
async def test_referral_records_destination_and_still_owes_a_bill(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    seen: list[EncounterClosed] = []
    event_bus.subscribe(EncounterClosed, seen.append)
    try:
        updated = await service.record_referral(
            session,
            encounter,
            ReferralRecord(
                referred_to_facility="AIIMS Delhi",
                referral_reason="Neurosurgery unavailable here.",
                referral_transport="Ambulance",
            ),
            actor=doctor_user,
        )
        await session.commit()
    finally:
        event_bus.clear()

    assert updated.status is S.REFERRED_OUT
    assert updated.referred_to_facility == "AIIMS Delhi"
    assert updated.referred_at is not None
    assert seen[0].requires_settlement is True


async def test_referral_without_a_destination_is_refused(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    with pytest.raises(ValidationError):
        await transition(
            session,
            encounter,
            S.REFERRED_OUT,
            actor=doctor_user,
            metadata={"referral_reason": "Needs a bigger hospital."},
        )


# ---------------------------------------------------------------------------
# Edge case: LAMA / absconded
# ---------------------------------------------------------------------------
async def test_lama_records_who_pressed_the_button(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    updated = await service.record_lama(
        session,
        encounter,
        LamaRecord(lama_reason="Family took the patient home.", lama_form_signed=True),
        actor=doctor_user,
    )
    await session.commit()

    assert updated.status is S.LAMA
    assert updated.lama_recorded_by_id == doctor_user.id
    assert updated.lama_form_signed is True


async def test_an_unsigned_lama_is_recorded_as_unsigned(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """An unsigned LAMA is the hospital's legal exposure — not a footnote."""
    updated = await service.record_lama(
        session, encounter, LamaRecord(lama_reason="Absconded from the ward."), actor=doctor_user
    )
    await session.commit()
    assert updated.lama_form_signed is False


async def test_a_closed_visit_refuses_a_second_ending(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    await service.record_lama(
        session, encounter, LamaRecord(lama_reason="Went home."), actor=doctor_user
    )
    await session.commit()

    with pytest.raises(IllegalStateTransitionError):
        await service.record_referral(
            session,
            encounter,
            ReferralRecord(referred_to_facility="Elsewhere", referral_reason="Too late."),
            actor=doctor_user,
        )


# ---------------------------------------------------------------------------
# Edge case: the auto-close safety net (CLAUDE.md §6)
# ---------------------------------------------------------------------------
async def test_auto_close_closes_a_forgotten_visit(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    await service.start_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    result = await service.close_stale_encounters(
        session, hospital_id=tenant.id, older_than=timedelta(0)
    )
    await session.commit()

    assert result["completed"] == 1
    assert encounter.status is S.COMPLETED
    # Reporting has to tell "the clinic closed this" from "a job tidied up".
    assert encounter.closed_automatically is True


async def test_auto_close_marks_a_never_seen_patient_no_show_not_completed(
    session: AsyncSession, tenant: Hospital, encounter: Encounter
) -> None:
    """Recording a patient as seen because a job ran at midnight would be a
    fabricated clinical fact, and would corrupt every footfall number built
    on top of it."""
    result = await service.close_stale_encounters(
        session, hospital_id=tenant.id, older_than=timedelta(0)
    )
    await session.commit()

    assert result["no_show"] == 1
    assert result["completed"] == 0
    assert encounter.status is S.NO_SHOW


async def test_auto_close_leaves_a_visit_with_pending_items_alone(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    """Those need a person. Closing them would hide the work the net exists to
    surface."""
    await service.start_consultation(session, encounter, actor=doctor_user)
    await _order(session, encounter, doctor_user)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    result = await service.close_stale_encounters(
        session, hospital_id=tenant.id, older_than=timedelta(0)
    )
    await session.commit()

    assert result["held_for_staff"] == 1
    assert result["completed"] == 0
    assert encounter.status is S.PENDING_CLEARANCE


async def test_auto_close_ignores_admitted_patients(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    """An inpatient on day five is not a stale OPD visit."""
    await service.admit(session, encounter, actor=doctor_user)
    await session.commit()

    result = await service.close_stale_encounters(
        session, hospital_id=tenant.id, older_than=timedelta(0)
    )
    await session.commit()

    assert result == {"completed": 0, "no_show": 0, "held_for_staff": 0}
    assert encounter.status is S.ADMITTED


async def test_auto_close_respects_the_buffer(
    session: AsyncSession, tenant: Hospital, encounter: Encounter
) -> None:
    result = await service.close_stale_encounters(
        session, hospital_id=tenant.id, older_than=timedelta(hours=12)
    )
    await session.commit()

    assert result["no_show"] == 0
    assert encounter.status is S.REGISTERED


async def test_auto_close_leaves_no_actor_rather_than_a_fake_one(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    """Inventing a "system" user makes the audit log lie about who acts here."""
    await service.start_consultation(session, encounter, actor=doctor_user)
    await session.commit()
    await service.close_stale_encounters(session, hospital_id=tenant.id, older_than=timedelta(0))
    await session.commit()

    closing = (await service.get_timeline(session, encounter.id))[-1]
    assert closing.to_status is S.COMPLETED
    assert closing.actor_id is None


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------
async def test_vitals_compute_bmi_and_flag_the_abnormal(
    session: AsyncSession, encounter: Encounter, nurse_user: User
) -> None:
    vitals = await service.record_vitals(
        session,
        encounter,
        VitalsCreate(
            temperature_c=Decimal("39.4"),
            pulse_bpm=88,
            systolic_bp=120,
            diastolic_bp=80,
            height_cm=Decimal("160.0"),
            weight_kg=Decimal("64.00"),
        ),
        actor=nurse_user,
    )
    await session.commit()

    assert vitals.bmi == Decimal("25.0")
    assert vitals.is_abnormal is True


async def test_normal_vitals_are_not_flagged(
    session: AsyncSession, encounter: Encounter, nurse_user: User
) -> None:
    """A flag firing on every second patient is a flag people learn to ignore."""
    vitals = await service.record_vitals(
        session,
        encounter,
        VitalsCreate(
            temperature_c=Decimal("36.8"),
            pulse_bpm=76,
            systolic_bp=118,
            diastolic_bp=76,
            spo2_percent=98,
        ),
        actor=nurse_user,
    )
    await session.commit()
    assert vitals.is_abnormal is False


async def test_partial_vitals_are_accepted(
    session: AsyncSession, encounter: Encounter, nurse_user: User
) -> None:
    """A nurse with a BP cuff and no thermometer records what they have."""
    vitals = await service.record_vitals(
        session, encounter, VitalsCreate(systolic_bp=118, diastolic_bp=78), actor=nurse_user
    )
    await session.commit()
    assert vitals.temperature_c is None
    assert vitals.bmi is None


async def test_vitals_are_refused_on_a_closed_visit(
    session: AsyncSession, encounter: Encounter, nurse_user: User, doctor_user: User
) -> None:
    await service.start_consultation(session, encounter, actor=doctor_user)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    with pytest.raises(ConflictError) as error:
        await service.record_vitals(
            session, encounter, VitalsCreate(pulse_bpm=80), actor=nurse_user
        )
    assert error.value.code == "encounter_closed"


# ---------------------------------------------------------------------------
# Vitals: the bounds themselves
#
# These are schema tests, not service tests — no session, no encounter. They
# are here because the bounds are the contract the frontend's Zod schema
# mirrors (`frontend/src/features/vitals/schema.ts`), and a silent widening on
# this side would let the form reject a reading the backend would have taken.
# ---------------------------------------------------------------------------
def test_diastolic_may_not_reach_systolic() -> None:
    """A transposed cuff reading — 70 over 120 — is the common data-entry slip."""
    with pytest.raises(PydanticValidationError) as error:
        VitalsCreate(systolic_bp=70, diastolic_bp=120)
    assert "Diastolic pressure must be below systolic." in str(error.value)


def test_equal_blood_pressures_are_refused() -> None:
    """`>=`, not `>`: a pulse pressure of zero is not a measurement."""
    with pytest.raises(PydanticValidationError):
        VitalsCreate(systolic_bp=90, diastolic_bp=90)


@pytest.mark.parametrize(
    "payload",
    [{"systolic_bp": 120}, {"diastolic_bp": 80}],
    ids=["systolic-only", "diastolic-only"],
)
def test_half_a_blood_pressure_is_refused(payload: dict[str, int]) -> None:
    with pytest.raises(PydanticValidationError) as error:
        VitalsCreate(**payload)
    assert "Record both blood pressure values, or neither." in str(error.value)


def test_glucose_is_a_whole_number() -> None:
    """mg/dL readings are integers. A decimal is almost always mmol/L mistyped."""
    with pytest.raises(PydanticValidationError):
        VitalsCreate(blood_glucose_mgdl=Decimal("14.5"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature_c", Decimal("24.9")),
        ("temperature_c", Decimal("45.1")),
        ("temperature_c", Decimal("38.45")),  # more than one decimal place
        ("pulse_bpm", 401),
        ("respiratory_rate", 151),
        ("spo2_percent", 101),
        ("weight_kg", Decimal("0")),  # gt=0, so zero is not a weight
        ("weight_kg", Decimal("700.01")),
        ("blood_glucose_mgdl", 2001),
        ("pain_score", 11),
    ],
)
def test_out_of_range_vitals_are_refused(field: str, value: object) -> None:
    with pytest.raises(PydanticValidationError):
        VitalsCreate(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature_c", Decimal("25")),  # ge=25 — the floor is inclusive
        ("temperature_c", Decimal("45")),
        ("spo2_percent", 100),
        ("pulse_bpm", 0),
        ("respiratory_rate", 50),  # high, but real: flagged, never refused
    ],
)
def test_boundary_vitals_are_accepted(field: str, value: object) -> None:
    """The limits are what a body can register, not what is healthy."""
    assert VitalsCreate(**{field: value}) is not None  # type: ignore[arg-type]


def test_empty_vitals_are_valid() -> None:
    """Every field optional: a nurse records only what they measured."""
    assert VitalsCreate().systolic_bp is None


# ---------------------------------------------------------------------------
# Notes, templates and diagnoses
# ---------------------------------------------------------------------------
async def test_a_note_is_signed_on_write_by_default(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    note = await service.write_note(
        session,
        encounter,
        NoteCreate(note_type=NoteType.EXAMINATION, content="Chest clear. No added sounds."),
        actor=doctor_user,
    )
    await session.commit()

    assert note.is_signed is True
    assert note.signed_at is not None
    # Snapshotted so the record still reads correctly after a rename.
    assert note.author_name == doctor_user.full_name


async def test_only_the_author_may_sign_a_note(
    session: AsyncSession, encounter: Encounter, doctor_user: User, nurse_user: User
) -> None:
    """An e-signature anyone can apply is a timestamp with someone else's name."""
    note = await service.write_note(
        session, encounter, NoteCreate(content="Draft.", sign=False), actor=doctor_user
    )
    await session.commit()

    with pytest.raises(PermissionDeniedError):
        await service.sign_note(session, note, actor=nurse_user)


async def test_a_signed_note_cannot_be_signed_twice(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    note = await service.write_note(
        session, encounter, NoteCreate(content="Seen."), actor=doctor_user
    )
    await session.commit()

    with pytest.raises(ConflictError):
        await service.sign_note(session, note, actor=doctor_user)


async def test_using_a_template_counts_the_use(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    """Sorts the picker by what clinicians actually reach for."""
    template = await service.create_template(
        session,
        NoteTemplateCreate(title="URI advice", body="Rest, fluids, paracetamol SOS."),
        hospital_id=tenant.id,
        actor=doctor_user,
    )
    await session.commit()

    await service.write_note(
        session,
        encounter,
        NoteCreate(content="Rest, fluids, paracetamol SOS.", template_id=template.id),
        actor=doctor_user,
    )
    await session.commit()

    refreshed = await service.get_template(session, template.id, hospital_id=tenant.id)
    assert refreshed.usage_count == 1


async def test_recording_a_new_primary_demotes_the_old_one(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """The correction has to be a single call, not delete-then-insert."""
    await service.record_diagnosis(
        session,
        encounter,
        DiagnosisCreate(description="Viral fever", is_primary=True),
        actor=doctor_user,
    )
    await service.record_diagnosis(
        session,
        encounter,
        DiagnosisCreate(description="Dengue", code="A90", is_primary=True),
        actor=doctor_user,
    )
    await session.commit()

    diagnoses = await service.list_diagnoses(session, encounter.id)
    primaries = [item for item in diagnoses if item.is_primary]
    assert len(primaries) == 1
    assert primaries[0].description == "Dengue"


async def test_a_diagnosis_may_be_recorded_without_a_code(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """Forcing a code up front only teaches doctors to pick a wrong one."""
    diagnosis = await service.record_diagnosis(
        session, encounter, DiagnosisCreate(description="Query enteric fever"), actor=doctor_user
    )
    await session.commit()
    assert diagnosis.code is None


# ---------------------------------------------------------------------------
# Fulfilment roles
# ---------------------------------------------------------------------------
def test_a_cashier_cannot_mark_a_blood_test_resulted() -> None:
    """`order:fulfil` says a user may clear orders; this says which kind."""
    from app.modules.clinical.models import Order

    order = Order(
        hospital_id=uuid.uuid4(),
        encounter_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        order_type=OrderType.LAB,
        item_name="CBC",
        ordered_by_id=uuid.uuid4(),
        ordered_at=datetime.now(UTC),
    )
    with pytest.raises(PermissionDeniedError) as error:
        service.assert_may_fulfil(order, frozenset({Roles.CASHIER}))
    assert error.value.code == "wrong_fulfilment_role"

    service.assert_may_fulfil(order, frozenset({Roles.LAB_TECH}))


def test_every_role_named_as_a_fulfiller_can_actually_fulfil() -> None:
    """The two halves of the gate must agree, or one of them is decoration.

    `order:fulfil` is checked by the route before `FULFILMENT_ROLES` is ever
    consulted, so a role named in that mapping without the permission simply
    cannot act — and the mapping says it can. That drift is invisible in a
    permission matrix and was invisible here too until a live run hit it: a
    doctor who performed a procedure could not mark it done, and the visit sat
    in PENDING_CLEARANCE until an administrator noticed.

    Asserted as a property rather than role by role, so a role added to
    `FULFILMENT_ROLES` in a later phase cannot reintroduce it.
    """
    from app.modules.clinical.rbac import (
        FULFILMENT_ROLES,
        MODULE_ROLE_PERMISSIONS,
        ClinicalPermissions,
    )

    holders = {
        role
        for role, permissions in MODULE_ROLE_PERMISSIONS.items()
        if ClinicalPermissions.ORDER_FULFIL in permissions
    }
    unreachable = {
        order_type.value: sorted(roles - holders)
        for order_type, roles in FULFILMENT_ROLES.items()
        if roles - holders
    }
    assert not unreachable, f"named as fulfillers but lacking `order:fulfil`: {unreachable}"


# ---------------------------------------------------------------------------
# Chart and timeline
# ---------------------------------------------------------------------------
async def test_the_chart_carries_the_safety_banner(
    session: AsyncSession, encounter: Encounter, patient: Patient, doctor_user: User
) -> None:
    """A banner that arrives one request after the screen gets missed at
    exactly the wrong moment (CLAUDE.md §7b)."""
    from app.modules.patients.models import AlertSeverity, AlertType
    from app.modules.patients.schemas import AlertCreate

    await patients_service.record_alert(
        session,
        patient,
        AlertCreate(
            alert_type=AlertType.DRUG_ALLERGY, severity=AlertSeverity.CRITICAL, label="Penicillin"
        ),
    )
    await session.commit()

    chart = await service.build_chart(session, encounter)
    banner = chart["banner"]

    assert banner.uhid == patient.uhid
    assert banner.has_critical_alert is True
    assert any("Penicillin" in alert for alert in banner.alerts)


async def test_the_timeline_records_every_step_in_order(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    await service.start_consultation(session, encounter, actor=doctor_user)
    await service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    steps = [event.to_status for event in await service.get_timeline(session, encounter.id)]
    assert steps == [S.REGISTERED, S.IN_CONSULTATION, S.COMPLETED]


async def test_the_patient_timeline_spans_visits(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    for _ in range(2):
        visit = await service.open_encounter(
            session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
        )
        await service.start_consultation(session, visit, actor=doctor_user)
        await service.complete_consultation(session, visit, actor=doctor_user)
        await session.commit()

    events, total = await service.get_patient_timeline(
        session, PageParams(limit=50), patient_id=patient.id, hospital_id=tenant.id
    )
    assert total == 6  # two visits x (opened, in consultation, completed)
    assert len(events) == 6


async def test_the_transition_metadata_survives_json_serialisation(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    """Datetimes and UUIDs arrive here routinely; `json.dumps` refuses both, and
    a transition that cannot serialise its own audit row would lose a death."""
    moment = utc_now()
    await service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=moment, death_certified_by_id=doctor_user.id, cause_of_death="Stroke"
        ),
        actor=doctor_user,
    )
    await session.commit()

    closing = (await service.get_timeline(session, encounter.id))[-1]
    assert closing.event_metadata is not None
    assert closing.event_metadata["cause_of_death"] == "Stroke"
    assert closing.event_metadata["death_certified_by_id"] == str(doctor_user.id)


# ---------------------------------------------------------------------------
# Tenant isolation and the scheduled sweep
# ---------------------------------------------------------------------------
async def test_another_hospitals_visit_is_invisible(
    session: AsyncSession, tenant: Hospital, encounter: Encounter
) -> None:
    """Row-level security, not a WHERE clause someone can forget.

    The test harness connects as a role with no BYPASSRLS, so this is the
    policy answering — see `tests/conftest.py`.
    """
    from app.core.exceptions import NotFoundError

    other = uuid.uuid4()
    await db.set_tenant_context(session, other)
    with pytest.raises(NotFoundError):
        await service.get_encounter(session, encounter.id, hospital_id=other)

    await db.set_tenant_context(session, tenant.id)
    assert (await service.get_encounter(session, encounter.id, hospital_id=tenant.id)).id


async def test_the_sweep_closes_visits_for_every_tenant(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    """The scheduled job, end to end: it finds its own tenants and runs both nets."""
    from app.core.config import settings
    from app.workers import tasks

    await service.start_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    original = settings.ENCOUNTER_AUTO_CLOSE_HOURS
    settings.ENCOUNTER_AUTO_CLOSE_HOURS = 1
    try:
        # Backdate the visit past the buffer rather than waiting an hour.
        encounter.started_at = utc_now() - timedelta(hours=3)
        session.add(encounter)
        await session.commit()

        totals = await tasks.run_sweeps()
    finally:
        settings.ENCOUNTER_AUTO_CLOSE_HOURS = original

    assert totals["completed"] == 1
    assert "failed_hospitals" not in totals

    await session.refresh(encounter)
    assert encounter.status is S.COMPLETED
    assert encounter.closed_automatically is True


async def test_the_sweep_can_be_switched_off(session: AsyncSession, tenant: Hospital) -> None:
    """A deployment must be able to run the API without the jobs."""
    from app.core.config import settings
    from app.workers import tasks

    settings.WORKER_SWEEPS_ENABLED = False
    try:
        assert await tasks.run_sweeps() == {}
    finally:
        settings.WORKER_SWEEPS_ENABLED = True
