"""The dispatcher: the channel ladder, templates, idempotency, retry.

Suppression has its own file — it is the CLAUDE.md §14 invariant and deserves to
fail loudly and separately.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.config import settings
from app.core.exceptions import ConflictError
from app.core.pagination import PageParams
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.notifications import service
from app.modules.notifications.gateways import GatewayResult, OutboundMessage
from app.modules.notifications.models import (
    NotificationCategory,
    NotificationChannel,
    NotificationStatus,
    RecipientType,
    SuppressionReason,
)
from app.modules.notifications.schemas import (
    AdhocSend,
    MessageTemplateCreate,
    MessageTemplateUpdate,
)
from app.modules.notifications.service import Recipient
from app.modules.notifications.templates import TemplateCode
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test gateways
# ---------------------------------------------------------------------------
class RecordingGateway:
    """Accepts everything and remembers what it was handed."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        self.sent.append(message)
        return GatewayResult.ok(self.name, provider_message_id=f"ok-{len(self.sent)}")


class PickyGateway:
    """Refuses some channels and accepts the rest — the ladder under test."""

    name = "picky"

    def __init__(self, *, accepts: set[NotificationChannel]) -> None:
        self.accepts = accepts
        self.tried: list[NotificationChannel] = []

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        self.tried.append(message.channel)
        if message.channel in self.accepts:
            return GatewayResult.ok(self.name)
        return GatewayResult.failure(self.name, code="rejected", detail="Channel unavailable.")


class ExplodingGateway:
    """Raises instead of returning failure, the way a real SDK eventually will."""

    name = "exploding"

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        raise RuntimeError("connection reset by peer")


class SmsOnlyGateway:
    """A provider that genuinely cannot serve some channels."""

    name = "sms-only"

    def __init__(self) -> None:
        self.tried: list[NotificationChannel] = []

    def supports(self, channel: NotificationChannel) -> bool:
        return channel is NotificationChannel.SMS

    async def send(self, message: OutboundMessage) -> GatewayResult:
        self.tried.append(message.channel)
        return GatewayResult.ok(self.name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


@pytest.fixture
async def receptionist_user(session: AsyncSession, tenant: Hospital) -> User:
    from tests.conftest import _make_user

    user = await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=tenant.id)
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
def recipient(patient: Patient) -> Recipient:
    return Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone,
        email="sunita@example.com",
        language="en",
        patient_id=patient.id,
    )


@pytest.fixture
def english_default() -> AsyncIterator[None]:
    """Most tests read plain English copy rather than the Hindi default."""
    yield


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
async def test_a_message_is_rendered_from_shipped_copy_and_sent(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """No hospital template exists yet. The copy shipped in `templates.py` is
    used, and `needs_template` stays false — that flag means a real gap, not
    "somebody has not customised the wording"."""
    gateway = RecordingGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.REPORT_READY,
        recipient=recipient,
        context={
            "hospital_name": "Sunrise",
            "patient_name": "Sunita",
            "test_name": "CBC",
            "report_number": "RPT-26-000001",
        },
        category=NotificationCategory.REPORT,
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert notification.needs_template is False
    assert notification.channel is NotificationChannel.WHATSAPP
    assert notification.attempts == 1
    assert "CBC" in notification.body
    assert "Sunrise" in notification.body
    assert len(gateway.sent) == 1


async def test_a_hospital_template_overrides_the_shipped_copy(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """The two-tier resolution: a hospital's own wording wins over ours."""
    await service.create_template(
        session,
        MessageTemplateCreate(
            code=TemplateCode.REPORT_READY,
            channel=NotificationChannel.WHATSAPP,
            language="en",
            category=NotificationCategory.REPORT,
            body="Namaste {{patient_name}}, collect your {{test_name}} from window 3.",
        ),
        hospital_id=tenant.id,
    )
    await session.commit()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.REPORT_READY,
        recipient=recipient,
        context={"patient_name": "Sunita", "test_name": "CBC"},
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert notification.body == "Namaste Sunita, collect your CBC from window 3."


async def test_an_unknown_code_still_sends_and_is_flagged_for_a_human(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """Totality, the same contract `capture_charge` follows. A code with neither
    an override nor shipped copy degrades to the generic line and raises a flag;
    it never blocks the hospital."""
    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code="A_CODE_NOBODY_WROTE",
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Sunita"},
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert notification.needs_template is True
    assert "Sunrise" in notification.body

    flagged, total = await service.list_notifications(
        session, PageParams(), hospital_id=tenant.id, needs_template=True
    )
    assert total == 1
    assert flagged[0].id == notification.id


async def test_the_patients_language_selects_the_template(
    session: AsyncSession, tenant: Hospital, patient: Patient
) -> None:
    """`preferred_language` defaults to `hi`, and the dispatcher honours it
    without anyone asking — CLAUDE.md §9."""
    hindi_recipient = Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone,
        language="hi",
        patient_id=patient.id,
    )

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.REPORT_READY,
        recipient=hindi_recipient,
        context={
            "hospital_name": "Sunrise",
            "patient_name": "सुनीता",
            "test_name": "CBC",
            "report_number": "R1",
        },
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert "रिपोर्ट" in notification.body


async def test_an_untranslated_language_falls_back_to_english(
    session: AsyncSession, tenant: Hospital, patient: Patient
) -> None:
    tamil_recipient = Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone,
        language="ta",
        patient_id=patient.id,
    )

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.REPORT_READY,
        recipient=tamil_recipient,
        context={
            "hospital_name": "Sunrise",
            "patient_name": "Sunita",
            "test_name": "CBC",
            "report_number": "R1",
        },
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert "is ready for collection" in notification.body
    # Still not a configuration gap — English copy exists and was used.
    assert notification.needs_template is False


# ---------------------------------------------------------------------------
# The ladder (CLAUDE.md §9: WhatsApp -> SMS -> email)
# ---------------------------------------------------------------------------
async def test_the_ladder_falls_through_to_the_next_channel(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """WhatsApp refuses, SMS accepts. That is the ladder working, not a failure —
    and both rungs are recorded, because "we sent it" is not an answer to a
    patient who received nothing."""
    gateway = PickyGateway(accepts={NotificationChannel.SMS})

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Sunita"},
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert notification.channel is NotificationChannel.SMS
    assert notification.attempts == 2
    assert gateway.tried == [NotificationChannel.WHATSAPP, NotificationChannel.SMS]

    attempts = await service.list_attempts(session, notification.id)
    assert [a.channel for a in attempts] == [NotificationChannel.WHATSAPP, NotificationChannel.SMS]
    assert [a.succeeded for a in attempts] == [False, True]


async def test_a_channel_with_no_address_is_skipped_not_failed(
    session: AsyncSession, tenant: Hospital, patient: Patient
) -> None:
    """A patient with no email address should not accumulate an email failure on
    every message they are ever sent. The failure log is for provider problems."""
    no_email = Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone,
        email=None,
        language="en",
        patient_id=patient.id,
    )
    gateway = PickyGateway(accepts=set())

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=no_email,
        context={"hospital_name": "Sunrise", "patient_name": "Sunita"},
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.FAILED
    assert gateway.tried == [NotificationChannel.WHATSAPP, NotificationChannel.SMS]
    assert NotificationChannel.EMAIL not in gateway.tried


async def test_a_channel_the_provider_cannot_serve_is_skipped(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    gateway = SmsOnlyGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Sunita"},
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert gateway.tried == [NotificationChannel.SMS]


async def test_a_gateway_that_raises_does_not_end_the_walk(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """A third-party SDK will eventually throw. One broken provider must not stop
    the next channel from being tried — that is the entire reason for a ladder."""
    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Sunita"},
        gateway=ExplodingGateway(),
    )
    await session.commit()

    assert notification.status is NotificationStatus.FAILED
    attempts = await service.list_attempts(session, notification.id)
    assert len(attempts) == 3
    assert all(a.error_code == "gateway_exception" for a in attempts)
    assert "connection reset" in (notification.last_error or "")


async def test_restricting_a_message_to_one_channel_uses_only_that_channel(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    gateway = PickyGateway(accepts={NotificationChannel.EMAIL})

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "Sunrise", "patient_name": "Sunita"},
        restrict_channel=NotificationChannel.EMAIL,
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SENT
    assert gateway.tried == [NotificationChannel.EMAIL]


async def test_a_recipient_with_no_contact_details_fails_rather_than_suppresses(
    session: AsyncSession, tenant: Hospital, patient: Patient
) -> None:
    """Nobody *chose* not to message them, and reception can fix a missing phone
    number. FAILED puts it on a worklist; SUPPRESSED would file it as intended."""
    unreachable = Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=None,
        email=None,
        patient_id=patient.id,
    )

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=unreachable,
        context={},
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert notification.status is NotificationStatus.FAILED
    assert notification.suppression_reason is None
    assert "contact details" in (notification.last_error or "")


async def test_email_gets_a_subject_and_sms_does_not(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """The ladder re-renders per channel, because an SMS and an email are not the
    same text and the record must say what was actually sent."""
    gateway = PickyGateway(accepts={NotificationChannel.EMAIL})

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.REPORT_READY,
        recipient=recipient,
        context={
            "hospital_name": "Sunrise",
            "patient_name": "Sunita",
            "test_name": "CBC",
            "report_number": "R1",
        },
        gateway=gateway,
    )
    await session.commit()

    assert notification.channel is NotificationChannel.EMAIL
    assert notification.subject == "Your report is ready"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
async def test_the_same_source_event_twice_produces_one_message(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """A re-delivered event or a double-click must not tell a patient twice that
    the same report is ready."""
    report_id = uuid.uuid4()
    kwargs = {
        "hospital_id": tenant.id,
        "code": TemplateCode.REPORT_READY,
        "recipient": recipient,
        "context": {
            "hospital_name": "S",
            "patient_name": "Sunita",
            "test_name": "CBC",
            "report_number": "R1",
        },
        "source_module": "diagnostics",
        "source_type": "report",
        "source_id": report_id,
    }

    first = await service.notify(session, gateway=RecordingGateway(), **kwargs)  # type: ignore[arg-type]
    second = await service.notify(session, gateway=RecordingGateway(), **kwargs)  # type: ignore[arg-type]
    await session.commit()

    assert first.id == second.id
    _, total = await service.list_notifications(session, PageParams(), hospital_id=tenant.id)
    assert total == 1


async def test_a_failed_message_can_be_raised_again_for_the_same_source(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """The idempotency index excludes FAILED on purpose: a message that never
    reached anybody is not a duplicate the second time."""
    report_id = uuid.uuid4()
    kwargs = {
        "hospital_id": tenant.id,
        "code": TemplateCode.REPORT_READY,
        "recipient": recipient,
        "context": {"hospital_name": "S", "patient_name": "Sunita"},
        "source_module": "diagnostics",
        "source_type": "report",
        "source_id": report_id,
    }

    failed = await service.notify(session, gateway=PickyGateway(accepts=set()), **kwargs)  # type: ignore[arg-type]
    assert failed.status is NotificationStatus.FAILED

    retried = await service.notify(session, gateway=RecordingGateway(), **kwargs)  # type: ignore[arg-type]
    await session.commit()

    assert retried.id != failed.id
    assert retried.status is NotificationStatus.SENT


# ---------------------------------------------------------------------------
# Retry and cancel
# ---------------------------------------------------------------------------
async def test_retrying_a_failed_message_walks_the_ladder_again(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "S", "patient_name": "Sunita"},
        gateway=PickyGateway(accepts=set()),
    )
    assert notification.status is NotificationStatus.FAILED
    before = notification.attempts

    # The provider recovers.
    from app.modules.notifications import service as svc

    notification.status = NotificationStatus.PENDING
    notification.failed_at = None
    session.add(notification)
    await session.flush()
    updated = await svc.dispatch(session, notification, gateway=RecordingGateway())
    await session.commit()

    assert updated.status is NotificationStatus.SENT
    assert updated.attempts > before


async def test_retry_refuses_once_the_attempt_budget_is_spent(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """Retrying a number that does not exist forever costs money and burns
    provider reputation. After the budget, somebody picks up a phone."""
    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "S", "patient_name": "Sunita"},
        gateway=PickyGateway(accepts=set()),
    )
    # The budget is in ladder walks, not rungs: one walk of a three-channel
    # ladder already tries everything, so budgeting in rungs would retire a
    # message after a single provider outage.
    assert notification.attempts == 3
    assert notification.dispatch_rounds == 1

    notification.dispatch_rounds = settings.NOTIFICATION_MAX_ATTEMPTS
    session.add(notification)
    await session.flush()

    with pytest.raises(ConflictError, match="already been tried"):
        await service.retry(session, notification)


async def test_a_sent_message_cannot_be_retried_or_cancelled(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "S", "patient_name": "Sunita"},
        gateway=RecordingGateway(),
    )
    await session.commit()

    with pytest.raises(ConflictError, match="Only a failed message"):
        await service.retry(session, notification)
    with pytest.raises(ConflictError, match="already been sent"):
        await service.cancel(session, notification, reason="changed my mind")


async def test_the_retry_sweep_picks_up_failed_messages(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """The worker's safety net. Note it re-checks suppression on the way — see
    the suppression tests for why that matters more than it looks."""
    await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "S", "patient_name": "Sunita"},
        gateway=PickyGateway(accepts=set()),
    )
    await session.commit()

    result = await service.retry_failed(session, hospital_id=tenant.id)
    await session.commit()

    assert result["notifications_retried"] == 1


# ---------------------------------------------------------------------------
# Ad-hoc messages
# ---------------------------------------------------------------------------
async def test_staff_can_send_a_message_they_wrote_themselves(
    session: AsyncSession, tenant: Hospital, patient: Patient, receptionist_user: User
) -> None:

    notification = await service.send_adhoc(
        session,
        AdhocSend(
            patient_id=patient.id,
            body="Please bring your old X-ray films to tomorrow's visit.",
        ),
        hospital_id=tenant.id,
        actor=receptionist_user,
    )
    await session.commit()

    assert notification.body == "Please bring your old X-ray films to tomorrow's visit."
    assert notification.body_is_custom is True
    assert notification.triggered_by_id == receptionist_user.id


async def test_a_hand_written_body_survives_a_fall_through_to_another_channel(
    session: AsyncSession, tenant: Hospital, patient: Patient, receptionist_user: User
) -> None:
    """The ladder re-renders per channel from the template. A message staff typed
    has no template, and must not be overwritten on the way down."""
    written = "Bring your films. Ward 3, 9am."

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=Recipient(
            recipient_type=RecipientType.PATIENT,
            name=patient.full_name,
            phone=patient.phone,
            email="s@example.com",
            language="en",
            patient_id=patient.id,
        ),
        context={},
        body_override=written,
        gateway=PickyGateway(accepts={NotificationChannel.EMAIL}),
    )
    await session.commit()

    assert notification.channel is NotificationChannel.EMAIL
    assert notification.body == written


async def test_an_adhoc_message_needs_a_recipient(session: AsyncSession, tenant: Hospital) -> None:
    with pytest.raises(ValueError, match="patient_id"):
        AdhocSend(body="hello")


async def test_an_adhoc_message_needs_something_to_say(
    session: AsyncSession, patient: Patient
) -> None:
    with pytest.raises(ValueError, match="template_code or a body"):
        AdhocSend(patient_id=patient.id)


# ---------------------------------------------------------------------------
# Templates as data
# ---------------------------------------------------------------------------
async def test_a_duplicate_template_is_refused(session: AsyncSession, tenant: Hospital) -> None:
    payload = MessageTemplateCreate(
        code="REPORT_READY",
        channel=NotificationChannel.SMS,
        language="en",
        body="hello {{patient_name}}",
    )
    await service.create_template(session, payload, hospital_id=tenant.id)
    await session.commit()

    with pytest.raises(ConflictError, match="already exists"):
        await service.create_template(session, payload, hospital_id=tenant.id)


async def test_only_an_email_template_may_carry_a_subject() -> None:
    """Silently dropping a field somebody typed is how a hospital ends up
    believing its SMS has a headline."""
    with pytest.raises(ValueError, match="subject line"):
        MessageTemplateCreate(
            code="X", channel=NotificationChannel.SMS, body="hi", subject="Hello there"
        )


async def test_editing_a_template_does_not_rewrite_messages_already_sent(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """The reason `Notification` freezes its rendered body. A hospital fixing a
    typo today must not silently change what it told a patient last week."""
    template = await service.create_template(
        session,
        MessageTemplateCreate(
            code=TemplateCode.FOLLOW_UP_REMINDER,
            channel=NotificationChannel.WHATSAPP,
            language="en",
            body="Original wording for {{patient_name}}.",
        ),
        hospital_id=tenant.id,
    )
    await session.commit()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"patient_name": "Sunita"},
        gateway=RecordingGateway(),
    )
    await session.commit()
    assert notification.body == "Original wording for Sunita."

    await service.update_template(
        session, template, MessageTemplateUpdate(body="Completely different wording.")
    )
    await session.commit()
    await session.refresh(notification)

    assert notification.body == "Original wording for Sunita."


async def test_an_inactive_template_is_ignored_in_favour_of_shipped_copy(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    template = await service.create_template(
        session,
        MessageTemplateCreate(
            code=TemplateCode.REPORT_READY,
            channel=NotificationChannel.WHATSAPP,
            language="en",
            body="Retired wording.",
        ),
        hospital_id=tenant.id,
    )
    await service.update_template(session, template, MessageTemplateUpdate(is_active=False))
    await session.commit()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.REPORT_READY,
        recipient=recipient,
        context={
            "hospital_name": "Sunrise",
            "patient_name": "Sunita",
            "test_name": "CBC",
            "report_number": "R1",
        },
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert notification.body != "Retired wording."
    assert "CBC" in notification.body


async def test_a_template_supplies_the_category_a_suppression_is_matched_on(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    """Whoever wrote the copy knows better than the trigger what kind of message
    it is, and the category is what a patient's opt-out is matched against."""
    await service.create_template(
        session,
        MessageTemplateCreate(
            code="CUSTOM_NOTE",
            channel=NotificationChannel.WHATSAPP,
            language="en",
            category=NotificationCategory.BILLING,
            body="A note.",
        ),
        hospital_id=tenant.id,
    )
    await session.commit()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code="CUSTOM_NOTE",
        recipient=recipient,
        context={},
        category=NotificationCategory.ADMINISTRATIVE,
        gateway=RecordingGateway(),
    )
    await session.commit()

    assert notification.category is NotificationCategory.BILLING


# ---------------------------------------------------------------------------
# The master switch
# ---------------------------------------------------------------------------
async def test_disabling_messaging_records_the_message_rather_than_skipping_it(
    session: AsyncSession,
    tenant: Hospital,
    recipient: Recipient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hospital that switched messaging off six months ago should be able to
    discover that is why nobody is being reminded of anything — which an empty
    outbox does not tell them."""
    monkeypatch.setattr(settings, "NOTIFICATIONS_ENABLED", False)
    gateway = RecordingGateway()

    notification = await service.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context={"hospital_name": "S", "patient_name": "Sunita"},
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SUPPRESSED
    assert notification.suppression_reason is SuppressionReason.DISABLED
    assert gateway.sent == []
