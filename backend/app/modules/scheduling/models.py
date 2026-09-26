"""Doctor availability, appointments and the OPD queue.

Two lifecycles live here, and both are deliberately explicit rather than a free
`status` column anyone can assign to:

* **Appointment** — booked, checked in, seen, or one of the terminal outcomes
  (cancelled, no-show). CLAUDE.md §13 makes no-show and cancellation
  first-class, not an afterthought.
* **QueueEntry** — the physical waiting room: a token that is waiting, called,
  with the doctor, or gone.

They are separate because they answer different questions. An appointment is a
promise made in advance ("Tuesday, 11:00"); a queue entry is what is happening
in the corridor right now. A walk-in has a queue entry from the moment they
arrive and an appointment created alongside it, so downstream modules only ever
have to look in one place for "who was scheduled to be seen".
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import Date, DateTime, Index, Time, text
from sqlmodel import Field

from app.core.models import AuditedTenantModel, TenantModel

__all__ = [
    "Appointment",
    "AppointmentSequence",
    "AppointmentSource",
    "AppointmentStatus",
    "AvailabilityException",
    "Doctor",
    "DoctorAvailability",
    "QueueEntry",
    "QueuePriority",
    "QueueStatus",
    "TokenSequence",
    "Weekday",
]


class Weekday(enum.IntEnum):
    """Matches `datetime.date.weekday()` — Monday is 0.

    An IntEnum rather than a string so comparisons against Python's own weekday
    arithmetic need no translation table, which is exactly the kind of mapping
    that eventually gets one day out.
    """

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


class AppointmentStatus(enum.StrEnum):
    SCHEDULED = "SCHEDULED"
    CHECKED_IN = "CHECKED_IN"
    IN_CONSULTATION = "IN_CONSULTATION"
    COMPLETED = "COMPLETED"
    # --- terminal, first-class (CLAUDE.md §13 step 4) ---------------------
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"
    RESCHEDULED = "RESCHEDULED"


class AppointmentSource(enum.StrEnum):
    WALK_IN = "WALK_IN"
    PHONE = "PHONE"
    COUNTER = "COUNTER"
    ONLINE = "ONLINE"
    REFERRAL = "REFERRAL"
    FOLLOW_UP = "FOLLOW_UP"


class QueueStatus(enum.StrEnum):
    WAITING = "WAITING"
    CALLED = "CALLED"
    IN_CONSULTATION = "IN_CONSULTATION"
    COMPLETED = "COMPLETED"
    # Called but did not come forward. Recoverable — they may be in the toilet,
    # not gone — so this is not the same as leaving.
    SKIPPED = "SKIPPED"
    LEFT_WITHOUT_BEING_SEEN = "LEFT_WITHOUT_BEING_SEEN"


class QueuePriority(enum.IntEnum):
    """Lower sorts first. Values are spaced so a new tier can be slotted between
    two existing ones without renumbering rows that are already printed on
    tokens."""

    EMERGENCY = 10
    # Statutory in most Indian hospitals: senior citizens and pregnant women are
    # seen ahead of the general queue.
    PRIORITY = 20
    NORMAL = 30


class Doctor(AuditedTenantModel, table=True):
    """A practising clinician's scheduling profile.

    Separate from the `users` row: not every user is a doctor, and this holds
    facts that only matter when booking — which department they sit in, how long
    they take, whether they are currently accepting appointments at all.

    Consultation *pricing* is deliberately absent. Money belongs to `billing`,
    which models rate cards (cash, insurance, PMJAY) rather than a single
    number, and duplicating a fee here would guarantee the two disagree.
    """

    __tablename__ = "doctors"
    __table_args__ = (
        Index(
            "uq_doctors_user_live",
            "hospital_id",
            "user_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_doctors_hospital_id_department_id", "hospital_id", "department_id"),
    )

    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="RESTRICT", index=True)
    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )

    # Display name is denormalised from the user so a queue screen can be built
    # without joining identity on every render. Kept in sync by the service.
    display_name: str = Field(max_length=200, index=True)
    specialty: str | None = Field(default=None, max_length=120, index=True)
    qualification: str | None = Field(
        default=None, max_length=200, description="e.g. 'MBBS, MD (Medicine)'."
    )
    # National Medical Commission registration. Statutory on prescriptions and
    # discharge summaries, so it is captured here rather than found later.
    registration_number: str | None = Field(default=None, max_length=64, index=True)

    default_slot_minutes: int = Field(
        default=15,
        ge=1,
        le=240,
        description="Fallback when a session does not set its own slot length.",
    )
    # Cleared for leave, resignation, or simply a full book. Reception sees this
    # before it wastes a patient's trip.
    is_accepting_appointments: bool = Field(default=True, index=True)
    is_active: bool = Field(default=True, index=True)


class DoctorAvailability(AuditedTenantModel, table=True):
    """A recurring weekly clinic session, e.g. "Tuesdays 10:00-13:00".

    Stored as a rule rather than as generated slot rows. Materialising every
    slot for every doctor for every future date would be millions of rows that
    are mostly never booked, and changing a clinic's hours would mean rewriting
    them. Slots are derived on demand in `service.get_available_slots`.
    """

    __tablename__ = "doctor_availabilities"
    __table_args__ = (Index("ix_doctor_availabilities_doctor_id_weekday", "doctor_id", "weekday"),)

    doctor_id: uuid.UUID = Field(foreign_key="doctors.id", ondelete="CASCADE", index=True)

    weekday: Weekday = Field(index=True)
    start_time: time = Field(sa_type=Time)
    end_time: time = Field(sa_type=Time)
    slot_minutes: int = Field(default=15, ge=1, le=240)

    # A cap independent of slot arithmetic: an OPD that runs on tokens rather
    # than appointment times still needs to stop issuing them somewhere.
    max_tokens: int | None = Field(
        default=None, ge=1, description="Hard ceiling on tokens for this session."
    )
    location: str | None = Field(
        default=None, max_length=120, description="Room or counter, printed on the token."
    )

    # A session that has ended is closed by date rather than deleted, so past
    # appointments still explain themselves.
    valid_from: date | None = Field(default=None, sa_type=Date)
    valid_until: date | None = Field(default=None, sa_type=Date)
    is_active: bool = Field(default=True, index=True)


class AvailabilityException(AuditedTenantModel, table=True):
    """A one-off deviation: leave, a conference, or an extra Sunday clinic.

    Overrides the weekly rule for a single date. Modelled as an exception rather
    than by editing the recurring session because "Dr Rao is on leave on the
    14th" is a fact with a reason and an author, and it must not silently
    disappear when the regular timetable is next edited.
    """

    __tablename__ = "availability_exceptions"
    __table_args__ = (
        Index("ix_availability_exceptions_doctor_id_date", "doctor_id", "exception_date"),
    )

    doctor_id: uuid.UUID = Field(foreign_key="doctors.id", ondelete="CASCADE", index=True)
    exception_date: date = Field(sa_type=Date, index=True)

    # False = unavailable (leave). True = available at these times instead.
    is_available: bool = Field(default=False)
    start_time: time | None = Field(default=None, sa_type=Time)
    end_time: time | None = Field(default=None, sa_type=Time)
    slot_minutes: int | None = Field(default=None, ge=1, le=240)

    reason: str | None = Field(default=None, max_length=255)
    recorded_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )


class Appointment(AuditedTenantModel, table=True):
    """A patient's booked or walk-in visit to a doctor."""

    __tablename__ = "appointments"
    __table_args__ = (
        Index(
            "uq_appointments_number_live",
            "hospital_id",
            "appointment_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # At most one live appointment per doctor per instant. The service
        # checks for a clash first, but two receptionists clicking at the same
        # moment both pass that check and both insert — only the database can
        # actually arbitrate. Cancelled, no-show and rescheduled rows are
        # excluded, so a freed slot becomes bookable again.
        Index(
            "uq_appointments_doctor_slot_live",
            "doctor_id",
            "scheduled_start",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL AND status NOT IN ('CANCELLED', 'NO_SHOW', 'RESCHEDULED')"
            ),
        ),
        # The doctor's day view, and the double-booking check.
        Index("ix_appointments_doctor_id_scheduled_start", "doctor_id", "scheduled_start"),
        # "What is this patient's history with us" and follow-up compliance.
        Index("ix_appointments_patient_id_scheduled_start", "patient_id", "scheduled_start"),
        Index("ix_appointments_hospital_id_status", "hospital_id", "status"),
    )

    appointment_number: str = Field(
        max_length=32, index=True, description="Human-quotable reference, e.g. 'APT-26-000123'."
    )

    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    doctor_id: uuid.UUID = Field(foreign_key="doctors.id", ondelete="RESTRICT", index=True)
    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )

    scheduled_start: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    scheduled_end: datetime = Field(
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # Never assigned directly — every change goes through `transitions.py`.
    status: AppointmentStatus = Field(default=AppointmentStatus.SCHEDULED, index=True)
    source: AppointmentSource = Field(default=AppointmentSource.COUNTER, index=True)

    reason: str | None = Field(
        default=None, max_length=500, description="Complaint in the patient's words."
    )
    notes: str | None = Field(default=None, max_length=500)

    booked_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )

    checked_in_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    started_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # --- terminal outcomes, with the metadata that makes them explicable ---
    cancelled_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    cancelled_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    cancellation_reason: str | None = Field(default=None, max_length=255)
    # Who cancelled matters for follow-up: a clinic that cancels on a patient
    # owes them a new slot, and one a patient cancels does not.
    cancelled_by_patient: bool = Field(default=False)

    no_show_marked_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # Set on the old appointment when a patient is moved to a new slot, so the
    # chain stays walkable rather than looking like a cancellation plus an
    # unrelated booking.
    rescheduled_to_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="appointments.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )

    # Filled by `clinical` once it creates the Encounter for this visit. Kept
    # nullable and unenforced here so `scheduling` never has to know about the
    # clinical module — the link is written through an event handler.
    encounter_id: uuid.UUID | None = Field(default=None, index=True)


class QueueEntry(AuditedTenantModel, table=True):
    """A token in a doctor's queue for one day.

    Separate from the appointment because the waiting room is its own reality:
    tokens are called out of order for emergencies, patients wander off and come
    back, and a doctor running late changes nothing about the appointment that
    was booked three weeks ago.
    """

    __tablename__ = "queue_entries"
    __table_args__ = (
        Index(
            "uq_queue_entries_token_live",
            "hospital_id",
            "doctor_id",
            "queue_date",
            "token_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The queue screen: one doctor, today, ordered.
        Index(
            "ix_queue_entries_doctor_id_queue_date_status",
            "doctor_id",
            "queue_date",
            "status",
        ),
    )

    queue_date: date = Field(sa_type=Date, index=True)
    token_number: int = Field(ge=1, description="Called out loud; restarts daily per doctor.")

    doctor_id: uuid.UUID = Field(foreign_key="doctors.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    appointment_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="appointments.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )

    status: QueueStatus = Field(default=QueueStatus.WAITING, index=True)
    priority: QueuePriority = Field(default=QueuePriority.NORMAL, index=True)
    priority_reason: str | None = Field(
        default=None,
        max_length=120,
        description="Required when jumping the queue — an audit answer, not a note.",
    )

    # The order the doctor will see patients in, within one doctor's day.
    # Stored rather than derived from (priority, arrival) so reception can
    # re-rank by hand — drag the child with the fever above the routine
    # follow-up — and the doctor's list, the token board and "how many ahead"
    # all agree. Seeded at check-in so untouched queues sort exactly as they
    # did before this column existed: tier first, then arrival. Renumbered
    # 1..n on every manual move; not unique, because two counters checking in
    # at the same instant may both take max+1, and arrival breaks that tie.
    position: int = Field(default=0)

    checked_in_at: datetime = Field(
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    called_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    started_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # How many times they were called and did not appear. Three strikes is a
    # policy decision for the hospital, so the count is recorded and the
    # judgement left to staff.
    skip_count: int = Field(default=0)


class TokenSequence(TenantModel, table=True):
    """Per-doctor, per-day token counter.

    Same reasoning as `patients.UhidSequence`: a Postgres SEQUENCE is global and
    cannot restart per doctor per day, and token numbers are read aloud in a
    waiting room — they have to be small.
    """

    __tablename__ = "token_sequences"
    __table_args__ = (
        Index(
            "uq_token_sequences_doctor_date",
            "hospital_id",
            "doctor_id",
            "queue_date",
            unique=True,
        ),
    )

    doctor_id: uuid.UUID = Field(foreign_key="doctors.id", ondelete="CASCADE", index=True)
    queue_date: date = Field(sa_type=Date, index=True)
    last_value: int = Field(default=0)


class AppointmentSequence(TenantModel, table=True):
    """Per-hospital, per-year appointment number counter.

    Deliberately its own table rather than sharing `TokenSequence` with a
    sentinel doctor id: that trick saved one small model and paid for it by
    making the foreign key to `doctors` unsatisfiable. A constraint the database
    can actually enforce is worth more than a table avoided.
    """

    __tablename__ = "appointment_sequences"
    __table_args__ = (
        Index("uq_appointment_sequences_hospital_year", "hospital_id", "year", unique=True),
    )

    year: int = Field(index=True)
    last_value: int = Field(default=0)
