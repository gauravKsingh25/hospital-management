"""Request/response DTOs for `scheduling`."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.scheduling.models import (
    AppointmentSource,
    AppointmentStatus,
    QueuePriority,
    QueueStatus,
    Weekday,
)

__all__ = [
    "AppointmentBook",
    "AppointmentCancelRequest",
    "AppointmentRead",
    "AppointmentReschedule",
    "AvailabilityExceptionCreate",
    "AvailabilityExceptionRead",
    "DoctorAvailabilityCreate",
    "DoctorAvailabilityRead",
    "DoctorCreate",
    "DoctorQueue",
    "DoctorRead",
    "DoctorUpdate",
    "QueueBoardEntry",
    "QueueEntryRead",
    "QueueReassignRequest",
    "QueueReorderRequest",
    "QuickOpdRequest",
    "QuickOpdResponse",
    "SearchHit",
    "SearchHitKind",
    "SearchResults",
    "SkipRequest",
    "SlotRead",
]


# ---------------------------------------------------------------------------
# Doctors
# ---------------------------------------------------------------------------
class DoctorCreate(BaseModel):
    user_id: uuid.UUID
    department_id: uuid.UUID | None = None
    specialty: Annotated[str | None, Field(max_length=120)] = None
    qualification: Annotated[str | None, Field(max_length=200)] = None
    registration_number: Annotated[str | None, Field(max_length=64)] = None
    default_slot_minutes: Annotated[int, Field(ge=1, le=240)] = 15


class DoctorUpdate(BaseModel):
    department_id: uuid.UUID | None = None
    specialty: Annotated[str | None, Field(max_length=120)] = None
    qualification: Annotated[str | None, Field(max_length=200)] = None
    registration_number: Annotated[str | None, Field(max_length=64)] = None
    default_slot_minutes: Annotated[int | None, Field(ge=1, le=240)] = None
    is_accepting_appointments: bool | None = None
    is_active: bool | None = None


class DoctorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    department_id: uuid.UUID | None
    display_name: str
    specialty: str | None
    qualification: str | None
    registration_number: str | None
    default_slot_minutes: int
    is_accepting_appointments: bool
    is_active: bool


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
class DoctorAvailabilityCreate(BaseModel):
    weekday: Weekday
    start_time: time
    end_time: time
    slot_minutes: Annotated[int, Field(ge=1, le=240)] = 15
    max_tokens: Annotated[int | None, Field(ge=1)] = None
    location: Annotated[str | None, Field(max_length=120)] = None
    valid_from: date | None = None
    valid_until: date | None = None

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        if self.valid_until and self.valid_from and self.valid_until < self.valid_from:
            raise ValueError("valid_until must not be before valid_from.")
        return self


class DoctorAvailabilityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doctor_id: uuid.UUID
    weekday: Weekday
    start_time: time
    end_time: time
    slot_minutes: int
    max_tokens: int | None
    location: str | None
    valid_from: date | None
    valid_until: date | None
    is_active: bool


class AvailabilityExceptionCreate(BaseModel):
    """Leave, or an extra clinic on a date the weekly rule does not cover."""

    exception_date: date
    is_available: bool = False
    start_time: time | None = None
    end_time: time | None = None
    slot_minutes: Annotated[int | None, Field(ge=1, le=240)] = None
    reason: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.is_available and (self.start_time is None or self.end_time is None):
            raise ValueError("An extra session needs both start_time and end_time.")
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time.")
        return self


class AvailabilityExceptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doctor_id: uuid.UUID
    exception_date: date
    is_available: bool
    start_time: time | None
    end_time: time | None
    reason: str | None


class SlotRead(BaseModel):
    """A bookable window, derived on demand rather than stored."""

    start: datetime
    end: datetime
    is_available: bool
    location: str | None = None


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
class AppointmentBook(BaseModel):
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    scheduled_start: datetime
    source: AppointmentSource = AppointmentSource.COUNTER
    reason: Annotated[str | None, Field(max_length=500)] = None
    notes: Annotated[str | None, Field(max_length=500)] = None
    # Booking outside published clinic hours: a real need (the consultant agreed
    # to see them at 8pm) that must be deliberate rather than accidental.
    override_availability: bool = False


class AppointmentReschedule(BaseModel):
    scheduled_start: datetime
    reason: Annotated[str | None, Field(max_length=255)] = None
    override_availability: bool = False


class AppointmentCancelRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=255)]
    # Drives whether the clinic owes the patient a new slot, and whether the
    # notification reads as an apology or a confirmation.
    cancelled_by_patient: bool = False


class AppointmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    appointment_number: str
    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    department_id: uuid.UUID | None
    scheduled_start: datetime
    scheduled_end: datetime
    status: AppointmentStatus
    source: AppointmentSource
    reason: str | None
    checked_in_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    cancelled_by_patient: bool
    no_show_marked_at: datetime | None
    rescheduled_to_id: uuid.UUID | None
    encounter_id: uuid.UUID | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
class QueueEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    queue_date: date
    token_number: int
    doctor_id: uuid.UUID
    patient_id: uuid.UUID
    appointment_id: uuid.UUID | None
    department_id: uuid.UUID | None
    status: QueueStatus
    priority: QueuePriority
    priority_reason: str | None
    # Rank within the doctor's day. Lower is sooner. Compare, never assume
    # contiguous — see `QueueEntry.position`.
    position: int
    checked_in_at: datetime
    called_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    skip_count: int


class QueueBoardEntry(QueueEntryRead):
    """A queue row with enough about the patient to render a worklist.

    `QueueEntryRead` carries identifiers, which is the right shape for the
    queue's own logic and the wrong shape for a screen. A doctor's worklist
    showing `a3f9c2e1…` instead of a name is not a worklist, and the
    alternative — the frontend fetching each patient in turn — is one request
    per row on the screen staff refresh most often.

    So the router joins the two, once, through `patients.service` rather than
    a raw query across the module boundary (CLAUDE.md §2).

    The fields are optional because a queue row outlives nothing: a patient
    record that has been soft-deleted since check-in leaves the row intact and
    the name absent, which is more honest than failing the whole board.

    Note this is a *staff* view. A public waiting-room display showing token
    numbers only is a different endpoint, unauthenticated, and deliberately
    not this one — patient names must not be on a screen the waiting area can
    read.
    """

    patient_name: str | None = None
    patient_uhid: str | None = None
    patient_age_years: int | None = None
    patient_gender: str | None = None
    patient_is_deceased: bool = False

    # The chart this row opens. Carried here so a doctor clicking a waiting
    # patient goes straight to the consultation screen — without it the
    # frontend has to look the encounter up by appointment first, which is a
    # round trip between the click and anything appearing.
    encounter_id: uuid.UUID | None = None
    encounter_number: str | None = None


# ---------------------------------------------------------------------------
# Universal search (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
class SearchHitKind(enum.StrEnum):
    """What the search matched, so the client can route and label the hit."""

    PATIENT = "PATIENT"
    # Suppressed because ruff's S105 sees the word "token" and assumes a
    # credential. This one is the number called out across a waiting room.
    TOKEN = "TOKEN"  # noqa: S105
    APPOINTMENT = "APPOINTMENT"
    ENCOUNTER = "ENCOUNTER"
    DOCTOR = "DOCTOR"


class SearchHit(BaseModel):
    """One result, already phrased for a screen.

    `title` and `subtitle` are composed on the server rather than assembled by
    the client from six nullable ids. Five kinds of hit render in one list, and
    a client formatting each of them differently is five chances to disagree
    about how a patient is named.

    The ids are all here because the *destination* is the client's business —
    the backend has no idea what a route looks like and must not learn.
    """

    kind: SearchHitKind
    title: str
    subtitle: str | None = None

    patient_id: uuid.UUID | None = None
    patient_uhid: str | None = None
    encounter_id: uuid.UUID | None = None
    appointment_id: uuid.UUID | None = None
    doctor_id: uuid.UUID | None = None

    # Drives the same suppression as everywhere else: a deceased patient must
    # never be offered under a "book a follow-up" affordance.
    is_deceased: bool = False


class SearchResults(BaseModel):
    """Hits in the order they should be shown — best guess first.

    Not paginated, and that is deliberate. This is a lookup box, not a report:
    a receptionist who gets thirty results has mistyped, and the fix is to type
    more, not to page through. Anything that genuinely needs browsing goes to
    the patient index instead.
    """

    query: str
    hits: list[SearchHit] = Field(default_factory=list)


class SkipRequest(BaseModel):
    reason: Annotated[str | None, Field(max_length=120)] = None


class CheckInRequest(BaseModel):
    priority: QueuePriority = QueuePriority.NORMAL
    # Required by the service when priority is not NORMAL: jumping a queue in
    # front of waiting people is a decision someone must own.
    priority_reason: Annotated[str | None, Field(max_length=120)] = None


class QuickOpdRequest(BaseModel):
    """One-click OPD (CLAUDE.md §7b).

    Reception picks a patient and a doctor; the appointment, the check-in and
    the token all happen in this single call. The alternative — book, then find
    the booking, then check in, then issue a token — is four screens for the
    single most repeated action of the day.
    """

    patient_id: uuid.UUID
    doctor_id: uuid.UUID
    reason: Annotated[str | None, Field(max_length=500)] = None
    priority: QueuePriority = QueuePriority.NORMAL
    priority_reason: Annotated[str | None, Field(max_length=120)] = None
    source: AppointmentSource = AppointmentSource.WALK_IN


class QuickOpdResponse(BaseModel):
    """Everything the token slip needs, in one response.

    Including who the patient is. The slip carried a token number, a doctor and
    a room and nothing that identified the person holding it — so the piece of
    paper a patient walks away with could not be matched back to their record,
    by them or by anybody else. `uhid` is also what the QR on the slip encodes
    (CLAUDE.md §7b), which makes this the field the whole scanned-card feature
    is printed from.
    """

    appointment: AppointmentRead
    queue_entry: QueueEntryRead
    patient_name: str
    uhid: str
    token_number: int
    doctor_name: str
    department_name: str | None
    location: str | None
    # What reception tells the patient: "you are number 4 in the queue".
    patients_ahead: int
    # The chart opened alongside the token. Carried here so the doctor's screen
    # can be reached from the check-in response without a second lookup.
    encounter_id: uuid.UUID
    encounter_number: str


class QueueReassignRequest(BaseModel):
    """Move a waiting patient to another doctor's queue."""

    doctor_id: uuid.UUID
    # Optional, unlike a priority jump: balancing a busy clinic is routine and
    # asking reception to justify it every time would only train them to type
    # "busy". The audit log still records who moved whom, and where.
    reason: Annotated[str | None, Field(max_length=255)] = None


class QueueReorderRequest(BaseModel):
    """Put a token right after another one in the same doctor's line.

    `None` means the top. A neighbour rather than an index, so the request
    still means what the receptionist meant if a walk-in joined while they
    were dragging.
    """

    after_entry_id: uuid.UUID | None = None


class DoctorQueue(BaseModel):
    """One doctor's queue, with the counts reception balances by.

    The reception board is a row of these rather than the flat list a nurse
    works from, because reception's question is different: not "who is next"
    but "which clinic is drowning and which is idle". A doctor with nobody
    waiting is included on purpose — an empty column is the answer to that
    question, not noise.
    """

    doctor_id: uuid.UUID
    doctor_name: str
    specialty: str | None = None
    department_id: uuid.UUID | None = None
    department_name: str | None = None
    is_accepting_appointments: bool = True

    waiting: int = 0
    in_consultation: int = 0
    completed: int = 0
    # Minutes the longest-waiting patient has been in this queue. The number
    # that actually tells reception a clinic is behind — three people waiting
    # for five minutes is fine; one waiting for fifty is not.
    longest_wait_minutes: int | None = None

    # The line, in the order the doctor will see them.
    entries: list[QueueBoardEntry] = Field(default_factory=list)
    # Today's seen patients, most recent first — kept on the board, struck
    # through, so reception can still answer "has she been in yet?". Tokens
    # that left without being seen are counted nowhere here and not listed.
    seen: list[QueueBoardEntry] = Field(default_factory=list)


class QueueSummary(BaseModel):
    """The live queue board for one doctor."""

    doctor_id: uuid.UUID
    doctor_name: str
    queue_date: date
    waiting: int
    in_consultation: int
    completed: int
    current_token: int | None
    next_token: int | None
