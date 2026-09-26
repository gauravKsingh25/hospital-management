"""Request/response DTOs for `notifications`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.notifications.models import (
    NotificationCategory,
    NotificationChannel,
    NotificationStatus,
    RecipientType,
    SuppressionReason,
)

__all__ = [
    "AdhocSend",
    "AttemptRead",
    "MessageTemplateCreate",
    "MessageTemplateRead",
    "MessageTemplateUpdate",
    "NotificationCancelRequest",
    "NotificationRead",
    "NotificationSummary",
    "SuppressionCreate",
    "SuppressionLift",
    "SuppressionRead",
    "TemplatePreview",
    "TemplatePreviewResult",
]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
class MessageTemplateCreate(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    channel: NotificationChannel
    language: Annotated[str, Field(min_length=2, max_length=8)] = "en"
    category: NotificationCategory = NotificationCategory.ADMINISTRATIVE
    subject: Annotated[str | None, Field(max_length=200)] = None
    body: Annotated[str, Field(min_length=1, max_length=4000)]
    description: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode="after")
    def _subject_belongs_to_email(self) -> Self:
        """A subject on an SMS is a subject nobody will ever see.

        Rejected rather than ignored: silently dropping a field somebody typed
        is how a hospital ends up believing its SMS has a headline.
        """
        if self.subject and self.channel is not NotificationChannel.EMAIL:
            raise ValueError("Only an email template has a subject line.")
        return self


class MessageTemplateUpdate(BaseModel):
    category: NotificationCategory | None = None
    subject: Annotated[str | None, Field(max_length=200)] = None
    body: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    description: Annotated[str | None, Field(max_length=255)] = None
    is_active: bool | None = None


class MessageTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    channel: NotificationChannel
    language: str
    category: NotificationCategory
    subject: str | None
    body: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TemplatePreview(BaseModel):
    """Render a template against sample values without sending anything.

    Exists because the alternative way to find out that a template has a typo in
    a placeholder name is to read it on a patient's phone.
    """

    context: dict[str, Any] = Field(default_factory=dict)


class TemplatePreviewResult(BaseModel):
    subject: str | None
    body: str
    missing: list[str] = Field(
        default_factory=list,
        description="Placeholders the template asks for that the context did not supply.",
    )
    placeholders: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
class AdhocSend(BaseModel):
    """A message composed by staff, outside any event trigger.

    Still goes through the same dispatcher, and therefore through the same
    suppression checks. There is deliberately no path that sends a message
    without them.
    """

    patient_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    recipient_type: RecipientType = RecipientType.PATIENT
    category: NotificationCategory = NotificationCategory.ADMINISTRATIVE
    # Either name a template code, or supply a body directly.
    template_code: Annotated[str | None, Field(max_length=64)] = None
    body: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    subject: Annotated[str | None, Field(max_length=200)] = None
    context: dict[str, Any] = Field(default_factory=dict)
    encounter_id: uuid.UUID | None = None
    # Restrict the ladder to one channel, e.g. "send this one by SMS".
    channel: NotificationChannel | None = None

    @model_validator(mode="after")
    def _needs_a_recipient_and_something_to_say(self) -> Self:
        if self.recipient_type is RecipientType.PATIENT and self.patient_id is None:
            raise ValueError("A patient message needs a patient_id.")
        if self.recipient_type is RecipientType.STAFF and self.user_id is None:
            raise ValueError("A staff message needs a user_id.")
        if not self.template_code and not self.body:
            raise ValueError("Supply either a template_code or a body.")
        return self


class NotificationCancelRequest(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class AttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    attempt: int
    channel: NotificationChannel
    address: str
    succeeded: bool
    gateway: str
    provider_message_id: str | None
    error_code: str | None
    error_detail: str | None
    latency_ms: int | None
    created_at: datetime


class NotificationSummary(BaseModel):
    """The outbox row — the list screen is long.

    Carries the patient's name and UHID, not only their id. The outbox exists
    to answer "I never got the message", which is a sentence a named person
    says at a counter; a page of UUIDs cannot be scanned for them, and the
    alternative is a request per row. Attached in bulk by the router.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_code: str
    category: NotificationCategory
    recipient_type: RecipientType
    patient_id: uuid.UUID | None
    patient_name: str | None = None
    uhid: str | None = None
    status: NotificationStatus
    channel: NotificationChannel | None
    attempts: int
    needs_template: bool
    sent_at: datetime | None
    created_at: datetime


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_code: str
    category: NotificationCategory
    language: str

    recipient_type: RecipientType
    patient_id: uuid.UUID | None
    user_id: uuid.UUID | None
    encounter_id: uuid.UUID | None
    recipient_name: str | None
    recipient_phone: str | None
    recipient_email: str | None

    subject: str | None
    body: str

    status: NotificationStatus
    channel: NotificationChannel | None
    attempts: int
    sent_at: datetime | None
    failed_at: datetime | None
    last_error: str | None
    suppression_reason: SuppressionReason | None
    cancellation_reason: str | None
    needs_template: bool

    source_module: str | None
    source_type: str | None
    source_id: uuid.UUID | None
    created_at: datetime

    attempt_log: list[AttemptRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Suppressions
# ---------------------------------------------------------------------------
class SuppressionCreate(BaseModel):
    """Record a block. `DECEASED` is not accepted here.

    A death is recorded in `clinical`, and the suppression follows from it
    inside that same transaction. Allowing it to be typed in directly would
    create a second, editable source of truth for the one rule CLAUDE.md §14
    says to guard actively.
    """

    patient_id: uuid.UUID
    reason: SuppressionReason = SuppressionReason.OPTED_OUT
    channel: NotificationChannel | None = None
    category: NotificationCategory | None = None
    note: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode="after")
    def _death_is_not_typed_in(self) -> Self:
        if self.reason is SuppressionReason.DECEASED:
            raise ValueError(
                "A death suppression is recorded by the clinical death entry, not by hand."
            )
        if self.reason is SuppressionReason.DISABLED:
            raise ValueError(
                "DISABLED describes the deployment configuration, not a patient. "
                "Set NOTIFICATIONS_ENABLED instead."
            )
        return self


class SuppressionLift(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class SuppressionRead(BaseModel):
    """A block on messaging one patient.

    The endpoint's own summary is "who is blocked from messaging", and a
    `patient_id` does not answer *who*. Name and UHID are attached in bulk by
    the router, like every other worklist in the system.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    patient_name: str | None = None
    uhid: str | None = None
    reason: SuppressionReason
    channel: NotificationChannel | None
    category: NotificationCategory | None
    note: str | None
    recorded_by_id: uuid.UUID | None
    lifted_at: datetime | None
    lifted_reason: str | None
    created_at: datetime
