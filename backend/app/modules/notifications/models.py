"""Templates, the message record, its delivery attempts, and the block list.

CLAUDE.md §13 step 8 asks for a channel-agnostic dispatcher (WhatsApp → SMS →
email), templates, triggers wired to domain events, and a hard suppression rule
for deceased patients. Four tables carry that:

* `notification_templates` — what a message says, per (code, channel, language).
* `notifications` — one row per *message intent*. Rendered text is frozen onto
  it, so what the hospital can prove it sent does not change when somebody edits
  a template next month.
* `notification_attempts` — one row per channel actually tried. The ladder is
  only auditable if each rung is a record: "WhatsApp refused at 14:31, SMS
  accepted at 14:31" is a different fact from "an SMS went out".
* `notification_suppressions` — who must not be messaged, and why.

The last table is the one that matters most. CLAUDE.md §14 names *never send a
notification to a deceased patient* as an invariant to actively guard, and an
invariant enforced only by an `if` in one function is an invariant one refactor
away from being gone. So it is enforced three times over, at three different
altitudes:

1. **At enqueue** — `service.enqueue` refuses and records `SUPPRESSED`.
2. **At send** — re-checked in the dispatcher, because a follow-up reminder
   queued on Monday and sent on Friday spans a death that happened Wednesday.
   This is the layer people forget, and it is the one the real case needs.
3. **In the database** — a trigger on `notification_attempts` refuses the INSERT
   outright for a deceased patient (see the RLS/permissions migration). Same
   philosophy as row-level security in CLAUDE.md §3: the application is expected
   to get it right, and the database makes it impossible to get wrong.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Index, text
from sqlmodel import Field

from app.core.models import AuditedTenantModel, TenantModel

__all__ = [
    "Notification",
    "NotificationAttempt",
    "NotificationCategory",
    "NotificationChannel",
    "NotificationStatus",
    "NotificationSuppression",
    "NotificationTemplate",
    "RecipientType",
    "SuppressionReason",
]


class NotificationChannel(enum.StrEnum):
    """The three ways a message leaves the building (CLAUDE.md §9).

    Ordered by preference in configuration, not here — a hospital whose patients
    are not on WhatsApp should be able to reorder the ladder without a deploy.
    """

    WHATSAPP = "WHATSAPP"
    SMS = "SMS"
    EMAIL = "EMAIL"


class NotificationCategory(enum.StrEnum):
    """What the message is about.

    Carried so a patient can opt out of billing reminders without also opting
    out of "your biopsy result is ready" — a single global unsubscribe is the
    wrong granularity for a hospital.
    """

    APPOINTMENT = "APPOINTMENT"
    REPORT = "REPORT"
    BILLING = "BILLING"
    FOLLOW_UP = "FOLLOW_UP"
    # Staff-directed and urgent: a panic value, an unsigned report. Never
    # suppressible by a patient preference, because it is not addressed to them.
    CRITICAL_ALERT = "CRITICAL_ALERT"
    ADMINISTRATIVE = "ADMINISTRATIVE"


class RecipientType(enum.StrEnum):
    """Who the message is addressed to — not who it is *about*.

    The distinction carries the whole deceased rule. A message **about** a
    deceased patient addressed to **staff** ("this bill still needs settling
    with the family") is legitimate and necessary; CLAUDE.md §6 requires the
    settlement flow to run. The same fact addressed to the patient's own mobile
    is the thing that must never happen. Suppression keys on
    `recipient_type = PATIENT`, so both stay true at once.
    """

    PATIENT = "PATIENT"
    STAFF = "STAFF"


class NotificationStatus(enum.StrEnum):
    PENDING = "PENDING"
    # A gateway accepted it. Not the same as the patient having read it.
    SENT = "SENT"
    # A provider confirmed handset delivery. Set by a webhook later; the column
    # exists now so adding one is not a migration against a live table.
    DELIVERED = "DELIVERED"
    # Every channel in the ladder refused, or attempts ran out.
    FAILED = "FAILED"
    # Deliberately not sent. Distinct from FAILED on purpose: "we chose not to"
    # and "we tried and could not" are answers to different questions, and an
    # auditor asks the first one.
    SUPPRESSED = "SUPPRESSED"
    # Withdrawn before it went out — by staff, or by a death.
    CANCELLED = "CANCELLED"


# Statuses from which nothing further is attempted.
TERMINAL_STATUSES: frozenset[NotificationStatus] = frozenset(
    {
        NotificationStatus.SENT,
        NotificationStatus.DELIVERED,
        NotificationStatus.SUPPRESSED,
        NotificationStatus.CANCELLED,
    }
)


class SuppressionReason(enum.StrEnum):
    # The hard one. Never liftable — see `service.lift_suppression`.
    DECEASED = "DECEASED"
    # The patient asked. DPDP Act 2023 §6 makes consent withdrawable.
    OPTED_OUT = "OPTED_OUT"
    # The number bounces. Suppressing beats burning provider reputation on a
    # number that was mistyped at registration two years ago.
    INVALID_CONTACT = "INVALID_CONTACT"
    # A judgement call by staff, with a note. Rare and deliberate.
    STAFF_BLOCK = "STAFF_BLOCK"
    # Messaging is switched off for the whole deployment
    # (`NOTIFICATIONS_ENABLED=false`). Only ever appears on a `Notification`,
    # never on a `NotificationSuppression` row — it is a fact about the
    # configuration, not about a patient, and both the schema validator and
    # `service.suppress` refuse it. It exists as a reason at all because a
    # message that was never sent has to say why: a hospital that switched
    # messaging off six months ago should be able to discover that is why
    # nobody is being reminded of anything, rather than seeing an empty outbox.
    DISABLED = "DISABLED"


class NotificationTemplate(AuditedTenantModel, table=True):
    """What a message says, for one (code, channel, language).

    Tenant-scoped: hospitals sign off their own patient-facing wording, and a
    shared master would make one hospital's edit everybody's problem — the same
    reasoning as the diagnostics catalogue.

    Three rows per event is normal, not duplication: WhatsApp allows 1,024
    characters and a template header, an SMS costs money per 160, and email has
    a subject line. The same sentence does not fit all three.
    """

    __tablename__ = "notification_templates"
    __table_args__ = (
        Index(
            "uq_notification_templates_code_live",
            "hospital_id",
            "code",
            "channel",
            "language",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_notification_templates_hospital_id_code", "hospital_id", "code"),
    )

    code: str = Field(
        max_length=64,
        index=True,
        description="Event key, e.g. 'REPORT_READY'. Imported from `templates.TemplateCode`.",
    )
    channel: NotificationChannel = Field(index=True)
    language: str = Field(
        default="en",
        max_length=8,
        index=True,
        description="ISO 639-1. Matched against the patient's preferred_language.",
    )

    category: NotificationCategory = Field(default=NotificationCategory.ADMINISTRATIVE, index=True)

    # Email only. NULL for WhatsApp and SMS, which have no subject.
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(description="Placeholders in {{double_braces}}. See `templates.render`.")

    description: str | None = Field(
        default=None, max_length=255, description="What this template is for, shown to admins."
    )
    is_active: bool = Field(default=True, index=True)


class Notification(AuditedTenantModel, table=True):
    """One message intent, and everything provable about it afterwards.

    The rendered `subject`/`body` are **frozen onto this row** rather than
    re-rendered on read, for the same reason a diagnostics result snapshots its
    reference range and an invoice freezes its lines: what was actually sent is
    a fact about the past, and a template edit next month must not rewrite it.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Idempotency, on the pattern `charges` established. A re-published
        # event, a double-click, or a retried request must not message the same
        # patient twice about the same report. Terminal-but-unsent statuses are
        # excluded so a cancelled or suppressed message can be legitimately
        # raised again later (a lifted opt-out, a re-issued report).
        Index(
            "uq_notifications_source_live",
            "hospital_id",
            "source_module",
            "source_type",
            "source_id",
            "template_code",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL AND source_id IS NOT NULL "
                "AND status NOT IN ('CANCELLED', 'SUPPRESSED', 'FAILED')"
            ),
        ),
        # The dispatch worklist and the retry sweep.
        Index("ix_notifications_hospital_id_status", "hospital_id", "status"),
        # "What have we sent this patient?" — the timeline view.
        Index("ix_notifications_patient_id_created_at", "patient_id", "created_at"),
    )

    # --- what ---------------------------------------------------------------
    template_code: str = Field(max_length=64, index=True)
    category: NotificationCategory = Field(default=NotificationCategory.ADMINISTRATIVE, index=True)
    language: str = Field(default="en", max_length=8)

    # --- who ----------------------------------------------------------------
    recipient_type: RecipientType = Field(default=RecipientType.PATIENT, index=True)
    # The patient the message is *about*. Set even for staff-directed messages,
    # because "show me everything sent about this patient" is a real question and
    # because suppression is keyed on it.
    patient_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="patients.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    # The staff member a STAFF-directed message is addressed to.
    user_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="users.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
    encounter_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="encounters.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )

    # Snapshotted at enqueue. A patient who changes their number next year has
    # not retrospectively changed where last month's message went.
    recipient_name: str | None = Field(default=None, max_length=200)
    recipient_phone: str | None = Field(default=None, max_length=20)
    recipient_email: str | None = Field(default=None, max_length=255)

    # --- the message itself -------------------------------------------------
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(default="")
    # The values the placeholders were filled from. Kept so a failed render can
    # be diagnosed, and a message re-rendered after a template is fixed, without
    # re-deriving state that has since moved on.
    context: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    # --- delivery -----------------------------------------------------------
    status: NotificationStatus = Field(default=NotificationStatus.PENDING, index=True)
    # The channel that succeeded, or the last one tried. NULL until an attempt.
    channel: NotificationChannel | None = Field(default=None, index=True)
    # "Send this one by SMS and nothing else." NULL means walk the full ladder.
    # Persisted rather than held only in the enqueue call, because dispatch
    # recomputes the ladder — including on a retry days later — and a restriction
    # that survives only until the first send is not a restriction.
    restricted_channel: NotificationChannel | None = Field(default=None)

    # Two counters, because they answer different questions and conflating them
    # is a bug: `attempts` counts individual rungs (and numbers the
    # `notification_attempts` rows), while `dispatch_rounds` counts complete
    # walks of the ladder. `NOTIFICATION_MAX_ATTEMPTS` governs the second — one
    # walk of a three-channel ladder already tries everything, so budgeting in
    # rungs would retire a message after a single provider outage.
    attempts: int = Field(default=0)
    dispatch_rounds: int = Field(default=0)

    sent_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    failed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    last_error: str | None = Field(default=None, max_length=500)

    # Set when status is SUPPRESSED or CANCELLED. Never inferred later: the
    # reason a message did not go out is the entire value of the record.
    suppression_reason: SuppressionReason | None = Field(default=None, index=True)
    cancellation_reason: str | None = Field(default=None, max_length=255)

    # --- configuration gaps -------------------------------------------------
    # True when no template existed and a bare fallback was used. The direct
    # analogue of `charges.needs_pricing`: a dispatcher must be total with
    # respect to configuration — a hospital that has not written its Hindi
    # WhatsApp copy must still be able to discharge a patient — so a missing
    # template degrades the message and raises a flag, it never raises an error.
    needs_template: bool = Field(default=False, index=True)

    # True when staff typed the words themselves rather than naming a template.
    # The dispatcher re-renders per channel as the ladder falls through — an SMS
    # and an email are not the same text — and this is what stops that
    # re-render from overwriting a message a receptionist wrote by hand.
    body_is_custom: bool = Field(default=False)

    # --- provenance ---------------------------------------------------------
    # Which module and which record caused this. Backs the idempotency index and
    # answers "why did this patient get this?" without guesswork.
    source_module: str | None = Field(default=None, max_length=32, index=True)
    source_type: str | None = Field(default=None, max_length=32)
    source_id: uuid.UUID | None = Field(default=None, index=True)

    triggered_by_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="users.id",
        ondelete="SET NULL",
        nullable=True,
        description="NULL when the trigger was an event rather than a person.",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class NotificationAttempt(TenantModel, table=True):
    """One rung of the channel ladder, tried once.

    A row per attempt rather than a counter, because the useful question after a
    complaint is "which channel did we actually reach them on, and when?" — and
    because a WhatsApp rejection followed by an SMS success is the ladder working
    correctly, not a failure to be hidden.

    Not soft-deletable: these are append-only operational evidence, and the
    parent notification's soft delete already retires the whole story.
    """

    __tablename__ = "notification_attempts"
    __table_args__ = (
        Index("ix_notification_attempts_notification_id_attempt", "notification_id", "attempt"),
    )

    notification_id: uuid.UUID = Field(
        foreign_key="notifications.id", ondelete="CASCADE", index=True
    )
    attempt: int = Field(default=1, description="1-based, across all channels.")
    channel: NotificationChannel = Field(index=True)

    # Snapshotted: the number or address this particular attempt went to.
    address: str = Field(max_length=255)

    succeeded: bool = Field(default=False, index=True)
    gateway: str = Field(max_length=32, description="Which adapter handled it, e.g. 'console'.")
    # The provider's own id, for reconciling against their dashboard when a
    # patient swears they never got it.
    provider_message_id: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, max_length=64)
    error_detail: str | None = Field(default=None, max_length=500)
    latency_ms: int | None = Field(default=None)


class NotificationSuppression(AuditedTenantModel, table=True):
    """A standing instruction not to message someone.

    `reason = DECEASED` is the enforcement of CLAUDE.md §14 and behaves unlike
    every other row in the system: it is written inside the transaction that
    records the death (so it cannot be lost if a handler fails), and it can never
    be lifted. `service.lift_suppression` refuses, by design and with a test on
    it. A hospital that needs to un-record a death fixes the death record, and
    the suppression follows from that fact rather than being edited around.

    `channel` and `category` are NULL for a total block. A patient who wants no
    billing SMS but still wants their reports gets a narrow row instead.
    """

    __tablename__ = "notification_suppressions"
    __table_args__ = (
        # One live suppression per (patient, reason, channel, category). Partial
        # so lifting one releases the slot for a future re-block.
        Index(
            "uq_notification_suppressions_live",
            "hospital_id",
            "patient_id",
            "reason",
            "channel",
            "category",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND lifted_at IS NULL"),
        ),
        # The dispatcher's hot read: "is this patient blocked at all?"
        Index("ix_notification_suppressions_patient_id_lifted_at", "patient_id", "lifted_at"),
    )

    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    reason: SuppressionReason = Field(index=True)

    # NULL means "every channel" / "every category".
    channel: NotificationChannel | None = Field(default=None)
    category: NotificationCategory | None = Field(default=None)

    note: str | None = Field(default=None, max_length=255)
    recorded_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )

    lifted_at: datetime | None = Field(
        default=None,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    lifted_reason: str | None = Field(default=None, max_length=255)
    lifted_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )

    @property
    def is_active(self) -> bool:
        return self.lifted_at is None and self.deleted_at is None

    @property
    def is_permanent(self) -> bool:
        """A death is not an opt-out. It is never lifted."""
        return self.reason is SuppressionReason.DECEASED
