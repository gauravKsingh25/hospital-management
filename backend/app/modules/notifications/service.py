"""Notification business logic — the module's public interface.

CLAUDE.md §13 step 8: a channel-agnostic dispatcher (WhatsApp → SMS → email),
templates, triggers wired to domain events, and a hard suppression rule for
deceased patients. This file is the dispatcher and the suppression rule; the
triggers are in `handlers.py`.

---------------------------------------------------------------------------
The one invariant, and why it is checked three times
---------------------------------------------------------------------------

CLAUDE.md §14 lists *never send a notification to a deceased patient* as one of
two invariants to actively guard. The naive reading is "check `is_deceased`
before sending", and that check exists — but on its own it is not enough, for a
reason worth spelling out because it is the case that actually happens:

    Monday   a visit closes; a follow-up reminder is queued.
    Wednesday the patient dies.
    Friday   the retry sweep picks the message up and sends it.

A check performed only at enqueue passes on Monday and is irrelevant by Friday.
So the rule is enforced at three altitudes, and each one covers a different way
of getting it wrong:

1. **`enqueue`** — refuses and records `SUPPRESSED` with the reason. Covers the
   ordinary case and gives the audit trail its answer.
2. **`dispatch`** — re-checks immediately before the first gateway call, reading
   the patient afresh. Covers everything that ages between queueing and sending,
   which is the case above.
3. **The database** — a trigger on `notification_attempts` refuses the INSERT
   for a deceased patient (see the RLS migration). Covers a future caller that
   bypasses this module entirely. Same philosophy as row-level security in
   CLAUDE.md §3: the application is expected to be correct, and the database
   makes incorrectness impossible rather than unlikely.

The distinction that makes all three work is `recipient_type`. A message
**about** a deceased patient addressed to **staff** — "this bill still has to be
settled with the family" — is legitimate and required by CLAUDE.md §6, which
says the settlement flow runs for deceased patients. The same fact addressed to
the patient's own mobile is the thing that must never happen. Suppression keys
on `recipient_type = PATIENT`, so the hospital keeps working and the family is
never messaged.

---------------------------------------------------------------------------
Delivery model: send inline, on the fire-and-forget channel
---------------------------------------------------------------------------

`handlers.py` subscribes with `event_bus.subscribe` (not
`subscribe_transactional`), opens its own session, and calls `notify` — which
writes the row and walks the ladder in the same call. A flaky WhatsApp provider
therefore cannot fail a clinical write, which is the property `core/events.py`
built the fire-and-forget channel for.

Two costs come with that choice, and they are accepted rather than hidden:

* **A handler exception is logged and swallowed by design**, so a message can be
  lost without anybody being told. Mitigated by writing the `Notification` row
  *before* touching a gateway — once the row exists the message is recoverable,
  and `retry_failed` sweeps it up — but the window before that first flush is
  genuinely unprotected.
* **The publisher's transaction has not committed yet** when a fire-and-forget
  handler runs. If it rolls back, a message has gone out about something that
  never happened. Handlers therefore render from the **event payload**, never by
  re-reading the row the publisher just wrote, so the failure is bounded to a
  message about a rolled-back action rather than a crash or a wrong message.

The alternative — writing the row transactionally and dispatching from the
worker — removes both, at the cost of a second moving part. It was considered
and not taken; if the phantom-message case ever bites in production, that is the
change to make, and `enqueue`/`dispatch` are already separate functions so it is
a small one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.config import settings
from app.core.events import event_bus
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.identity.models import User
from app.modules.notifications import templates as tpl
from app.modules.notifications.events import (
    NotificationFailed,
    NotificationSent,
    NotificationSuppressed,
    NotificationTemplateMissing,
)
from app.modules.notifications.gateways import (
    Gateway,
    GatewayResult,
    OutboundMessage,
    get_gateway,
)
from app.modules.notifications.models import (
    TERMINAL_STATUSES,
    Notification,
    NotificationAttempt,
    NotificationCategory,
    NotificationChannel,
    NotificationStatus,
    NotificationSuppression,
    NotificationTemplate,
    RecipientType,
    SuppressionReason,
)
from app.modules.notifications.schemas import (
    AdhocSend,
    MessageTemplateCreate,
    MessageTemplateUpdate,
    SuppressionCreate,
)
from app.modules.patients import service as patients_service

logger = logging.getLogger(__name__)

__all__ = [
    "Recipient",
    "active_suppressions",
    "cancel",
    "cancel_pending_for_patient",
    "create_template",
    "dispatch",
    "enqueue",
    "get_notification",
    "get_suppression",
    "get_template",
    "lift_suppression",
    "list_attempts",
    "list_notifications",
    "list_suppressions",
    "list_templates",
    "notify",
    "resolve_template",
    "retry",
    "retry_failed",
    "send_adhoc",
    "suppress",
    "suppress_for_death",
    "update_template",
]


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True, slots=True)
class Recipient:
    """Where a message goes, resolved once and then snapshotted onto the row.

    Deliberately a value object rather than an ORM row. The dispatcher must not
    hold a `Patient` across a gateway call: the interesting failure is a lazy
    attribute load firing inside an adapter, and the way to make that impossible
    is to not have the object.
    """

    recipient_type: RecipientType
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    language: str = tpl.DEFAULT_LANGUAGE
    patient_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    def address_for(self, channel: NotificationChannel) -> str | None:
        if channel is NotificationChannel.EMAIL:
            return self.email
        return self.phone


async def _patient_recipient(session: AsyncSession, patient_id: uuid.UUID) -> Recipient:
    """Read a patient's contact details through `patients.service` (CLAUDE.md §2).

    The guardian's number is used when the patient has none of their own — a
    minor or an unconscious admission is registered against whoever brought
    them. Note that this makes the guardian's phone a *patient-directed*
    address, which is exactly right for the deceased rule: CLAUDE.md §6 forbids
    the follow-up SMS reaching the family, and the family's number is usually
    this one.
    """
    patient = await patients_service.get_patient(session, patient_id)
    return Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone or patient.guardian_phone,
        email=patient.email,
        language=patient.preferred_language or tpl.DEFAULT_LANGUAGE,
        patient_id=patient.id,
    )


def _staff_recipient(user: User) -> Recipient:
    return Recipient(
        recipient_type=RecipientType.STAFF,
        name=user.full_name,
        phone=user.phone,
        email=user.email,
        # Staff-facing alerts stay in English: a critical-value callout is read
        # under time pressure by whoever is on shift, and a half-translated
        # clinical alert is worse than a plain one.
        language=tpl.DEFAULT_LANGUAGE,
        user_id=user.id,
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
async def create_template(
    session: AsyncSession, payload: MessageTemplateCreate, *, hospital_id: uuid.UUID
) -> NotificationTemplate:
    existing = await _find_template(
        session,
        hospital_id=hospital_id,
        code=payload.code,
        channel=payload.channel,
        language=payload.language,
    )
    if existing is not None:
        raise ConflictError(
            f"A {payload.channel.value} template for {payload.code} already exists "
            f"in {payload.language}.",
            code="template_exists",
            details={"template_id": str(existing.id)},
        )

    template = NotificationTemplate(
        hospital_id=hospital_id,
        code=payload.code.strip().upper(),
        channel=payload.channel,
        language=payload.language.strip().lower(),
        category=payload.category,
        subject=payload.subject,
        body=payload.body,
        description=payload.description,
    )
    session.add(template)
    await session.flush()
    await session.refresh(template)
    return template


async def get_template(
    session: AsyncSession, template_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> NotificationTemplate:
    template = (
        (
            await session.execute(
                select(NotificationTemplate).where(
                    col(NotificationTemplate.id) == template_id,
                    col(NotificationTemplate.hospital_id) == hospital_id,
                    col(NotificationTemplate.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if template is None:
        raise NotFoundError("Template not found.", code="template_not_found")
    return template


async def update_template(
    session: AsyncSession, template: NotificationTemplate, payload: MessageTemplateUpdate
) -> NotificationTemplate:
    """Edit the wording. Does not touch messages already sent.

    Worth stating because it is the whole reason `Notification` freezes its
    rendered body: a hospital correcting a typo today must not silently rewrite
    what it told a patient last week.
    """
    if payload.subject is not None and template.channel is not NotificationChannel.EMAIL:
        raise ValidationError("Only an email template has a subject line.")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(template, field, value)
    session.add(template)
    await session.flush()
    await session.refresh(template)
    return template


async def list_templates(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    code: str | None = None,
    channel: NotificationChannel | None = None,
    language: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[NotificationTemplate], int]:
    filters: list[ColumnElement[bool]] = [
        col(NotificationTemplate.hospital_id) == hospital_id,
        col(NotificationTemplate.deleted_at).is_(None),
    ]
    if code:
        filters.append(col(NotificationTemplate.code) == code.strip().upper())
    if channel is not None:
        filters.append(col(NotificationTemplate.channel) == channel)
    if language:
        filters.append(col(NotificationTemplate.language) == language.strip().lower())
    if not include_inactive:
        filters.append(col(NotificationTemplate.is_active).is_(True))

    total = (
        await session.execute(
            select(func.count()).select_from(NotificationTemplate).where(*filters)
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(NotificationTemplate)
                .where(*filters)
                .order_by(
                    col(NotificationTemplate.code),
                    col(NotificationTemplate.channel),
                    col(NotificationTemplate.language),
                )
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def _find_template(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    code: str,
    channel: NotificationChannel,
    language: str,
    active_only: bool = False,
) -> NotificationTemplate | None:
    filters: list[ColumnElement[bool]] = [
        col(NotificationTemplate.hospital_id) == hospital_id,
        col(NotificationTemplate.code) == code.strip().upper(),
        col(NotificationTemplate.channel) == channel,
        col(NotificationTemplate.language) == language.strip().lower(),
        col(NotificationTemplate.deleted_at).is_(None),
    ]
    if active_only:
        filters.append(col(NotificationTemplate.is_active).is_(True))
    return (await session.execute(select(NotificationTemplate).where(*filters))).scalars().first()


@dataclass(frozen=True, kw_only=True, slots=True)
class ResolvedTemplate:
    """Copy for one message, from whichever of the two tiers supplied it.

    A value object rather than the ORM row, because a hospital override and the
    shipped default have to look identical to the renderer. `source` is carried
    so the outbox can answer "is this hospital using our wording or its own?",
    which is the first question asked when a message reads oddly.
    """

    subject: str | None
    body: str
    category: NotificationCategory
    language: str
    source: str  # "hospital" | "default"


async def resolve_template(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    code: str,
    channel: NotificationChannel,
    language: str | None,
) -> ResolvedTemplate | None:
    """Find the best copy for a message. Two tiers, each with a language fallback.

    A hospital's own `notification_templates` row wins; failing that, the copy
    shipped in `templates.DEFAULT_TEMPLATES`. That ordering is what lets a tenant
    onboarded next year send working English and Hindi messages on day one while
    still being free to rewrite every word.

    Returns `None` only when neither tier has the code at all — an ad-hoc message
    under a made-up code. The caller then renders `templates.fallback_body` and
    flags `needs_template`, on the same contract as `billing.capture_charge`
    capturing at zero: a configuration gap degrades a message, it never blocks
    the hospital.
    """
    for candidate in tpl.language_preference(language):
        row = await _find_template(
            session,
            hospital_id=hospital_id,
            code=code,
            channel=channel,
            language=candidate,
            active_only=True,
        )
        if row is not None:
            return ResolvedTemplate(
                subject=row.subject,
                body=row.body,
                category=row.category,
                language=row.language,
                source="hospital",
            )

    shipped = tpl.default_template(code, language)
    if shipped is not None:
        found_language, template = shipped
        return ResolvedTemplate(
            subject=tpl.subject_for(channel, template),
            body=template.body,
            category=template.category,
            language=found_language,
            source="default",
        )
    return None


# ---------------------------------------------------------------------------
# Suppression — the CLAUDE.md §14 invariant
# ---------------------------------------------------------------------------
async def active_suppressions(
    session: AsyncSession, *, hospital_id: uuid.UUID, patient_id: uuid.UUID
) -> list[NotificationSuppression]:
    rows = (
        (
            await session.execute(
                select(NotificationSuppression).where(
                    col(NotificationSuppression.hospital_id) == hospital_id,
                    col(NotificationSuppression.patient_id) == patient_id,
                    col(NotificationSuppression.deleted_at).is_(None),
                    col(NotificationSuppression.lifted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _matches(
    suppression: NotificationSuppression,
    *,
    channel: NotificationChannel | None,
    category: NotificationCategory,
) -> bool:
    """Whether a standing block covers this particular message.

    NULL on the row means "all". A block with no channel set covers every
    channel, which is what a patient means when they say "stop texting me" and
    then also stop wanting WhatsApp.
    """
    if suppression.channel is not None and suppression.channel is not channel:
        return False
    return not (suppression.category is not None and suppression.category is not category)


async def _blocking_reason(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    recipient_type: RecipientType,
    patient_id: uuid.UUID | None,
    category: NotificationCategory,
    channel: NotificationChannel | None = None,
) -> SuppressionReason | None:
    """The reason this message must not go out, or None.

    Staff-directed messages are never suppressed by a patient-level block, and
    that is the point rather than an oversight — see the module docstring. A
    critical potassium result reaches the doctor whatever the patient has opted
    out of, and the settlement notice for a deceased patient reaches the billing
    desk.
    """
    if recipient_type is not RecipientType.PATIENT or patient_id is None:
        return None

    # Layer one: the death flag, read from the patient record itself. Denormalised
    # onto `patients` in Phase 4 precisely so this question is answerable without
    # a join into encounters that a dispatcher might skip.
    patient = await patients_service.get_patient(session, patient_id)
    if patient.is_deceased:
        return SuppressionReason.DECEASED

    for suppression in await active_suppressions(
        session, hospital_id=hospital_id, patient_id=patient_id
    ):
        if suppression.reason is SuppressionReason.DECEASED:
            return SuppressionReason.DECEASED
        if _matches(suppression, channel=channel, category=category):
            return suppression.reason
    return None


async def suppress(
    session: AsyncSession,
    payload: SuppressionCreate,
    *,
    hospital_id: uuid.UUID,
    actor: User | None = None,
) -> NotificationSuppression:
    """Record a standing block — an opt-out, a dead number, a staff decision.

    Deliberately cannot record a death: that path is `suppress_for_death`, called
    from inside the transaction that records the death. Two ways to create the
    same row would mean two places for the invariant to be got wrong.
    """
    if payload.reason in (SuppressionReason.DECEASED, SuppressionReason.DISABLED):
        raise ValidationError(
            "That reason is not recorded by hand.",
            code="suppression_reason_not_manual",
            details={"reason": payload.reason.value},
        )

    existing = await _find_suppression(
        session,
        hospital_id=hospital_id,
        patient_id=payload.patient_id,
        reason=payload.reason,
        channel=payload.channel,
        category=payload.category,
    )
    if existing is not None:
        return existing

    # Confirms the patient exists and belongs to this tenant before writing.
    await patients_service.get_patient(session, payload.patient_id)

    suppression = NotificationSuppression(
        hospital_id=hospital_id,
        patient_id=payload.patient_id,
        reason=payload.reason,
        channel=payload.channel,
        category=payload.category,
        note=payload.note,
        recorded_by_id=actor.id if actor else None,
    )
    session.add(suppression)
    await session.flush()
    await session.refresh(suppression)
    return suppression


async def suppress_for_death(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID,
    note: str | None = None,
) -> NotificationSuppression:
    """Block every patient-directed message, permanently. CLAUDE.md §14.

    Called from `handlers.suppress_on_death`, which subscribes on the
    **transactional** channel. That choice is deliberate and is the one place
    this module departs from fire-and-forget: this handler writes a row to the
    same database in the same transaction as the death record, so it has no
    independent failure domain, and `core/events.py` is explicit that swallowing
    an exception there buys nothing and loses the row. The row it would lose is
    the enforcement of the invariant itself.

    Idempotent: recording the same death twice returns the existing block.
    """
    existing = await _find_suppression(
        session,
        hospital_id=hospital_id,
        patient_id=patient_id,
        reason=SuppressionReason.DECEASED,
        channel=None,
        category=None,
    )
    if existing is not None:
        return existing

    suppression = NotificationSuppression(
        hospital_id=hospital_id,
        patient_id=patient_id,
        reason=SuppressionReason.DECEASED,
        channel=None,
        category=None,
        note=note or "Death recorded. No automated message is ever sent to this patient.",
    )
    session.add(suppression)
    await session.flush()
    logger.info("messaging permanently suppressed for deceased patient %s", patient_id)
    return suppression


async def _find_suppression(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID,
    reason: SuppressionReason,
    channel: NotificationChannel | None,
    category: NotificationCategory | None,
) -> NotificationSuppression | None:
    filters: list[ColumnElement[bool]] = [
        col(NotificationSuppression.hospital_id) == hospital_id,
        col(NotificationSuppression.patient_id) == patient_id,
        col(NotificationSuppression.reason) == reason,
        col(NotificationSuppression.deleted_at).is_(None),
        col(NotificationSuppression.lifted_at).is_(None),
    ]
    filters.append(
        col(NotificationSuppression.channel).is_(None)
        if channel is None
        else col(NotificationSuppression.channel) == channel
    )
    filters.append(
        col(NotificationSuppression.category).is_(None)
        if category is None
        else col(NotificationSuppression.category) == category
    )
    return (
        (await session.execute(select(NotificationSuppression).where(*filters))).scalars().first()
    )


async def get_suppression(
    session: AsyncSession, suppression_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> NotificationSuppression:
    row = (
        (
            await session.execute(
                select(NotificationSuppression).where(
                    col(NotificationSuppression.id) == suppression_id,
                    col(NotificationSuppression.hospital_id) == hospital_id,
                    col(NotificationSuppression.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise NotFoundError("Suppression not found.", code="suppression_not_found")
    return row


async def lift_suppression(
    session: AsyncSession,
    suppression: NotificationSuppression,
    *,
    reason: str,
    actor: User | None = None,
) -> NotificationSuppression:
    """Release a block. Refuses a death, always.

    The refusal lives here rather than in RBAC on purpose. A permission can be
    granted to a role by a hospital administrator editing rows — RBAC is
    data-driven (CLAUDE.md §8), which is a feature everywhere except here. There
    is no permission that lifts a death because there is no code path that does.
    """
    if suppression.is_permanent:
        raise ConflictError(
            "A death suppression cannot be lifted. If the death was recorded in "
            "error, correct the clinical record.",
            code="suppression_permanent",
            details={"reason": suppression.reason.value},
        )
    if suppression.lifted_at is not None:
        raise ConflictError("This block has already been lifted.", code="suppression_lifted")

    suppression.lifted_at = utc_now()
    suppression.lifted_reason = reason
    suppression.lifted_by_id = actor.id if actor else None
    session.add(suppression)
    await session.flush()
    await session.refresh(suppression)
    return suppression


async def list_suppressions(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    reason: SuppressionReason | None = None,
    include_lifted: bool = False,
) -> tuple[list[NotificationSuppression], int]:
    filters: list[ColumnElement[bool]] = [
        col(NotificationSuppression.hospital_id) == hospital_id,
        col(NotificationSuppression.deleted_at).is_(None),
    ]
    if patient_id is not None:
        filters.append(col(NotificationSuppression.patient_id) == patient_id)
    if reason is not None:
        filters.append(col(NotificationSuppression.reason) == reason)
    if not include_lifted:
        filters.append(col(NotificationSuppression.lifted_at).is_(None))

    total = (
        await session.execute(
            select(func.count()).select_from(NotificationSuppression).where(*filters)
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(NotificationSuppression)
                .where(*filters)
                .order_by(col(NotificationSuppression.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


# ---------------------------------------------------------------------------
# The channel ladder
# ---------------------------------------------------------------------------
def channel_ladder(
    recipient: Recipient,
    *,
    gateway: Gateway,
    restrict: NotificationChannel | None = None,
) -> list[NotificationChannel]:
    """WhatsApp → SMS → email, minus the rungs that cannot work.

    Skipped rather than attempted-and-failed when the recipient has no address
    for a channel or the configured provider cannot serve it. That distinction
    matters on the screen: a hospital reading its failure log should see real
    provider problems, not a wall of "no email address" against patients who
    were never going to have one.
    """
    order = [
        NotificationChannel(name)
        for name in settings.NOTIFICATION_CHANNEL_PRIORITY
        if name in NotificationChannel.__members__
    ]
    if restrict is not None:
        order = [channel for channel in order if channel is restrict]
    return [
        channel for channel in order if recipient.address_for(channel) and gateway.supports(channel)
    ]


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------
async def enqueue(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    code: str,
    recipient: Recipient,
    context: dict[str, object] | None = None,
    category: NotificationCategory = NotificationCategory.ADMINISTRATIVE,
    encounter_id: uuid.UUID | None = None,
    source_module: str | None = None,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    restrict_channel: NotificationChannel | None = None,
    body_override: str | None = None,
    subject_override: str | None = None,
    triggered_by: User | None = None,
    gateway: Gateway | None = None,
) -> Notification:
    """Create the message record. Renders it; does not send it.

    Total with respect to configuration, on the same contract as
    `billing.capture_charge`: no template, no translation, no placeholder value —
    none of these raise. The message degrades to fallback copy and flags itself.

    Idempotent when `source_id` is given: a re-delivered event returns the
    existing row rather than messaging the patient twice about one report.
    """
    payload = dict(context or {})

    if source_id is not None:
        existing = await _find_by_source(
            session,
            hospital_id=hospital_id,
            source_module=source_module,
            source_type=source_type,
            source_id=source_id,
            code=code,
        )
        if existing is not None:
            logger.debug("notification already raised for %s/%s", source_type, source_id)
            return existing

    notification = Notification(
        hospital_id=hospital_id,
        template_code=code.strip().upper(),
        category=category,
        language=recipient.language,
        recipient_type=recipient.recipient_type,
        patient_id=recipient.patient_id,
        user_id=recipient.user_id,
        encounter_id=encounter_id,
        recipient_name=recipient.name,
        recipient_phone=recipient.phone,
        recipient_email=recipient.email,
        context=payload,
        source_module=source_module,
        source_type=source_type,
        source_id=source_id,
        restricted_channel=restrict_channel,
        triggered_by_id=triggered_by.id if triggered_by else None,
    )

    # Render against the first channel we would actually try, so the body that
    # is stored is the body that would go out. If the ladder later falls through
    # to another channel, `dispatch` re-renders for that channel — an SMS and an
    # email are not the same text and storing one while sending the other would
    # make the record a lie.
    ladder = channel_ladder(recipient, gateway=gateway or get_gateway(), restrict=restrict_channel)
    preview_channel = ladder[0] if ladder else NotificationChannel.SMS

    if body_override:
        # Staff wrote the words themselves; there is no template to be missing,
        # and dispatch must not re-render over the top of them.
        rendered = tpl.render(body_override, payload, subject=subject_override)
        notification.body_is_custom = True
    else:
        rendered, template = await _render_for(
            session,
            hospital_id=hospital_id,
            code=code,
            channel=preview_channel,
            language=recipient.language,
            context=payload,
        )
        notification.needs_template = template is None
        if template is not None:
            # The template's own category wins over the caller's guess: whoever
            # wrote the copy knows better than the trigger what kind of message
            # it is, and the category is what a patient's opt-out is matched on.
            notification.category = template.category

    notification.subject = rendered.subject
    notification.body = rendered.body

    # --- suppression, layer one -------------------------------------------
    reason = (
        SuppressionReason.DISABLED
        if not settings.NOTIFICATIONS_ENABLED
        else await _blocking_reason(
            session,
            hospital_id=hospital_id,
            recipient_type=recipient.recipient_type,
            patient_id=recipient.patient_id,
            category=notification.category,
            channel=restrict_channel,
        )
    )
    if reason is not None:
        notification.status = NotificationStatus.SUPPRESSED
        notification.suppression_reason = reason

    if not ladder and notification.status is NotificationStatus.PENDING:
        # Nowhere to send it. FAILED rather than SUPPRESSED: nobody chose this,
        # and it is a data-quality problem reception can actually fix.
        notification.status = NotificationStatus.FAILED
        notification.failed_at = utc_now()
        notification.last_error = "No usable contact details for this recipient."

    session.add(notification)
    await session.flush()
    await session.refresh(notification)

    if notification.status is NotificationStatus.SUPPRESSED and reason is not None:
        logger.info(
            "notification %s suppressed reason=%s code=%s",
            notification.id,
            reason.value,
            notification.template_code,
        )
        await event_bus.publish(
            NotificationSuppressed(
                hospital_id=hospital_id,
                notification_id=notification.id,
                patient_id=notification.patient_id,
                template_code=notification.template_code,
                reason=reason.value,
            ),
            session=session,
        )
    elif notification.needs_template:
        await event_bus.publish(
            NotificationTemplateMissing(
                hospital_id=hospital_id,
                notification_id=notification.id,
                template_code=notification.template_code,
                language=notification.language,
                channel=preview_channel.value,
            ),
            session=session,
        )

    return notification


async def _render_for(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    code: str,
    channel: NotificationChannel,
    language: str,
    context: dict[str, object],
) -> tuple[tpl.RenderResult, ResolvedTemplate | None]:
    """Render `code` for one channel. Returns the text and the copy it came from.

    A `None` template means neither tier had the code and the generic fallback
    line was used — which is what the caller records as `needs_template`.
    """
    template = await resolve_template(
        session, hospital_id=hospital_id, code=code, channel=channel, language=language
    )
    if template is None:
        rendered = tpl.render(tpl.fallback_body(code), context, used_fallback=True)
        logger.warning(
            "no %s copy for %s in %s (not even a shipped default); using generic fallback",
            channel.value,
            code,
            language,
        )
        return rendered, None

    rendered = tpl.render(template.body, context, subject=template.subject)
    if rendered.missing:
        logger.warning(
            "template %s/%s/%s (%s) has unfilled placeholders: %s",
            code,
            channel.value,
            template.language,
            template.source,
            ", ".join(rendered.missing),
        )
    return rendered, template


async def _find_by_source(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    source_module: str | None,
    source_type: str | None,
    source_id: uuid.UUID,
    code: str,
) -> Notification | None:
    return (
        (
            await session.execute(
                select(Notification).where(
                    col(Notification.hospital_id) == hospital_id,
                    col(Notification.source_module) == source_module,
                    col(Notification.source_type) == source_type,
                    col(Notification.source_id) == source_id,
                    col(Notification.template_code) == code.strip().upper(),
                    col(Notification.deleted_at).is_(None),
                    col(Notification.status).not_in(
                        [
                            NotificationStatus.CANCELLED,
                            NotificationStatus.SUPPRESSED,
                            NotificationStatus.FAILED,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
async def dispatch(
    session: AsyncSession, notification: Notification, *, gateway: Gateway | None = None
) -> Notification:
    """Walk the ladder until something accepts the message.

    Re-checks suppression first — **layer two** of the CLAUDE.md §14 invariant.
    Under inline sending that check runs milliseconds after `enqueue`'s and looks
    redundant; under `retry_failed` it runs days later, and that is the case it
    exists for. A follow-up reminder queued on Monday must not go out on Friday
    to a patient who died on Wednesday.
    """
    if notification.is_terminal:
        return notification

    reason = await _blocking_reason(
        session,
        hospital_id=notification.hospital_id,
        recipient_type=notification.recipient_type,
        patient_id=notification.patient_id,
        category=notification.category,
        channel=notification.channel,
    )
    if reason is not None:
        return await _mark_suppressed(session, notification, reason)

    if not settings.NOTIFICATIONS_ENABLED:
        return await _mark_suppressed(session, notification, SuppressionReason.DISABLED)

    active_gateway = gateway or get_gateway()
    recipient = Recipient(
        recipient_type=notification.recipient_type,
        name=notification.recipient_name,
        phone=notification.recipient_phone,
        email=notification.recipient_email,
        language=notification.language,
        patient_id=notification.patient_id,
        user_id=notification.user_id,
    )
    ladder = channel_ladder(
        recipient, gateway=active_gateway, restrict=notification.restricted_channel
    )
    if not ladder:
        return await _mark_failed(
            session, notification, error="No usable contact details for this recipient."
        )

    notification.dispatch_rounds += 1
    tried: list[str] = []
    last_error: str | None = None

    for channel in ladder:
        address = recipient.address_for(channel)
        if address is None:  # pragma: no cover - channel_ladder already filtered
            continue

        # Re-render per channel: an SMS and an email are not the same text, and
        # the row must record what was actually sent on the channel that sent it.
        body, subject = notification.body, notification.subject
        if not notification.needs_template and not notification.body_is_custom:
            rendered, template = await _render_for(
                session,
                hospital_id=notification.hospital_id,
                code=notification.template_code,
                channel=channel,
                language=notification.language,
                context=dict(notification.context or {}),
            )
            if template is not None:
                body, subject = rendered.body, rendered.subject

        notification.attempts += 1
        notification.channel = channel
        tried.append(channel.value)

        result = await _attempt(
            session,
            notification,
            gateway=active_gateway,
            channel=channel,
            address=address,
            body=body,
            subject=subject,
        )

        if result:
            notification.status = NotificationStatus.SENT
            notification.sent_at = utc_now()
            notification.body = body
            notification.subject = subject
            notification.last_error = None
            session.add(notification)
            await session.flush()
            await event_bus.publish(
                NotificationSent(
                    hospital_id=notification.hospital_id,
                    notification_id=notification.id,
                    patient_id=notification.patient_id,
                    template_code=notification.template_code,
                    channel=channel.value,
                    attempts=notification.attempts,
                ),
                session=session,
            )
            return notification

        last_error = notification.last_error

    return await _mark_failed(session, notification, error=last_error, tried=tuple(tried))


async def _attempt(
    session: AsyncSession,
    notification: Notification,
    *,
    gateway: Gateway,
    channel: NotificationChannel,
    address: str,
    body: str,
    subject: str | None,
) -> bool:
    """One rung. Records the attempt whatever happens, and never raises.

    A gateway is contracted to *return* failure rather than raise it, but a
    third-party SDK will eventually raise anyway — a socket timeout, a JSON
    decode error on an HTML error page. Catching here keeps one broken provider
    from ending the walk before the next channel is tried, which is the entire
    reason there is a ladder.
    """
    message = OutboundMessage(
        notification_id=notification.id,
        channel=channel,
        address=address,
        body=body,
        subject=subject,
        language=notification.language,
        template_code=notification.template_code,
    )

    try:
        result = await gateway.send(message)
    except Exception as exc:
        logger.exception("gateway %s raised on %s", gateway.name, channel.value)
        # Normalised into the same shape a well-behaved adapter returns, so the
        # attempt record does not distinguish "the provider said no" from "the
        # provider's client library fell over". Both are a rung that did not work.
        result = GatewayResult.failure(
            gateway.name, code="gateway_exception", detail=f"{type(exc).__name__}: {exc}"
        )

    session.add(
        NotificationAttempt(
            hospital_id=notification.hospital_id,
            notification_id=notification.id,
            attempt=notification.attempts,
            channel=channel,
            address=address[:255],
            succeeded=result.succeeded,
            gateway=result.gateway,
            provider_message_id=result.provider_message_id,
            error_code=result.error_code,
            error_detail=result.error_detail,
            latency_ms=result.latency_ms,
        )
    )
    if not result.succeeded:
        notification.last_error = (result.error_detail or result.error_code or "Delivery failed.")[
            :500
        ]
    await session.flush()
    return result.succeeded


async def _mark_suppressed(
    session: AsyncSession, notification: Notification, reason: SuppressionReason
) -> Notification:
    notification.status = NotificationStatus.SUPPRESSED
    notification.suppression_reason = reason
    session.add(notification)
    await session.flush()
    logger.info("notification %s suppressed at dispatch reason=%s", notification.id, reason.value)
    await event_bus.publish(
        NotificationSuppressed(
            hospital_id=notification.hospital_id,
            notification_id=notification.id,
            patient_id=notification.patient_id,
            template_code=notification.template_code,
            reason=reason.value,
        ),
        session=session,
    )
    return notification


async def _mark_failed(
    session: AsyncSession,
    notification: Notification,
    *,
    error: str | None,
    tried: tuple[str, ...] = (),
) -> Notification:
    notification.status = NotificationStatus.FAILED
    notification.failed_at = utc_now()
    if error:
        notification.last_error = error[:500]
    session.add(notification)
    await session.flush()
    await event_bus.publish(
        NotificationFailed(
            hospital_id=notification.hospital_id,
            notification_id=notification.id,
            patient_id=notification.patient_id,
            template_code=notification.template_code,
            channels_tried=tried,
            last_error=notification.last_error,
        ),
        session=session,
    )
    return notification


async def notify(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    code: str,
    recipient: Recipient,
    context: dict[str, object] | None = None,
    category: NotificationCategory = NotificationCategory.ADMINISTRATIVE,
    encounter_id: uuid.UUID | None = None,
    source_module: str | None = None,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    restrict_channel: NotificationChannel | None = None,
    body_override: str | None = None,
    subject_override: str | None = None,
    triggered_by: User | None = None,
    gateway: Gateway | None = None,
) -> Notification:
    """Enqueue and send, in one call. The entry point every trigger uses.

    Split into two functions underneath rather than one, so that moving to a
    worker-dispatched outbox later is a change to this function alone.
    """
    notification = await enqueue(
        session,
        hospital_id=hospital_id,
        code=code,
        recipient=recipient,
        context=context,
        category=category,
        encounter_id=encounter_id,
        source_module=source_module,
        source_type=source_type,
        source_id=source_id,
        restrict_channel=restrict_channel,
        body_override=body_override,
        subject_override=subject_override,
        triggered_by=triggered_by,
        gateway=gateway,
    )
    if notification.status is not NotificationStatus.PENDING:
        return notification
    return await dispatch(session, notification, gateway=gateway)


async def send_adhoc(
    session: AsyncSession,
    payload: AdhocSend,
    *,
    hospital_id: uuid.UUID,
    actor: User | None = None,
) -> Notification:
    """A message composed by staff at the counter.

    Goes through the same dispatcher and therefore the same suppression checks.
    There is no path in this module that sends a message without them — which is
    what makes the invariant a property of the system rather than of each caller.
    """
    if payload.recipient_type is RecipientType.PATIENT:
        if payload.patient_id is None:
            raise ValidationError("A patient message needs a patient_id.")
        recipient = await _patient_recipient(session, payload.patient_id)
    else:
        from app.modules.identity import service as identity_service

        if payload.user_id is None:
            raise ValidationError("A staff message needs a user_id.")
        user = await identity_service.get_user(session, payload.user_id)
        if user.hospital_id is not None and user.hospital_id != hospital_id:
            raise NotFoundError("User not found.", code="user_not_found")
        recipient = _staff_recipient(user)

    return await notify(
        session,
        hospital_id=hospital_id,
        code=payload.template_code or "ADHOC",
        recipient=recipient,
        context=payload.context,
        category=payload.category,
        encounter_id=payload.encounter_id,
        source_module="notifications",
        source_type="adhoc",
        restrict_channel=payload.channel,
        body_override=payload.body,
        subject_override=payload.subject,
        triggered_by=actor,
    )


# ---------------------------------------------------------------------------
# Retry, cancel, and the sweep
# ---------------------------------------------------------------------------
async def retry(
    session: AsyncSession, notification: Notification, *, actor: User | None = None
) -> Notification:
    """Give a failed message another walk down the ladder.

    Refuses once `NOTIFICATION_MAX_ATTEMPTS` is spent. Retrying a number that
    does not exist forever costs money and burns provider reputation, and the
    honest answer after three failures is that somebody has to pick up a phone.
    """
    if notification.status is not NotificationStatus.FAILED:
        raise ConflictError(
            "Only a failed message can be retried; this one is "
            f"{notification.status.value.lower()}.",
            code="notification_not_failed",
        )
    if notification.dispatch_rounds >= settings.NOTIFICATION_MAX_ATTEMPTS:
        raise ConflictError(
            f"This message has already been tried {notification.dispatch_rounds} times "
            f"across every available channel. Contact the patient directly.",
            code="notification_attempts_exhausted",
            details={
                "dispatch_rounds": notification.dispatch_rounds,
                "attempts": notification.attempts,
            },
        )

    notification.status = NotificationStatus.PENDING
    notification.failed_at = None
    if actor is not None:
        notification.triggered_by_id = actor.id
    session.add(notification)
    await session.flush()
    return await dispatch(session, notification)


async def cancel(
    session: AsyncSession,
    notification: Notification,
    *,
    reason: str,
    actor: User | None = None,
) -> Notification:
    """Withdraw a message that has not gone out."""
    if notification.status in (NotificationStatus.SENT, NotificationStatus.DELIVERED):
        raise ConflictError(
            "This message has already been sent and cannot be recalled.",
            code="notification_already_sent",
        )
    if notification.status is NotificationStatus.CANCELLED:
        raise ConflictError("This message is already cancelled.", code="notification_cancelled")

    notification.status = NotificationStatus.CANCELLED
    notification.cancellation_reason = reason[:255]
    if actor is not None:
        notification.triggered_by_id = actor.id
    session.add(notification)
    await session.flush()
    await session.refresh(notification)
    return notification


async def cancel_pending_for_patient(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID,
    reason: str,
) -> int:
    """Withdraw everything queued for one patient. Returns how many.

    Called when a death is recorded. Suppression alone would stop these at
    dispatch — that is layer two doing its job — but leaving a queue of pending
    "book your follow-up" messages against a deceased patient is a trap waiting
    for the next person who writes a bulk-send script. Clearing them means the
    dangerous rows do not exist rather than merely not being read.

    Only patient-directed messages. Staff-directed ones about the same patient —
    the settlement notice CLAUDE.md §6 requires — are left alone.
    """
    rows = (
        (
            await session.execute(
                select(Notification).where(
                    col(Notification.hospital_id) == hospital_id,
                    col(Notification.patient_id) == patient_id,
                    col(Notification.recipient_type) == RecipientType.PATIENT,
                    col(Notification.status) == NotificationStatus.PENDING,
                    col(Notification.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = utc_now()
    for row in rows:
        row.status = NotificationStatus.CANCELLED
        row.cancellation_reason = reason[:255]
        row.suppression_reason = SuppressionReason.DECEASED
        row.updated_at = now
        session.add(row)
    if rows:
        await session.flush()
        logger.info("cancelled %d queued message(s) for patient %s", len(rows), patient_id)
    return len(rows)


async def retry_failed(
    session: AsyncSession, *, hospital_id: uuid.UUID, older_than: timedelta | None = None
) -> dict[str, int]:
    """The retry sweep, run per tenant by the worker.

    Not the transactional-outbox pattern: these rows already exist and already
    failed, so this is resilience on an existing record rather than a second
    delivery mechanism. Messages past `NOTIFICATION_MAX_ATTEMPTS` are left alone
    for a human.

    Each message is dispatched in its own flush and its own error boundary — one
    patient with a wedged row must not stop the rest of the hospital's messages
    from ever being retried, which is the same reasoning `workers.tasks.run_sweeps`
    applies per tenant.
    """
    if not settings.NOTIFICATION_RETRY_ENABLED:
        return {"notifications_retried": 0, "notifications_sent": 0}

    cutoff: datetime | None = utc_now() - older_than if older_than else None
    filters: list[ColumnElement[bool]] = [
        col(Notification.hospital_id) == hospital_id,
        col(Notification.status) == NotificationStatus.FAILED,
        col(Notification.dispatch_rounds) < settings.NOTIFICATION_MAX_ATTEMPTS,
        col(Notification.deleted_at).is_(None),
    ]
    if cutoff is not None:
        filters.append(col(Notification.failed_at) <= cutoff)

    rows = (
        (
            await session.execute(
                select(Notification)
                .where(*filters)
                .order_by(col(Notification.failed_at))
                .limit(settings.NOTIFICATION_RETRY_BATCH_SIZE)
                # Two workers must not walk the same ladder at once and send the
                # patient two copies. SKIP LOCKED lets a second worker take the
                # next row rather than block behind this one.
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    retried = sent = 0
    for row in rows:
        row.status = NotificationStatus.PENDING
        row.failed_at = None
        session.add(row)
        await session.flush()
        retried += 1
        try:
            result = await dispatch(session, row)
        except Exception:
            logger.exception("retry failed for notification %s", row.id)
            continue
        if result.status is NotificationStatus.SENT:
            sent += 1

    return {"notifications_retried": retried, "notifications_sent": sent}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def get_notification(
    session: AsyncSession, notification_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Notification:
    notification = (
        (
            await session.execute(
                select(Notification).where(
                    col(Notification.id) == notification_id,
                    col(Notification.hospital_id) == hospital_id,
                    col(Notification.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if notification is None:
        raise NotFoundError("Notification not found.", code="notification_not_found")
    return notification


async def list_notifications(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    encounter_id: uuid.UUID | None = None,
    status: NotificationStatus | None = None,
    category: NotificationCategory | None = None,
    channel: NotificationChannel | None = None,
    needs_template: bool | None = None,
) -> tuple[list[Notification], int]:
    """The outbox, and the missing-template worklist."""
    filters: list[ColumnElement[bool]] = [
        col(Notification.hospital_id) == hospital_id,
        col(Notification.deleted_at).is_(None),
    ]
    if patient_id is not None:
        filters.append(col(Notification.patient_id) == patient_id)
    if encounter_id is not None:
        filters.append(col(Notification.encounter_id) == encounter_id)
    if status is not None:
        filters.append(col(Notification.status) == status)
    if category is not None:
        filters.append(col(Notification.category) == category)
    if channel is not None:
        filters.append(col(Notification.channel) == channel)
    if needs_template is not None:
        filters.append(col(Notification.needs_template).is_(needs_template))

    total = (
        await session.execute(select(func.count()).select_from(Notification).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(*filters)
                .order_by(col(Notification.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def list_attempts(
    session: AsyncSession, notification_id: uuid.UUID
) -> list[NotificationAttempt]:
    """Every rung tried, in order. The answer to "did you actually message me?"."""
    rows = (
        (
            await session.execute(
                select(NotificationAttempt)
                .where(col(NotificationAttempt.notification_id) == notification_id)
                .order_by(col(NotificationAttempt.attempt), col(NotificationAttempt.created_at))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# Re-exported so callers do not import the models module for one enum check.
TERMINAL = TERMINAL_STATUSES
