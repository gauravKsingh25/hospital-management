"""Domain events published by `scheduling`.

These are the module's other public surface. `clinical` will subscribe to
`PatientCheckedIn` to open an Encounter, `billing` to `ConsultationCompleted` to
capture the consultation charge, and `notifications` to the appointment events
for reminders — none of which requires `scheduling` to know any of those modules
exist (CLAUDE.md §2).

Kept deliberately thin: an event carries identifiers and the few facts a
subscriber cannot cheaply re-derive, not a copy of the record. A fat event
becomes a second, silently diverging schema.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from app.core.events import DomainEvent

__all__ = [
    "AppointmentBooked",
    "AppointmentCancelled",
    "AppointmentNoShow",
    "ConsultationCompleted",
    "ConsultationStarted",
    "PatientCheckedIn",
    "QueueEntryReassigned",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class AppointmentBooked(DomainEvent):
    appointment_id: uuid.UUID
    appointment_number: str
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    scheduled_start: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientCheckedIn(DomainEvent):
    """The patient is physically present and holding a token.

    This is the moment `clinical` creates the Encounter (CLAUDE.md §6:
    REGISTERED). Publishing it rather than calling `clinical` directly is what
    keeps the OPD flow working before that module exists, and what will let it
    be extracted later.
    """

    appointment_id: uuid.UUID
    queue_entry_id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    department_id: uuid.UUID | None
    token_number: int
    queue_date: date


@dataclass(frozen=True, kw_only=True, slots=True)
class ConsultationStarted(DomainEvent):
    appointment_id: uuid.UUID
    queue_entry_id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID


@dataclass(frozen=True, kw_only=True, slots=True)
class ConsultationCompleted(DomainEvent):
    appointment_id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    # Wall-clock minutes with the doctor. Feeds the queue analytics in
    # `reporting` and, later, honest waiting-time estimates.
    duration_minutes: int


@dataclass(frozen=True, kw_only=True, slots=True)
class AppointmentCancelled(DomainEvent):
    appointment_id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    scheduled_start: datetime
    # Decides who owes whom a new slot, and whether a notification is an apology
    # or a confirmation.
    cancelled_by_patient: bool
    reason: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class AppointmentNoShow(DomainEvent):
    appointment_id: uuid.UUID
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    scheduled_start: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class QueueEntryReassigned(DomainEvent):
    """Reception moved a waiting patient to a different doctor's queue.

    Carries both doctors and both tokens because every subscriber wants the
    pair: a notification has to tell the patient their *new* number, and the
    analytics have to take one off the old clinic and put one on the new.
    """

    queue_entry_id: uuid.UUID
    appointment_id: uuid.UUID | None
    patient_id: uuid.UUID
    from_doctor_id: uuid.UUID
    to_doctor_id: uuid.UUID
    from_token_number: int
    to_token_number: int
    queue_date: date
    reason: str | None
