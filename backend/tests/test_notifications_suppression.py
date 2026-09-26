"""Never message a deceased patient — CLAUDE.md §14, tested at every layer.

This is the file to read first if you are changing anything in `notifications`.
The rule is enforced three times, and each test below names which layer it is
holding down:

1. **enqueue** — the ordinary case.
2. **dispatch** — the case that actually happens: a reminder queued on Monday,
   a death on Wednesday, a retry sweep on Friday.
3. **the database** — a trigger, for callers that do not exist yet.

Plus the two things that keep the rule from becoming a nuisance: staff-directed
messages *about* a deceased patient still go out (CLAUDE.md §6 requires the
settlement flow to run), and a death suppression can never be lifted by anybody.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.events import event_bus
from app.core.exceptions import ConflictError, ValidationError
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.events import PatientDeceased
from app.modules.clinical.models import Encounter, EncounterStatus
from app.modules.clinical.schemas import DeathRecord
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.notifications import service
from app.modules.notifications.gateways import GatewayResult, OutboundMessage
from app.modules.notifications.models import (
    NotificationAttempt,
    NotificationCategory,
    NotificationChannel,
    NotificationStatus,
    RecipientType,
    SuppressionReason,
)
from app.modules.notifications.schemas import SuppressionCreate
from app.modules.notifications.service import Recipient
from app.modules.notifications.templates import TemplateCode
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


class RecordingGateway:
    """Remembers everything it was asked to send. Nothing should reach it."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        self.sent.append(message)
        return GatewayResult.ok(self.name)


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
async def records_officer(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.RECORDS_OFFICER)


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Ram Prasad", phone="9812345670", gender=Gender.MALE, age_years=71
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
def recipient(patient: Patient) -> Recipient:
    return Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone,
        email="ram@example.com",
        language="en",
        patient_id=patient.id,
    )


@pytest.fixture
async def encounter(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> Encounter:
    record = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    return record


async def _record_death(session: AsyncSession, encounter: Encounter, doctor_user: User) -> None:
    await clinical_service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=utc_now(),
            death_certified_by_id=doctor_user.id,
            cause_of_death="Cardiac arrest.",
        ),
        actor=doctor_user,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Layer 1 — enqueue
# ---------------------------------------------------------------------------
async def test_a_message_to_a_deceased_patient_is_never_queued(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    recipient: Recipient,
) -> None:
    """Layer one. The message is recorded as SUPPRESSED with its reason rather
    than silently dropped — an auditor asks *why* nothing was sent."""
    await _record_death(session, encounter, doctor_user)
    gateway = RecordingGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Ram Prasad"},
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SUPPRESSED
    assert notification.suppression_reason is SuppressionReason.DECEASED
    assert gateway.sent == []
    assert await service.list_attempts(session, notification.id) == []


async def test_recording_a_death_writes_a_permanent_suppression(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    patient: Patient,
) -> None:
    """Written by the transactional subscriber, inside the death's own
    transaction — so it cannot be lost the way a fire-and-forget handler can."""
    await _record_death(session, encounter, doctor_user)

    blocks = await service.active_suppressions(
        session, hospital_id=tenant.id, patient_id=patient.id
    )

    assert len(blocks) == 1
    assert blocks[0].reason is SuppressionReason.DECEASED
    assert blocks[0].channel is None  # every channel
    assert blocks[0].category is None  # every category
    assert blocks[0].is_permanent


async def test_recording_a_death_cancels_messages_already_queued(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    patient: Patient,
    recipient: Recipient,
) -> None:
    """Suppression alone would stop these at dispatch. Clearing them means the
    dangerous rows do not exist, rather than merely not being read — the next
    person to write a bulk-send script does not have to know the rule."""
    queued = await service.enqueue(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Ram Prasad"},
    )
    await session.commit()
    assert queued.status is NotificationStatus.PENDING

    await _record_death(session, encounter, doctor_user)
    await session.refresh(queued)

    assert queued.status is NotificationStatus.CANCELLED
    assert queued.suppression_reason is SuppressionReason.DECEASED


# ---------------------------------------------------------------------------
# Layer 2 — dispatch
# ---------------------------------------------------------------------------
async def test_a_message_queued_before_a_death_is_refused_at_send(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    patient: Patient,
    recipient: Recipient,
) -> None:
    """**The case this whole design exists for.**

    A reminder is queued while the patient is alive. The patient dies. Something
    later tries to send it — here, directly, standing in for the retry sweep days
    afterwards. A check performed only at enqueue passed and is now irrelevant.

    The queued row is force-set back to PENDING after the death precisely to
    defeat the cancel-the-queue step, so that this test exercises the dispatch
    check and nothing else.
    """
    queued = await service.enqueue(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Ram Prasad"},
    )
    await session.commit()

    await _record_death(session, encounter, doctor_user)

    # Undo the queue-clearing so only layer two can save us.
    await session.refresh(queued)
    queued.status = NotificationStatus.PENDING
    queued.suppression_reason = None
    session.add(queued)
    await session.flush()

    gateway = RecordingGateway()
    result = await service.dispatch(session, queued, gateway=gateway)
    await session.commit()

    assert result.status is NotificationStatus.SUPPRESSED
    assert result.suppression_reason is SuppressionReason.DECEASED
    assert gateway.sent == []


async def test_the_retry_sweep_does_not_resurrect_a_message_for_a_dead_patient(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    recipient: Recipient,
) -> None:
    """The same case, through the worker path rather than by hand."""

    class DeadGateway:
        name = "dead"

        def supports(self, channel: NotificationChannel) -> bool:
            return True

        async def send(self, message: OutboundMessage) -> GatewayResult:
            return GatewayResult.failure(self.name, code="down")

    failed = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Ram Prasad"},
        gateway=DeadGateway(),
    )
    await session.commit()
    assert failed.status is NotificationStatus.FAILED

    await _record_death(session, encounter, doctor_user)

    await service.retry_failed(session, hospital_id=tenant.id)
    await session.commit()
    await session.refresh(failed)

    assert failed.status is NotificationStatus.SUPPRESSED
    assert failed.suppression_reason is SuppressionReason.DECEASED


# ---------------------------------------------------------------------------
# Layer 3 — the database
# ---------------------------------------------------------------------------
async def test_the_database_refuses_a_delivery_attempt_for_a_deceased_patient(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    recipient: Recipient,
) -> None:
    """Layer three: the backstop for code that does not exist yet.

    Written deliberately *around* the service layer — a raw INSERT of an attempt
    row, exactly what a future module or a well-meaning bulk script might do.
    The trigger refuses it. Same philosophy as row-level security under
    multi-tenancy (CLAUDE.md §3): the application is expected to be correct, and
    the database makes incorrectness impossible.
    """
    queued = await service.enqueue(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Ram Prasad"},
    )
    await session.commit()

    await _record_death(session, encounter, doctor_user)

    with pytest.raises(Exception, match="is deceased"):
        session.add(
            NotificationAttempt(
                hospital_id=tenant.id,
                notification_id=queued.id,
                attempt=1,
                channel=NotificationChannel.SMS,
                address="9812345670",
                succeeded=True,
                gateway="bypassed-everything",
            )
        )
        await session.flush()

    await session.rollback()


async def test_the_database_allows_an_attempt_for_a_living_patient(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """The guard has to let the hospital work. A trigger that refuses everything
    would pass the test above and break every message ever sent."""
    queued = await service.enqueue(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Ram Prasad"},
    )
    session.add(
        NotificationAttempt(
            hospital_id=tenant.id,
            notification_id=queued.id,
            attempt=1,
            channel=NotificationChannel.SMS,
            address="9812345670",
            succeeded=True,
            gateway="test",
        )
    )
    await session.flush()
    await session.commit()

    assert len(await service.list_attempts(session, queued.id)) == 1


# ---------------------------------------------------------------------------
# What the rule must NOT break
# ---------------------------------------------------------------------------
async def test_staff_are_still_told_about_a_deceased_patients_unpaid_bill(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    patient: Patient,
) -> None:
    """CLAUDE.md §6 requires the settlement flow to run for a deceased patient —
    the bill does not leave with them. A message *about* the patient addressed to
    *staff* is legitimate; the same fact sent to the family's mobile is not. The
    whole rule turns on `recipient_type`."""
    await _record_death(session, encounter, doctor_user)
    gateway = RecordingGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.SETTLEMENT_REQUIRED,
        recipient=Recipient(
            recipient_type=RecipientType.STAFF,
            name=doctor_user.full_name,
            phone="9800000001",
            email=doctor_user.email,
            user_id=doctor_user.id,
            patient_id=patient.id,
        ),
        context={
            "hospital_name": "Sunrise",
            "patient_name": "Ram Prasad",
            "balance_due": "Rs 500.00",
            "context_status": "Deceased",
        },
        category=NotificationCategory.BILLING,
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert len(gateway.sent) == 1


async def test_a_critical_result_reaches_a_doctor_whatever_the_patient_opted_out_of(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """A potassium of 7.2 is not a marketing message."""
    await service.suppress(
        session,
        SuppressionCreate(patient_id=patient.id, reason=SuppressionReason.OPTED_OUT),
        hospital_id=tenant.id,
    )
    await session.commit()
    gateway = RecordingGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.CRITICAL_RESULT,
        recipient=Recipient(
            recipient_type=RecipientType.STAFF,
            name=doctor_user.full_name,
            phone="9800000002",
            email=doctor_user.email,
            user_id=doctor_user.id,
            patient_id=patient.id,
        ),
        context={
            "patient_name": "Ram Prasad",
            "uhid": patient.uhid,
            "test_name": "Potassium",
            "critical_values": "7.2 mmol/L",
        },
        category=NotificationCategory.CRITICAL_ALERT,
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert len(gateway.sent) == 1


# ---------------------------------------------------------------------------
# Lifting — and the one that never can be
# ---------------------------------------------------------------------------
async def test_a_death_suppression_can_never_be_lifted(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    patient: Patient,
) -> None:
    """Refused in the service rather than by RBAC, deliberately. Permissions are
    data-driven rows a hospital administrator can edit (CLAUDE.md §8) — which is
    a feature everywhere except here. There is no permission that lifts a death
    because there is no code path that does."""
    await _record_death(session, encounter, doctor_user)
    blocks = await service.active_suppressions(
        session, hospital_id=tenant.id, patient_id=patient.id
    )

    with pytest.raises(ConflictError, match="cannot be lifted"):
        await service.lift_suppression(session, blocks[0], reason="Recorded in error")


async def test_a_death_suppression_cannot_be_created_by_hand(patient: Patient) -> None:
    """One way in, and it is the clinical death entry. Two sources of truth for
    this rule would be one too many."""
    with pytest.raises(ValueError, match="not by hand"):
        SuppressionCreate(patient_id=patient.id, reason=SuppressionReason.DECEASED)


async def test_the_configuration_reason_is_not_a_patient_block(patient: Patient) -> None:
    with pytest.raises(ValueError, match="NOTIFICATIONS_ENABLED"):
        SuppressionCreate(patient_id=patient.id, reason=SuppressionReason.DISABLED)


async def test_the_service_also_refuses_those_reasons_directly(
    session: AsyncSession, tenant: Hospital, patient: Patient
) -> None:
    """Belt and braces: the schema validator is the front door, not the only one.
    A future caller building the payload another way still gets refused."""
    payload = SuppressionCreate(patient_id=patient.id, reason=SuppressionReason.OPTED_OUT)
    object.__setattr__(payload, "reason", SuppressionReason.DECEASED)

    with pytest.raises(ValidationError, match="not recorded by hand"):
        await service.suppress(session, payload, hospital_id=tenant.id)


# ---------------------------------------------------------------------------
# Opt-outs (DPDP Act 2023 §6 — consent is withdrawable)
# ---------------------------------------------------------------------------
async def test_an_opt_out_stops_messages_and_can_be_lifted(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    recipient: Recipient,
    records_officer: User,
) -> None:
    block = await service.suppress(
        session,
        SuppressionCreate(
            patient_id=patient.id, reason=SuppressionReason.OPTED_OUT, note="Asked at the counter."
        ),
        hospital_id=tenant.id,
        actor=records_officer,
    )
    await session.commit()

    blocked = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "S", "patient_name": "Ram"},
        gateway=RecordingGateway(),
    )
    assert blocked.status is NotificationStatus.SUPPRESSED
    assert blocked.suppression_reason is SuppressionReason.OPTED_OUT

    await service.lift_suppression(
        session, block, reason="Patient asked to resume.", actor=records_officer
    )
    await session.commit()

    allowed = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "S", "patient_name": "Ram"},
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert allowed.status is NotificationStatus.SENT


async def test_a_category_opt_out_is_narrow(
    session: AsyncSession, tenant: Hospital, patient: Patient, recipient: Recipient
) -> None:
    """A single global unsubscribe is the wrong granularity for a hospital. A
    patient who wants no billing reminders still wants their biopsy result."""
    await service.suppress(
        session,
        SuppressionCreate(
            patient_id=patient.id,
            reason=SuppressionReason.OPTED_OUT,
            category=NotificationCategory.BILLING,
        ),
        hospital_id=tenant.id,
    )
    await session.commit()

    billing = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.INVOICE_ISSUED,
        recipient=recipient,
        context={
            "hospital_name": "S",
            "patient_name": "Ram",
            "invoice_number": "INV-1",
            "grand_total": "Rs 500",
            "balance_due": "Rs 500",
        },
        gateway=RecordingGateway(),
    )
    report = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.REPORT_READY,
        recipient=recipient,
        context={
            "hospital_name": "S",
            "patient_name": "Ram",
            "test_name": "Biopsy",
            "report_number": "R1",
        },
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert billing.status is NotificationStatus.SUPPRESSED
    assert report.status is NotificationStatus.SENT


async def test_recording_the_same_block_twice_returns_the_same_row(
    session: AsyncSession, tenant: Hospital, patient: Patient
) -> None:
    payload = SuppressionCreate(patient_id=patient.id, reason=SuppressionReason.OPTED_OUT)

    first = await service.suppress(session, payload, hospital_id=tenant.id)
    second = await service.suppress(session, payload, hospital_id=tenant.id)
    await session.commit()

    assert first.id == second.id


async def test_a_lifted_block_cannot_be_lifted_again(
    session: AsyncSession, tenant: Hospital, patient: Patient
) -> None:
    block = await service.suppress(
        session,
        SuppressionCreate(patient_id=patient.id, reason=SuppressionReason.OPTED_OUT),
        hospital_id=tenant.id,
    )
    await service.lift_suppression(session, block, reason="Resumed.")
    await session.commit()

    with pytest.raises(ConflictError, match="already been lifted"):
        await service.lift_suppression(session, block, reason="Again.")


# ---------------------------------------------------------------------------
# The follow-up gate (CLAUDE.md §6)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "final_status",
    [
        EncounterStatus.LAMA.value,
        EncounterStatus.REFERRED_OUT.value,
        EncounterStatus.DECEASED.value,
    ],
)
async def test_no_follow_up_nudge_for_a_visit_that_did_not_end_well(
    session: AsyncSession, tenant: Hospital, patient: Patient, final_status: str
) -> None:
    """Gated on COMPLETED alone, in the handler.

    Suppression would catch the deceased case on its own, but LAMA and
    REFERRED_OUT are neither deceased nor completed — and "please book your
    follow-up" to a patient who discharged themselves against advice, or who was
    sent to another hospital, is wrong in a way no suppression rule catches.
    """
    from app.modules.clinical.events import EncounterClosed
    from app.modules.notifications import handlers

    await handlers.notify_follow_up(
        EncounterClosed(
            hospital_id=tenant.id,
            encounter_id=patient.id,  # unused on this path
            patient_id=patient.id,
            final_status=final_status,
            requires_settlement=True,
            closed_automatically=False,
        )
    )

    _, total = await service.list_notifications(session, PageParams(), hospital_id=tenant.id)
    assert total == 0


# ---------------------------------------------------------------------------
# The channel choice behind the suppression handler
# ---------------------------------------------------------------------------
async def test_death_suppression_is_registered_transactionally() -> None:
    """Guards the design decision, not just its behaviour.

    Every other subscriber in this module is fire-and-forget so a flaky gateway
    cannot fail a clinical write. This one is the exception: it calls no gateway,
    writes to the same database in the same transaction, and what it writes *is*
    the invariant — so a swallowed exception would lose the rule itself. If
    somebody moves it onto the other channel, this fails.
    """
    transactional = event_bus._transactional.get(PatientDeceased, [])
    fire_and_forget = event_bus._handlers.get(PatientDeceased, [])

    assert any(h.__name__ == "suppress_on_death" for h in transactional)
    assert not any(h.__name__ == "suppress_on_death" for h in fire_and_forget)


async def test_every_outbound_trigger_is_fire_and_forget() -> None:
    """The mirror image: nothing that talks to a provider may sit in a clinical
    transaction. A gateway timeout must never roll back a consultation."""
    from app.modules.billing.events import InvoiceIssued, PaymentReceived, SettlementRequired
    from app.modules.clinical.events import EncounterClosed
    from app.modules.diagnostics.events import CriticalResultFlagged, ReportReady, SpecimenRejected
    from app.modules.scheduling.events import AppointmentBooked, AppointmentCancelled

    outbound = (
        AppointmentBooked,
        AppointmentCancelled,
        ReportReady,
        CriticalResultFlagged,
        SpecimenRejected,
        PaymentReceived,
        InvoiceIssued,
        SettlementRequired,
        EncounterClosed,
    )

    for event_type in outbound:
        names = [h.__name__ for h in event_bus._transactional.get(event_type, [])]
        assert not any(name.startswith("notify_") for name in names), (
            f"{event_type.__name__} has a notification handler on the transactional channel"
        )
