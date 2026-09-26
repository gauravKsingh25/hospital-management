"""Scheduling business logic — the module's public interface."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import ColumnElement, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.events import event_bus
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.scheduling.events import (
    AppointmentBooked,
    AppointmentCancelled,
    AppointmentNoShow,
    ConsultationCompleted,
    ConsultationStarted,
    PatientCheckedIn,
    QueueEntryReassigned,
)
from app.modules.scheduling.models import (
    Appointment,
    AppointmentSequence,
    AppointmentSource,
    AppointmentStatus,
    AvailabilityException,
    Doctor,
    DoctorAvailability,
    QueueEntry,
    QueuePriority,
    QueueStatus,
    TokenSequence,
    Weekday,
)
from app.modules.scheduling.schemas import (
    AppointmentBook,
    AvailabilityExceptionCreate,
    DoctorAvailabilityCreate,
    DoctorCreate,
    DoctorUpdate,
    SearchHit,
    SearchHitKind,
    SlotRead,
)
from app.modules.scheduling.transitions import (
    TERMINAL_QUEUE_STATUSES,
    assert_appointment_transition,
    assert_queue_transition,
)

# `tenancy` owns the hospital record, and therefore owns what day it is here.
# A module-level import rather than a local one: this is not a cycle risk —
# `tenancy` depends on nothing above it — and the queue's date is not an
# incidental detail of one function.
from app.modules.tenancy import service as tenancy_service

logger = logging.getLogger(__name__)

__all__ = [
    "book_appointment",
    "call_next_patient",
    "cancel_appointment",
    "check_in",
    "close_queue_for_appointment",
    "complete_consultation",
    "create_doctor",
    "get_appointment",
    "get_appointment_by_number",
    "get_available_slots",
    "get_doctor",
    "get_queue",
    "link_encounter",
    "list_appointments",
    "mark_no_show",
    "mark_overdue_no_shows",
    "quick_opd",
    "reschedule_appointment",
    "search",
    "set_availability",
    "skip_token",
    "start_consultation",
]

# How long after a booked start a patient can still turn up before the slot is
# written off. Generous on purpose: Indian OPDs run late, and a patient sitting
# in the corridor while the system marks them absent is worse than a slot held
# open a little too long.
NO_SHOW_GRACE = timedelta(minutes=90)


# ---------------------------------------------------------------------------
# Doctors
# ---------------------------------------------------------------------------
async def create_doctor(
    session: AsyncSession,
    payload: DoctorCreate,
    *,
    hospital_id: uuid.UUID,
    display_name: str,
) -> Doctor:
    """Attach a scheduling profile to an existing user.

    `display_name` is passed in by the caller (which has already loaded the user
    through `identity.service`) rather than joined here — `scheduling` must not
    read another module's tables directly (CLAUDE.md §2).
    """
    existing = (
        (
            await session.execute(
                select(Doctor).where(
                    col(Doctor.hospital_id) == hospital_id,
                    col(Doctor.user_id) == payload.user_id,
                    col(Doctor.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise ConflictError("That user already has a doctor profile.", code="doctor_profile_exists")

    doctor = Doctor(
        hospital_id=hospital_id,
        display_name=display_name,
        **payload.model_dump(),
    )
    session.add(doctor)
    await session.flush()
    await session.refresh(doctor)
    return doctor


async def get_doctor(
    session: AsyncSession, doctor_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Doctor:
    doctor = (
        (
            await session.execute(
                select(Doctor).where(
                    col(Doctor.id) == doctor_id,
                    col(Doctor.hospital_id) == hospital_id,
                    col(Doctor.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if doctor is None:
        raise NotFoundError("Doctor not found.", code="doctor_not_found")
    return doctor


async def get_doctor_by_user(
    session: AsyncSession, user_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Doctor | None:
    """Resolve the logged-in user to their doctor profile.

    Used by the "my queue" routes so a doctor never has to know their own
    internal id.
    """
    return (
        (
            await session.execute(
                select(Doctor).where(
                    col(Doctor.user_id) == user_id,
                    col(Doctor.hospital_id) == hospital_id,
                    col(Doctor.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def list_doctors(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
    accepting_only: bool = False,
) -> tuple[list[Doctor], int]:
    filters: list[ColumnElement[bool]] = [
        col(Doctor.hospital_id) == hospital_id,
        col(Doctor.deleted_at).is_(None),
        col(Doctor.is_active).is_(True),
    ]
    if department_id is not None:
        filters.append(col(Doctor.department_id) == department_id)
    if accepting_only:
        filters.append(col(Doctor.is_accepting_appointments).is_(True))

    total = (
        await session.execute(select(func.count()).select_from(Doctor).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Doctor)
                .where(*filters)
                .order_by(col(Doctor.display_name))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def list_all_active_doctors(session: AsyncSession, *, hospital_id: uuid.UUID) -> list[Doctor]:
    """Every practising doctor, unpaginated.

    The one deliberate exception to CLAUDE.md §11's "every list is paginated":
    this is bounded by the size of the medical staff, not by anything that
    grows with patients, and the screen that needs it — reception's per-doctor
    board — is meaningless if a doctor can fall off the second page.
    """
    rows = (
        (
            await session.execute(
                select(Doctor)
                .where(
                    col(Doctor.hospital_id) == hospital_id,
                    col(Doctor.deleted_at).is_(None),
                    col(Doctor.is_active).is_(True),
                )
                .order_by(col(Doctor.display_name))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def update_doctor(session: AsyncSession, doctor: Doctor, payload: DoctorUpdate) -> Doctor:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doctor, field, value)
    doctor.updated_at = utc_now()
    session.add(doctor)
    await session.flush()
    await session.refresh(doctor)
    return doctor


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
async def set_availability(
    session: AsyncSession, doctor: Doctor, payload: DoctorAvailabilityCreate
) -> DoctorAvailability:
    """Add a recurring weekly session, refusing one that overlaps another."""
    same_day = (
        (
            await session.execute(
                select(DoctorAvailability).where(
                    col(DoctorAvailability.doctor_id) == doctor.id,
                    col(DoctorAvailability.weekday) == payload.weekday,
                    col(DoctorAvailability.is_active).is_(True),
                    col(DoctorAvailability.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for session_row in same_day:
        if payload.start_time < session_row.end_time and session_row.start_time < payload.end_time:
            raise ConflictError(
                "That overlaps an existing session for this doctor on the same day.",
                code="availability_overlap",
                details={
                    "existing": f"{session_row.start_time:%H:%M}-{session_row.end_time:%H:%M}"
                },
            )

    availability = DoctorAvailability(
        hospital_id=doctor.hospital_id, doctor_id=doctor.id, **payload.model_dump()
    )
    session.add(availability)
    await session.flush()
    await session.refresh(availability)
    return availability


async def list_availability(
    session: AsyncSession, doctor_id: uuid.UUID
) -> list[DoctorAvailability]:
    rows = (
        (
            await session.execute(
                select(DoctorAvailability)
                .where(
                    col(DoctorAvailability.doctor_id) == doctor_id,
                    col(DoctorAvailability.deleted_at).is_(None),
                    col(DoctorAvailability.is_active).is_(True),
                )
                .order_by(col(DoctorAvailability.weekday), col(DoctorAvailability.start_time))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def add_availability_exception(
    session: AsyncSession,
    doctor: Doctor,
    payload: AvailabilityExceptionCreate,
    *,
    recorded_by_id: uuid.UUID | None = None,
) -> AvailabilityException:
    exception = AvailabilityException(
        hospital_id=doctor.hospital_id,
        doctor_id=doctor.id,
        recorded_by_id=recorded_by_id,
        **payload.model_dump(),
    )
    session.add(exception)
    await session.flush()
    await session.refresh(exception)
    return exception


async def _sessions_for(
    session: AsyncSession, doctor: Doctor, on_date: date
) -> list[tuple[time, time, int, str | None, int | None]]:
    """The doctor's working windows on one date, after exceptions.

    Returns `(start, end, slot_minutes, location, max_tokens)` tuples. An
    exception either cancels the day outright or replaces it — a doctor is
    never both on leave and holding a clinic.
    """
    exceptions = (
        (
            await session.execute(
                select(AvailabilityException).where(
                    col(AvailabilityException.doctor_id) == doctor.id,
                    col(AvailabilityException.exception_date) == on_date,
                    col(AvailabilityException.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    blocking = [item for item in exceptions if not item.is_available]
    if blocking:
        return []

    extra = [item for item in exceptions if item.is_available]
    if extra:
        return [
            (
                item.start_time or time(0, 0),
                item.end_time or time(23, 59),
                item.slot_minutes or doctor.default_slot_minutes,
                None,
                None,
            )
            for item in extra
        ]

    weekday = Weekday(on_date.weekday())
    rules = (
        (
            await session.execute(
                select(DoctorAvailability)
                .where(
                    col(DoctorAvailability.doctor_id) == doctor.id,
                    col(DoctorAvailability.weekday) == weekday,
                    col(DoctorAvailability.is_active).is_(True),
                    col(DoctorAvailability.deleted_at).is_(None),
                )
                .order_by(col(DoctorAvailability.start_time))
            )
        )
        .scalars()
        .all()
    )

    windows = []
    for rule in rules:
        if rule.valid_from and on_date < rule.valid_from:
            continue
        if rule.valid_until and on_date > rule.valid_until:
            continue
        windows.append(
            (rule.start_time, rule.end_time, rule.slot_minutes, rule.location, rule.max_tokens)
        )
    return windows


async def get_available_slots(
    session: AsyncSession, doctor: Doctor, on_date: date
) -> list[SlotRead]:
    """Derive bookable slots for a date.

    Computed rather than stored: materialising every slot for every doctor for
    every future date would be millions of rows that are mostly never booked,
    and moving a clinic by half an hour would mean rewriting all of them.
    """
    windows = await _sessions_for(session, doctor, on_date)
    if not windows:
        return []

    day_start = datetime.combine(on_date, time(0, 0), tzinfo=UTC)
    booked = {
        row.scheduled_start
        for row in (
            (
                await session.execute(
                    select(Appointment).where(
                        col(Appointment.doctor_id) == doctor.id,
                        col(Appointment.scheduled_start) >= day_start,
                        col(Appointment.scheduled_start) < day_start + timedelta(days=1),
                        col(Appointment.deleted_at).is_(None),
                        col(Appointment.status).notin_(
                            [
                                AppointmentStatus.CANCELLED,
                                AppointmentStatus.NO_SHOW,
                                AppointmentStatus.RESCHEDULED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    }

    slots: list[SlotRead] = []
    for start_time, end_time, slot_minutes, location, _ in windows:
        cursor = datetime.combine(on_date, start_time, tzinfo=UTC)
        window_end = datetime.combine(on_date, end_time, tzinfo=UTC)
        step = timedelta(minutes=slot_minutes)
        while cursor + step <= window_end:
            slots.append(
                SlotRead(
                    start=cursor,
                    end=cursor + step,
                    is_available=cursor not in booked,
                    location=location,
                )
            )
            cursor += step
    return slots


async def _is_within_availability(
    session: AsyncSession, doctor: Doctor, start: datetime, end: datetime
) -> bool:
    windows = await _sessions_for(session, doctor, start.date())
    for start_time, end_time, _, _, _ in windows:
        window_start = datetime.combine(start.date(), start_time, tzinfo=start.tzinfo or UTC)
        window_end = datetime.combine(start.date(), end_time, tzinfo=start.tzinfo or UTC)
        if window_start <= start and end <= window_end:
            return True
    return False


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------
async def _next_appointment_number(session: AsyncSession, hospital_id: uuid.UUID) -> str:
    """`APT-YY-NNNNNN`, unique per hospital per year.

    Same upsert-then-lock shape as the token counter below; a separate table
    because appointment numbers are not per-doctor and must not borrow a foreign
    key they cannot satisfy.
    """
    year = datetime.now(UTC).year
    await session.execute(
        text(
            """
            INSERT INTO appointment_sequences
                (id, hospital_id, year, last_value, created_at, updated_at)
            VALUES (:id, :hospital_id, :year, 0, now(), now())
            ON CONFLICT (hospital_id, year) DO NOTHING
            """
        ),
        {"id": new_id(), "hospital_id": hospital_id, "year": year},
    )
    row = (
        (
            await session.execute(
                select(AppointmentSequence)
                .where(
                    col(AppointmentSequence.hospital_id) == hospital_id,
                    col(AppointmentSequence.year) == year,
                )
                .with_for_update()
            )
        )
        .scalars()
        .one()
    )

    row.last_value += 1
    session.add(row)
    await session.flush()
    return f"APT-{year % 100:02d}-{row.last_value:06d}"


async def _next_counter(
    session: AsyncSession, hospital_id: uuid.UUID, doctor_id: uuid.UUID, on_date: date
) -> int:
    """Atomically increment a per-hospital/doctor/date counter.

    `ON CONFLICT DO NOTHING` then `SELECT ... FOR UPDATE`: a plain
    check-then-insert races on the first allocation of the day, which is exactly
    when several counters open at once.
    """
    await session.execute(
        text(
            """
            INSERT INTO token_sequences
                (id, hospital_id, doctor_id, queue_date, last_value, created_at, updated_at)
            VALUES (:id, :hospital_id, :doctor_id, :queue_date, 0, now(), now())
            ON CONFLICT (hospital_id, doctor_id, queue_date) DO NOTHING
            """
        ),
        {
            "id": new_id(),
            "hospital_id": hospital_id,
            "doctor_id": doctor_id,
            "queue_date": on_date,
        },
    )
    row = (
        (
            await session.execute(
                select(TokenSequence)
                .where(
                    col(TokenSequence.hospital_id) == hospital_id,
                    col(TokenSequence.doctor_id) == doctor_id,
                    col(TokenSequence.queue_date) == on_date,
                )
                .with_for_update()
            )
        )
        .scalars()
        .one()
    )

    row.last_value += 1
    session.add(row)
    await session.flush()
    return int(row.last_value)


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
async def book_appointment(
    session: AsyncSession,
    payload: AppointmentBook,
    *,
    hospital_id: uuid.UUID,
    booked_by_id: uuid.UUID | None = None,
) -> Appointment:
    doctor = await get_doctor(session, payload.doctor_id, hospital_id=hospital_id)
    if not doctor.is_accepting_appointments:
        raise ConflictError(
            f"{doctor.display_name} is not accepting appointments.",
            code="doctor_not_accepting",
        )

    start = payload.scheduled_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = start + timedelta(minutes=doctor.default_slot_minutes)

    if not payload.override_availability and not await _is_within_availability(
        session, doctor, start, end
    ):
        raise ConflictError(
            f"{doctor.display_name} does not hold a clinic at that time.",
            code="outside_availability",
            details={"resolution": "Pass override_availability to book anyway."},
        )

    clash = (
        (
            await session.execute(
                select(Appointment).where(
                    col(Appointment.doctor_id) == doctor.id,
                    col(Appointment.scheduled_start) == start,
                    col(Appointment.deleted_at).is_(None),
                    col(Appointment.status).notin_(
                        [
                            AppointmentStatus.CANCELLED,
                            AppointmentStatus.NO_SHOW,
                            AppointmentStatus.RESCHEDULED,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .first()
    )
    if clash is not None:
        raise ConflictError(
            "That slot is already booked.",
            code="slot_taken",
            details={"appointment_number": clash.appointment_number},
        )

    appointment = Appointment(
        hospital_id=hospital_id,
        appointment_number=await _next_appointment_number(session, hospital_id),
        patient_id=payload.patient_id,
        doctor_id=doctor.id,
        department_id=doctor.department_id,
        scheduled_start=start,
        scheduled_end=end,
        source=payload.source,
        reason=payload.reason,
        notes=payload.notes,
        booked_by_id=booked_by_id,
    )
    session.add(appointment)
    await session.flush()
    await session.refresh(appointment)

    await event_bus.publish(
        AppointmentBooked(
            hospital_id=hospital_id,
            actor_id=booked_by_id,
            appointment_id=appointment.id,
            appointment_number=appointment.appointment_number,
            patient_id=appointment.patient_id,
            doctor_id=doctor.id,
            scheduled_start=start,
        ),
        session=session,
    )
    return appointment


async def get_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Appointment:
    appointment = (
        (
            await session.execute(
                select(Appointment).where(
                    col(Appointment.id) == appointment_id,
                    col(Appointment.hospital_id) == hospital_id,
                    col(Appointment.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if appointment is None:
        raise NotFoundError("Appointment not found.", code="appointment_not_found")
    return appointment


async def get_visit_links(
    session: AsyncSession, appointment_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, uuid.UUID]:
    """Appointment id → the Encounter it opened, whatever state that visit is in.

    `link_encounter` writes this at check-in and nothing unwrites it, so it
    still answers after the visit has closed — which `clinical`'s live-chart
    lookup, by design, does not.
    """
    if not appointment_ids:
        return {}
    rows = (
        await session.execute(
            select(col(Appointment.id), col(Appointment.encounter_id)).where(
                col(Appointment.id).in_(list(appointment_ids)),
                col(Appointment.hospital_id) == hospital_id,
                col(Appointment.encounter_id).is_not(None),
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows if row[1] is not None}


async def list_appointments(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    doctor_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    on_date: date | None = None,
    status: AppointmentStatus | None = None,
) -> tuple[list[Appointment], int]:
    filters: list[ColumnElement[bool]] = [
        col(Appointment.hospital_id) == hospital_id,
        col(Appointment.deleted_at).is_(None),
    ]
    if doctor_id is not None:
        filters.append(col(Appointment.doctor_id) == doctor_id)
    if patient_id is not None:
        filters.append(col(Appointment.patient_id) == patient_id)
    if status is not None:
        filters.append(col(Appointment.status) == status)
    if on_date is not None:
        day_start = datetime.combine(on_date, time(0, 0), tzinfo=UTC)
        filters.append(col(Appointment.scheduled_start) >= day_start)
        filters.append(col(Appointment.scheduled_start) < day_start + timedelta(days=1))

    total = (
        await session.execute(select(func.count()).select_from(Appointment).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Appointment)
                .where(*filters)
                .order_by(col(Appointment.scheduled_start))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def cancel_appointment(
    session: AsyncSession,
    appointment: Appointment,
    *,
    reason: str,
    cancelled_by_patient: bool = False,
    actor_id: uuid.UUID | None = None,
) -> Appointment:
    assert_appointment_transition(appointment.status, AppointmentStatus.CANCELLED)

    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = utc_now()
    appointment.cancelled_by_id = actor_id
    appointment.cancellation_reason = reason
    appointment.cancelled_by_patient = cancelled_by_patient
    appointment.updated_at = utc_now()
    session.add(appointment)

    # A cancelled appointment must not leave a live token in the queue.
    await _close_queue_entries(session, appointment.id, status=QueueStatus.LEFT_WITHOUT_BEING_SEEN)
    await session.flush()

    await event_bus.publish(
        AppointmentCancelled(
            hospital_id=appointment.hospital_id,
            actor_id=actor_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            scheduled_start=appointment.scheduled_start,
            cancelled_by_patient=cancelled_by_patient,
            reason=reason,
        ),
        session=session,
    )
    await session.refresh(appointment)
    return appointment


async def mark_no_show(
    session: AsyncSession, appointment: Appointment, *, actor_id: uuid.UUID | None = None
) -> Appointment:
    assert_appointment_transition(appointment.status, AppointmentStatus.NO_SHOW)

    appointment.status = AppointmentStatus.NO_SHOW
    appointment.no_show_marked_at = utc_now()
    appointment.updated_at = utc_now()
    session.add(appointment)

    await _close_queue_entries(session, appointment.id, status=QueueStatus.LEFT_WITHOUT_BEING_SEEN)
    await session.flush()

    await event_bus.publish(
        AppointmentNoShow(
            hospital_id=appointment.hospital_id,
            actor_id=actor_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            scheduled_start=appointment.scheduled_start,
        ),
        session=session,
    )
    await session.refresh(appointment)
    return appointment


async def reschedule_appointment(
    session: AsyncSession,
    appointment: Appointment,
    *,
    new_start: datetime,
    reason: str | None = None,
    override_availability: bool = False,
    actor_id: uuid.UUID | None = None,
) -> Appointment:
    """Move a patient to a new slot, leaving a walkable chain.

    The old appointment becomes `RESCHEDULED` and points at the new one, rather
    than being cancelled and forgotten — otherwise a clinic that moves a patient
    three times looks identical to three cancellations plus three fresh
    bookings, and follow-up compliance reporting quietly lies.
    """
    assert_appointment_transition(appointment.status, AppointmentStatus.RESCHEDULED)

    replacement = await book_appointment(
        session,
        AppointmentBook(
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            scheduled_start=new_start,
            source=appointment.source,
            reason=appointment.reason,
            notes=appointment.notes,
            override_availability=override_availability,
        ),
        hospital_id=appointment.hospital_id,
        booked_by_id=actor_id,
    )

    appointment.status = AppointmentStatus.RESCHEDULED
    appointment.rescheduled_to_id = replacement.id
    appointment.cancellation_reason = reason
    appointment.updated_at = utc_now()
    session.add(appointment)
    await session.flush()
    await session.refresh(replacement)
    return replacement


async def mark_overdue_no_shows(
    session: AsyncSession, *, hospital_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Write off appointments nobody turned up for. Returns how many.

    A safety net for the click reception never got round to, run from a
    background job. Only `SCHEDULED` appointments are touched — someone who
    checked in and is sitting in the corridor is emphatically not a no-show,
    whatever the clock says.
    """
    moment = now or utc_now()
    cutoff = moment - NO_SHOW_GRACE

    stale = (
        (
            await session.execute(
                select(Appointment).where(
                    col(Appointment.hospital_id) == hospital_id,
                    col(Appointment.status) == AppointmentStatus.SCHEDULED,
                    col(Appointment.scheduled_start) < cutoff,
                    col(Appointment.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    for appointment in stale:
        await mark_no_show(session, appointment)

    if stale:
        logger.info("marked %d overdue appointment(s) as no-show", len(stale))
    return len(stale)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
async def _close_queue_entries(
    session: AsyncSession, appointment_id: uuid.UUID, *, status: QueueStatus
) -> None:
    entries = (
        (
            await session.execute(
                select(QueueEntry).where(
                    col(QueueEntry.appointment_id) == appointment_id,
                    col(QueueEntry.status).notin_(
                        [QueueStatus.COMPLETED, QueueStatus.LEFT_WITHOUT_BEING_SEEN]
                    ),
                    col(QueueEntry.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for entry in entries:
        entry.status = status
        entry.updated_at = utc_now()
        session.add(entry)


async def close_queue_for_appointment(
    session: AsyncSession, appointment_id: uuid.UUID, *, was_seen: bool
) -> int:
    """Take the token off the board when the visit it belongs to has ended.

    **The bug this fixes was that nothing did.** Queue entries were only ever
    closed by cancelling or no-showing the appointment, so a visit that
    *finished* — completed, admitted, or ended by one of §6's three unplanned
    exits — left its token sitting on the live board as `WAITING`. The OPD
    queue never emptied. It was invisible while the only screens that read it
    filtered by patient name, and became impossible to miss the day a deceased
    patient was showing first in line for a doctor.

    `was_seen` decides the closing status, and it comes from the encounter —
    specifically from whether a doctor started the consultation, which is
    exactly how `reporting` defines "seen". Taking it from the *token's* own
    status would have been wrong: in the real OPD flow the doctor starts the
    consultation on the chart, not on the queue board, so a token frequently
    reads `WAITING` right up to the moment the visit closes. Deriving from that
    would file every patient the doctor actually saw as "left without being
    seen" and corrupt the one figure on the dashboard that is supposed to mean
    a patient gave up and went home.

    Set directly rather than through `assert_queue_transition`, like the
    cancellation sweep above: this is a closure driven by something that
    happened elsewhere, not a receptionist moving a token along.
    """
    entries = (
        (
            await session.execute(
                select(QueueEntry).where(
                    col(QueueEntry.appointment_id) == appointment_id,
                    col(QueueEntry.status).notin_(list(TERMINAL_QUEUE_STATUSES)),
                    col(QueueEntry.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    closing = QueueStatus.COMPLETED if was_seen else QueueStatus.LEFT_WITHOUT_BEING_SEEN
    moment = utc_now()
    for entry in entries:
        entry.status = closing
        entry.updated_at = moment
        if closing is QueueStatus.COMPLETED:
            # When the token came off the line — what reception's "seen
            # today" list orders by and shows. Not a consultation duration.
            entry.completed_at = moment
        session.add(entry)

    return len(entries)


# The statuses that hold a place in the line. `IN_CONSULTATION` is with the
# doctor and has no place to hold; the two terminal statuses have left it.
_QUEUED_STATUSES: frozenset[QueueStatus] = frozenset(
    {QueueStatus.WAITING, QueueStatus.CALLED, QueueStatus.SKIPPED}
)


async def _live_queue_locked(
    session: AsyncSession, *, hospital_id: uuid.UUID, doctor_id: uuid.UUID, queue_date: date
) -> list[QueueEntry]:
    """One doctor's day, everyone still in line, in order — and locked.

    `FOR UPDATE` on the rows, so two receptionists dragging in the same queue
    at the same moment serialise rather than interleave: the second waits for
    the first's renumbering to commit and then re-slots against it. Without
    the lock both compute positions from the same stale snapshot and the
    result is two rows claiming the same rank.
    """
    rows = (
        (
            await session.execute(
                select(QueueEntry)
                .where(
                    col(QueueEntry.hospital_id) == hospital_id,
                    col(QueueEntry.doctor_id) == doctor_id,
                    col(QueueEntry.queue_date) == queue_date,
                    col(QueueEntry.deleted_at).is_(None),
                    col(QueueEntry.status).in_(list(_QUEUED_STATUSES)),
                )
                .order_by(col(QueueEntry.position), col(QueueEntry.checked_in_at))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _renumber(session: AsyncSession, ordered: list[QueueEntry]) -> None:
    """Write positions 1..n. Only rows whose rank changed are dirtied."""
    for index, row in enumerate(ordered, start=1):
        if row.position != index:
            row.position = index
            session.add(row)


async def _seed_position(
    session: AsyncSession,
    entry: QueueEntry,
    *,
    hospital_id: uuid.UUID,
    doctor_id: uuid.UUID,
    queue_date: date,
) -> None:
    """Give a newly queued token its place — the rule the old computed order
    used, made explicit: a normal patient joins the end; an emergency or
    priority patient goes in ahead of the first patient of a lower tier, and
    behind everyone of their own tier or higher who is already waiting.

    After reception has re-ranked by hand the tiers may be interleaved, and
    then "ahead of the first lower tier" is a judgement call. It is the
    conservative one: it never jumps a priority patient over somebody staff
    deliberately put at the top.
    """
    live = [
        row
        for row in await _live_queue_locked(
            session, hospital_id=hospital_id, doctor_id=doctor_id, queue_date=queue_date
        )
        if row.id != entry.id
    ]
    cut = next(
        (index for index, row in enumerate(live) if row.priority > entry.priority),
        None,
    )
    if cut is None:
        # The common case — every normal check-in — costs one row write.
        entry.position = (live[-1].position if live else 0) + 1
        session.add(entry)
        return
    live.insert(cut, entry)
    _renumber(session, live)


async def reorder_queue_entry(
    session: AsyncSession,
    entry: QueueEntry,
    *,
    after_entry_id: uuid.UUID | None,
) -> QueueEntry:
    """Move a token to a new place in its doctor's line.

    Expressed as "put it right after *that* token" (or at the top, for
    `None`) rather than "put it at index 3". An index is a statement about a
    list the receptionist saw a few seconds ago; by the time it arrives a new
    walk-in may have joined and index 3 means somebody else. A neighbour is a
    fact that survives the delay, and if the neighbour has since been called
    in or left, the move is refused rather than landing somewhere unintended.

    The whole line is renumbered 1..n afterwards. A doctor's day is at most a
    couple of hundred rows, so this is cheaper to reason about than gapped or
    fractional ranks that need a compaction job of their own.
    """
    if entry.status not in _QUEUED_STATUSES:
        raise ConflictError(
            f"A token that is {entry.status.value.lower().replace('_', ' ')} "
            "has no place in the line to change.",
            code="queue_entry_not_reorderable",
            details={"status": entry.status.value},
        )
    if after_entry_id == entry.id:
        raise ValidationError("A token cannot be placed after itself.", code="self_reference")

    live = await _live_queue_locked(
        session,
        hospital_id=entry.hospital_id,
        doctor_id=entry.doctor_id,
        queue_date=entry.queue_date,
    )
    by_id = {row.id: row for row in live}
    # `entry` came from a different SELECT; work on the locked instances.
    if entry.id not in by_id:
        raise ConflictError(
            "The token is no longer in the line.", code="queue_entry_not_reorderable"
        )
    moving = by_id[entry.id]

    remaining = [row for row in live if row.id != moving.id]
    if after_entry_id is None:
        remaining.insert(0, moving)
    else:
        anchor_row = by_id.get(after_entry_id)
        if anchor_row is None:
            raise NotFoundError(
                "The token to place this one after is not in the same line.",
                code="queue_anchor_not_found",
                details={"after_entry_id": str(after_entry_id)},
            )
        remaining.insert(remaining.index(anchor_row) + 1, moving)

    _renumber(session, remaining)
    moving.updated_at = utc_now()
    session.add(moving)
    await session.flush()
    await session.refresh(moving)
    return moving


async def check_in(
    session: AsyncSession,
    appointment: Appointment,
    *,
    priority: QueuePriority = QueuePriority.NORMAL,
    priority_reason: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> QueueEntry:
    """The patient is here. Issue a token and put them in the queue."""
    assert_appointment_transition(appointment.status, AppointmentStatus.CHECKED_IN)

    if priority is not QueuePriority.NORMAL and not priority_reason:
        # Jumping the queue in front of people who have been waiting is a
        # decision someone has to own by name.
        raise ValidationError(
            "A reason is required when giving a patient priority.",
            code="priority_reason_required",
        )

    # The hospital's date, not the server's. With `utc_now().date()` every
    # check-in after 18:30 IST was filed under yesterday — invisible to the
    # live queue board, and continuing yesterday's token series instead of
    # starting today's. See `tenancy.service.local_today`.
    today = await tenancy_service.local_today(session, appointment.hospital_id)
    token = await _next_counter(session, appointment.hospital_id, appointment.doctor_id, today)

    entry = QueueEntry(
        hospital_id=appointment.hospital_id,
        queue_date=today,
        token_number=token,
        doctor_id=appointment.doctor_id,
        patient_id=appointment.patient_id,
        appointment_id=appointment.id,
        department_id=appointment.department_id,
        priority=priority,
        priority_reason=priority_reason,
        checked_in_at=utc_now(),
    )
    session.add(entry)
    # Flushed first so the placement query below sees a row with an id to
    # exclude — and so a token exists before anything reads the line.
    await session.flush()
    await _seed_position(
        session,
        entry,
        hospital_id=appointment.hospital_id,
        doctor_id=appointment.doctor_id,
        queue_date=today,
    )

    appointment.status = AppointmentStatus.CHECKED_IN
    appointment.checked_in_at = utc_now()
    appointment.updated_at = utc_now()
    session.add(appointment)
    await session.flush()
    await session.refresh(entry)

    # `clinical` opens the Encounter from here (CLAUDE.md §6: REGISTERED).
    # Published rather than called so the OPD flow works before that module
    # exists, and so it can be extracted later.
    await event_bus.publish(
        PatientCheckedIn(
            hospital_id=appointment.hospital_id,
            actor_id=actor_id,
            appointment_id=appointment.id,
            queue_entry_id=entry.id,
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            department_id=appointment.department_id,
            token_number=token,
            queue_date=today,
        ),
        session=session,
    )
    return entry


async def link_encounter(
    session: AsyncSession, appointment: Appointment, encounter_id: uuid.UUID
) -> Appointment:
    """Record which Encounter `clinical` opened for this booking.

    `scheduling` owns this column, so `clinical` asks rather than writing into
    another module's table (CLAUDE.md §2). The alternative — letting the clinical
    module reach across — is the first crack in a boundary that exists so
    billing and the patient app can be lifted out later.
    """
    appointment.encounter_id = encounter_id
    appointment.updated_at = utc_now()
    session.add(appointment)
    await session.flush()
    return appointment


async def quick_opd(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    patient_id: uuid.UUID,
    doctor_id: uuid.UUID,
    reason: str | None = None,
    priority: QueuePriority = QueuePriority.NORMAL,
    priority_reason: str | None = None,
    source: AppointmentSource = AppointmentSource.WALK_IN,
    actor_id: uuid.UUID | None = None,
) -> tuple[Appointment, QueueEntry]:
    """One-click OPD (CLAUDE.md §7b): book, check in and tokenise in one call.

    The alternative is four screens for the single most repeated action of the
    day. `override_availability` is implied — a walk-in is standing at the
    counter now, and refusing them because the clinic technically started ten
    minutes ago would be absurd.
    """
    appointment = await book_appointment(
        session,
        AppointmentBook(
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_start=utc_now(),
            source=source,
            reason=reason,
            override_availability=True,
        ),
        hospital_id=hospital_id,
        booked_by_id=actor_id,
    )
    entry = await check_in(
        session,
        appointment,
        priority=priority,
        priority_reason=priority_reason,
        actor_id=actor_id,
    )
    return appointment, entry


async def get_queue(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    doctor_id: uuid.UUID | None = None,
    queue_date: date | None = None,
    active_only: bool = True,
) -> list[QueueEntry]:
    """The waiting-room board, in the order patients will actually be seen."""
    on_date = queue_date or await tenancy_service.local_today(session, hospital_id)
    filters: list[ColumnElement[bool]] = [
        col(QueueEntry.hospital_id) == hospital_id,
        col(QueueEntry.queue_date) == on_date,
        col(QueueEntry.deleted_at).is_(None),
    ]
    if doctor_id is not None:
        filters.append(col(QueueEntry.doctor_id) == doctor_id)
    if active_only:
        filters.append(
            col(QueueEntry.status).notin_(
                [QueueStatus.COMPLETED, QueueStatus.LEFT_WITHOUT_BEING_SEEN]
            )
        )

    rows = (
        (
            await session.execute(
                select(QueueEntry)
                .where(*filters)
                # The stored rank — seeded tier-then-arrival at check-in and
                # changed only by reception's hand (`reorder_queue_entry`).
                # Arrival breaks the rare tie of two simultaneous check-ins.
                .order_by(col(QueueEntry.position), col(QueueEntry.checked_in_at))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def count_patients_ahead(session: AsyncSession, entry: QueueEntry) -> int:
    """How many people this patient is actually behind — what reception tells them."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(QueueEntry)
                .where(
                    col(QueueEntry.doctor_id) == entry.doctor_id,
                    col(QueueEntry.queue_date) == entry.queue_date,
                    col(QueueEntry.deleted_at).is_(None),
                    col(QueueEntry.status).notin_(
                        [QueueStatus.COMPLETED, QueueStatus.LEFT_WITHOUT_BEING_SEEN]
                    ),
                    (col(QueueEntry.position) < entry.position)
                    | (
                        (col(QueueEntry.position) == entry.position)
                        & (col(QueueEntry.checked_in_at) < entry.checked_in_at)
                    ),
                )
            )
        ).scalar_one()
    )


async def get_queue_entry(
    session: AsyncSession, entry_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> QueueEntry:
    entry = (
        (
            await session.execute(
                select(QueueEntry).where(
                    col(QueueEntry.id) == entry_id,
                    col(QueueEntry.hospital_id) == hospital_id,
                    col(QueueEntry.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if entry is None:
        raise NotFoundError("Queue entry not found.", code="queue_entry_not_found")
    return entry


async def call_next_patient(
    session: AsyncSession, *, hospital_id: uuid.UUID, doctor_id: uuid.UUID
) -> QueueEntry | None:
    """Call the next waiting patient. Returns None when the queue is empty."""
    queue = await get_queue(session, hospital_id=hospital_id, doctor_id=doctor_id)
    waiting = [entry for entry in queue if entry.status is QueueStatus.WAITING]
    if not waiting:
        # Nobody new; a skipped patient who has come back is next in line.
        waiting = [entry for entry in queue if entry.status is QueueStatus.SKIPPED]
    if not waiting:
        return None

    entry = waiting[0]
    assert_queue_transition(entry.status, QueueStatus.CALLED)
    entry.status = QueueStatus.CALLED
    entry.called_at = utc_now()
    entry.updated_at = utc_now()
    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return entry


async def skip_token(
    session: AsyncSession, entry: QueueEntry, *, reason: str | None = None
) -> QueueEntry:
    """Called and did not come forward.

    Recoverable on purpose: a patient in the toilet is not a patient who has
    gone home, and the count is left for staff to judge rather than a threshold
    written into the code.
    """
    assert_queue_transition(entry.status, QueueStatus.SKIPPED)
    entry.status = QueueStatus.SKIPPED
    entry.skip_count += 1
    if reason:
        entry.priority_reason = reason
    entry.updated_at = utc_now()
    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return entry


async def start_consultation(
    session: AsyncSession,
    entry: QueueEntry,
    *,
    actor_id: uuid.UUID | None = None,
) -> QueueEntry:
    assert_queue_transition(entry.status, QueueStatus.IN_CONSULTATION)

    entry.status = QueueStatus.IN_CONSULTATION
    entry.started_at = utc_now()
    entry.updated_at = utc_now()
    session.add(entry)

    if entry.appointment_id is not None:
        appointment = await get_appointment(
            session, entry.appointment_id, hospital_id=entry.hospital_id
        )
        assert_appointment_transition(appointment.status, AppointmentStatus.IN_CONSULTATION)
        appointment.status = AppointmentStatus.IN_CONSULTATION
        appointment.started_at = utc_now()
        appointment.updated_at = utc_now()
        session.add(appointment)

        await event_bus.publish(
            ConsultationStarted(
                hospital_id=entry.hospital_id,
                actor_id=actor_id,
                appointment_id=appointment.id,
                queue_entry_id=entry.id,
                patient_id=entry.patient_id,
                doctor_id=entry.doctor_id,
            ),
            session=session,
        )

    await session.flush()
    await session.refresh(entry)
    return entry


async def complete_consultation(
    session: AsyncSession,
    entry: QueueEntry,
    *,
    actor_id: uuid.UUID | None = None,
) -> QueueEntry:
    """Mark the visit finished.

    Note what this does *not* do: close the Encounter. That belongs to
    `clinical`'s state machine, which has to weigh pending labs, medicines and
    charges before a visit is really over (CLAUDE.md §6). This module only
    reports that the doctor is free again.
    """
    assert_queue_transition(entry.status, QueueStatus.COMPLETED)

    entry.status = QueueStatus.COMPLETED
    entry.completed_at = utc_now()
    entry.updated_at = utc_now()
    session.add(entry)

    if entry.appointment_id is not None:
        appointment = await get_appointment(
            session, entry.appointment_id, hospital_id=entry.hospital_id
        )
        assert_appointment_transition(appointment.status, AppointmentStatus.COMPLETED)
        appointment.status = AppointmentStatus.COMPLETED
        appointment.completed_at = utc_now()
        appointment.updated_at = utc_now()
        session.add(appointment)

        started = entry.started_at or entry.checked_in_at
        minutes = max(0, int((utc_now() - started).total_seconds() // 60))
        await event_bus.publish(
            ConsultationCompleted(
                hospital_id=entry.hospital_id,
                actor_id=actor_id,
                appointment_id=appointment.id,
                patient_id=entry.patient_id,
                doctor_id=entry.doctor_id,
                duration_minutes=minutes,
            ),
            session=session,
        )

    await session.flush()
    await session.refresh(entry)
    return entry


async def mark_left_without_being_seen(session: AsyncSession, entry: QueueEntry) -> QueueEntry:
    assert_queue_transition(entry.status, QueueStatus.LEFT_WITHOUT_BEING_SEEN)
    entry.status = QueueStatus.LEFT_WITHOUT_BEING_SEEN
    entry.updated_at = utc_now()
    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return entry


# The legal route from each live status to COMPLETED, walked one hop at a
# time so every step is one `transitions.py` already allows. Reception marks a
# patient seen *after* the doctor has seen them, so the intermediate states
# happened in the room even if nobody told the system; nothing is persisted
# between hops.
_QUEUE_SEEN_ROUTE: dict[QueueStatus, tuple[QueueStatus, ...]] = {
    QueueStatus.WAITING: (QueueStatus.IN_CONSULTATION, QueueStatus.COMPLETED),
    QueueStatus.CALLED: (QueueStatus.IN_CONSULTATION, QueueStatus.COMPLETED),
    QueueStatus.SKIPPED: (
        QueueStatus.CALLED,
        QueueStatus.IN_CONSULTATION,
        QueueStatus.COMPLETED,
    ),
    QueueStatus.IN_CONSULTATION: (QueueStatus.COMPLETED,),
}

_APPOINTMENT_SEEN_ROUTE: dict[AppointmentStatus, tuple[AppointmentStatus, ...]] = {
    AppointmentStatus.CHECKED_IN: (
        AppointmentStatus.IN_CONSULTATION,
        AppointmentStatus.COMPLETED,
    ),
    AppointmentStatus.IN_CONSULTATION: (AppointmentStatus.COMPLETED,),
}


async def mark_seen(session: AsyncSession, entry: QueueEntry) -> QueueEntry:
    """Reception records that the doctor has seen this patient.

    For the clinic whose doctor does not touch the system — or does, but not
    until later — reception is the one who knows the patient came out of the
    room. The token and the booking both close as COMPLETED, exactly as when
    the doctor finishes on the queue; `call_next` stops offering them and the
    board strikes them through.

    Two things this deliberately does *not* invent:

    * **A start time.** `started_at` stays empty if nobody recorded one. A
      timestamp taken at the moment reception clicked would claim the
      consultation began then, and quietly inflate every wait-time figure.
    * **`ConsultationCompleted`.** Its `duration_minutes` is exactly the fact
      reception does not have, and a zero would read as a real measurement to
      any subscriber that sums it.

    The chart is `clinical`'s to move; the caller does that through its
    service (CLAUDE.md §2) so the visit is not auto-closed as a no-show.
    """
    route = _QUEUE_SEEN_ROUTE.get(entry.status)
    if route is None:
        raise ConflictError(
            f"A token that is {entry.status.value.lower().replace('_', ' ')} "
            "cannot be marked as seen.",
            code="queue_entry_not_markable",
            details={"status": entry.status.value},
        )

    moment = utc_now()
    current = entry.status
    for hop in route:
        assert_queue_transition(current, hop)
        current = hop
    entry.status = QueueStatus.COMPLETED
    entry.completed_at = moment
    entry.updated_at = moment
    session.add(entry)

    if entry.appointment_id is not None:
        appointment = await get_appointment(
            session, entry.appointment_id, hospital_id=entry.hospital_id
        )
        appointment_route = _APPOINTMENT_SEEN_ROUTE.get(appointment.status)
        if appointment_route is None:
            # Surfaces the state machine's own wording ("already completed",
            # "cancelled cannot become completed") rather than a new one.
            assert_appointment_transition(appointment.status, AppointmentStatus.COMPLETED)
        else:
            step = appointment.status
            for hop_status in appointment_route:
                assert_appointment_transition(step, hop_status)
                step = hop_status
        appointment.status = AppointmentStatus.COMPLETED
        appointment.completed_at = moment
        appointment.updated_at = moment
        session.add(appointment)

    await session.flush()
    await session.refresh(entry)
    return entry


# The statuses a token can be moved from. Not `CALLED`: a doctor who has just
# called a number out is expecting that patient to walk in, and moving them
# mid-call is how two doctors end up looking for the same person. Reception
# waits for the skip, then moves them.
REASSIGNABLE_QUEUE_STATUSES: frozenset[QueueStatus] = frozenset(
    {QueueStatus.WAITING, QueueStatus.SKIPPED}
)


async def reassign_queue_entry(
    session: AsyncSession,
    entry: QueueEntry,
    *,
    to_doctor_id: uuid.UUID,
    reason: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> QueueEntry:
    """Move a waiting patient to another doctor's queue.

    Reception's load-balancing lever: when one clinic has fourteen waiting and
    the one next door has none, the patient standing at the counter should not
    have to know that. The token row is updated in place rather than closed
    and re-issued, so the appointment and chart it is linked to stay linked
    and the patient keeps their place in time — `checked_in_at` is untouched,
    because they *have* been waiting, and a move must never send someone to
    the back of a queue they did not choose.

    What does change: the token number, because tokens are a per-doctor series
    and number 7 already means someone else in the new clinic; and the
    appointment's doctor, so the booking and the visit agree on who saw them.
    The encounter's doctor is the caller's to update through `clinical` — this
    module does not know a chart exists.
    """
    if entry.status not in REASSIGNABLE_QUEUE_STATUSES:
        raise ConflictError(
            f"A token that is {entry.status.value.lower().replace('_', ' ')} cannot be moved.",
            code="queue_entry_not_movable",
            details={
                "status": entry.status.value,
                "movable": sorted(status.value for status in REASSIGNABLE_QUEUE_STATUSES),
            },
        )
    if to_doctor_id == entry.doctor_id:
        raise ValidationError("The patient is already in that doctor's queue.", code="same_doctor")

    doctor = await get_doctor(session, to_doctor_id, hospital_id=entry.hospital_id)
    if not doctor.is_active or not doctor.is_accepting_appointments:
        raise ConflictError(
            f"{doctor.display_name} is not accepting patients.",
            code="doctor_not_accepting",
        )

    from_doctor_id = entry.doctor_id
    from_token = entry.token_number
    moment = utc_now()

    # Allocate the new number *before* touching the row. `_next_counter`
    # flushes, and a half-updated entry — new doctor, old token — collides
    # with whoever already holds that number in the new clinic
    # (`uq_queue_entries_token_live`). Doctor and token change together.
    new_token = await _next_counter(session, entry.hospital_id, doctor.id, entry.queue_date)
    entry.doctor_id = doctor.id
    entry.department_id = doctor.department_id
    entry.token_number = new_token
    session.add(entry)
    await session.flush()
    # A place in the new line, by the same rule as a fresh check-in. Their
    # rank in the old line means nothing here.
    await _seed_position(
        session,
        entry,
        hospital_id=entry.hospital_id,
        doctor_id=doctor.id,
        queue_date=entry.queue_date,
    )
    if entry.status is QueueStatus.SKIPPED:
        # Skipped by the old doctor means nothing to the new one; they go back
        # to plain waiting (a legal transition — see `transitions.py`).
        assert_queue_transition(entry.status, QueueStatus.WAITING)
        entry.status = QueueStatus.WAITING
    entry.called_at = None
    entry.updated_at = moment
    session.add(entry)

    if entry.appointment_id is not None:
        appointment = await get_appointment(
            session, entry.appointment_id, hospital_id=entry.hospital_id
        )
        appointment.doctor_id = doctor.id
        appointment.department_id = doctor.department_id
        appointment.updated_at = moment
        session.add(appointment)

    await session.flush()
    await session.refresh(entry)

    await event_bus.publish(
        QueueEntryReassigned(
            hospital_id=entry.hospital_id,
            actor_id=actor_id,
            queue_entry_id=entry.id,
            appointment_id=entry.appointment_id,
            patient_id=entry.patient_id,
            from_doctor_id=from_doctor_id,
            to_doctor_id=doctor.id,
            from_token_number=from_token,
            to_token_number=entry.token_number,
            queue_date=entry.queue_date,
            reason=reason,
        ),
        session=session,
    )
    return entry


# ---------------------------------------------------------------------------
# Universal search (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
# Long enough to be worth a query, short enough to be a lookup rather than a
# report. Someone who wants to browse wants the patient index instead.
MAX_SEARCH_HITS = 8

# A token is called out loud across a waiting room, so it is small and it
# restarts daily. Four digits is already implausible; beyond that the digits
# are a phone number or a UHID, and `patients.search_patients` owns those.
_MAX_TOKEN_DIGITS = 4


async def search(
    session: AsyncSession,
    query: str,
    *,
    hospital_id: uuid.UUID,
    limit: int = MAX_SEARCH_HITS,
    can_open_charts: bool = False,
) -> list[SearchHit]:
    """One box that resolves anything a member of staff is holding.

    CLAUDE.md §7b asks for a single field that resolves "UHID, name, mobile,
    token, doctor, or appointment number". Three of those live in `patients`,
    three in this module, and one visit number in `clinical` — which is why
    this is a **server-side** composition rather than a frontend component
    fanning out to three endpoints and merging the answers. A client doing that
    would re-rank the results by string distance and push an exact UHID match
    below a near miss, and every new client would get the merge subtly wrong in
    its own way.

    It lives in `scheduling` for one reason and it is not a semantic one:
    `scheduling` already depends on both `patients` and `clinical`
    (CLAUDE.md §2), so composing them here adds no dependency edge that the
    module graph did not already have. Putting it in `patients` — the more
    obvious home — would have `patients` importing two modules downstream of
    it and invert the graph.

    ## Ranking

    Exact identifiers first, in the order they are unambiguous: a visit number,
    then a booking number, then today's token. Then patients, whose own search
    already ranks UHID above phone above name. Doctors last — somebody typing a
    doctor's name usually wants a clinic, not a record, and it is the weakest
    signal in the box.

    Probes are gated by the *shape* of the term rather than all being run on
    every keystroke: a term with no digits cannot be a token, and a term with
    no separator cannot be one of our reference numbers.

    `can_open_charts` is the caller's `note:read`, passed in rather than looked
    up: this module has no business asking the RBAC tables what a user may do,
    and the router already knows. It decides whether an open visit is offered
    as somewhere to *go* — see section 4b. Defaulting to `False` keeps the
    conservative answer the default for any future caller that forgets.
    """
    # Imported here rather than at module scope. `scheduling.service` is
    # imported by `clinical`'s router at start-up, and a top-level import of
    # `clinical.service` would close a cycle through it.
    from app.modules.clinical import service as clinical_service
    from app.modules.patients import service as patients_service
    from app.modules.patients.schemas import PatientRead

    term = query.strip()
    if not term:
        return []

    # A single character is normally too little to search on — one letter
    # matches most of the hospital and returns a page of noise. One *digit* is
    # different: it is token 4, and the first nine patients of every clinic
    # have a single-digit token. Refusing them would break the lookup for the
    # first hour of every morning, which is the busiest hour.
    if len(term) < 2 and not term.isdigit():
        return []

    hits: list[SearchHit] = []
    seen: set[tuple[str, uuid.UUID]] = set()

    def add(hit: SearchHit) -> None:
        """Append unless this exact record is already on the list.

        A patient can legitimately match twice — once by UHID and once as the
        owner of today's token — and showing them twice makes a short list look
        broken. The first (better-ranked) hit wins.
        """
        anchor = hit.encounter_id or hit.appointment_id or hit.doctor_id or hit.patient_id
        if anchor is None:
            return
        key = (hit.kind.value, anchor)
        if key in seen:
            return
        seen.add(key)
        hits.append(hit)

    digits = term if term.isdigit() else ""
    has_separator = "-" in term
    has_letters = any(character.isalpha() for character in term)

    # --- 1. a visit number off a discharge slip ---------------------------
    if has_separator:
        encounter = await clinical_service.get_encounter_by_number(
            session, term, hospital_id=hospital_id
        )
        if encounter is not None:
            patient = (
                await patients_service.get_patients_by_ids(
                    session, [encounter.patient_id], hospital_id=hospital_id
                )
            ).get(encounter.patient_id)
            view = PatientRead.model_validate(patient) if patient is not None else None
            add(
                SearchHit(
                    kind=SearchHitKind.ENCOUNTER,
                    title=view.full_name if view else encounter.encounter_number,
                    subtitle=f"{encounter.encounter_number} · {encounter.status.value}",
                    patient_id=encounter.patient_id,
                    patient_uhid=view.uhid if view else None,
                    encounter_id=encounter.id,
                    is_deceased=bool(view and view.is_deceased),
                )
            )

    # --- 2. a booking number off an appointment card ----------------------
    if has_separator:
        appointment = await get_appointment_by_number(session, term, hospital_id=hospital_id)
        if appointment is not None:
            patient = (
                await patients_service.get_patients_by_ids(
                    session, [appointment.patient_id], hospital_id=hospital_id
                )
            ).get(appointment.patient_id)
            view = PatientRead.model_validate(patient) if patient is not None else None
            add(
                SearchHit(
                    kind=SearchHitKind.APPOINTMENT,
                    title=view.full_name if view else appointment.appointment_number,
                    subtitle=(f"{appointment.appointment_number} · {appointment.status.value}"),
                    patient_id=appointment.patient_id,
                    patient_uhid=view.uhid if view else None,
                    appointment_id=appointment.id,
                    doctor_id=appointment.doctor_id,
                    is_deceased=bool(view and view.is_deceased),
                )
            )

    # --- 3. a token number, called out this morning -----------------------
    if digits and len(digits) <= _MAX_TOKEN_DIGITS:
        entries = await _queue_entries_by_token(
            session, int(digits), hospital_id=hospital_id, limit=limit
        )
        patients = await patients_service.get_patients_by_ids(
            session, [entry.patient_id for entry in entries], hospital_id=hospital_id
        )
        encounters = await clinical_service.get_encounters_by_appointments(
            session,
            [entry.appointment_id for entry in entries if entry.appointment_id is not None],
            hospital_id=hospital_id,
        )
        for entry in entries:
            patient = patients.get(entry.patient_id)
            view = PatientRead.model_validate(patient) if patient is not None else None
            encounter = (
                encounters.get(entry.appointment_id) if entry.appointment_id is not None else None
            )
            add(
                SearchHit(
                    kind=SearchHitKind.TOKEN,
                    title=view.full_name if view else f"Token {entry.token_number}",
                    subtitle=f"Token {entry.token_number} · {entry.status.value}",
                    patient_id=entry.patient_id,
                    patient_uhid=view.uhid if view else None,
                    encounter_id=encounter.id if encounter is not None else None,
                    appointment_id=entry.appointment_id,
                    doctor_id=entry.doctor_id,
                    is_deceased=bool(view and view.is_deceased),
                )
            )

    # --- 4. the common case: a patient by UHID, mobile or name ------------
    found, _ = await patients_service.search_patients(
        session, term, PageParams(limit=limit), hospital_id=hospital_id
    )
    for patient in found:
        view = PatientRead.model_validate(patient)
        add(
            SearchHit(
                kind=SearchHitKind.PATIENT,
                title=view.full_name,
                subtitle=f"{view.uhid} · {view.phone}",
                patient_id=view.id,
                patient_uhid=view.uhid,
                is_deceased=view.is_deceased,
            )
        )

    # --- 4b. is that patient already in the middle of a visit? ------------
    #
    # The error this prevents is registering somebody twice. A receptionist
    # scans a card, sees a patient record, and opens a fresh visit — while the
    # patient is already halfway through one, sitting in a corridor with a
    # token. A split visit is a split bill and a chart in two halves.
    #
    # It lands in two different places depending on what the caller can do,
    # because the *information* is useful to everyone and the *destination* is
    # not:
    #
    #   * On the patient's own hit, always, as "in a visit right now". That is
    #     the part that stops the double registration, and it costs reception
    #     no navigation at all.
    #   * As a separate hit that opens the chart — only for callers who can
    #     read one. Reception cannot (`note:read` gates the chart), and this
    #     codebase's standing rule is that a link which 403s is worse than a
    #     missing one: staff who meet a few of those stop trusting the screen.
    #
    # Ranked below the patient record either way, so pressing Enter on the
    # first hit stays safe for whoever is holding the scanner.
    #
    # One query for the whole page, through `clinical.service` (CLAUDE.md §2).
    if found:
        open_visits = await clinical_service.get_open_encounters_by_patients(
            session, [patient.id for patient in found], hospital_id=hospital_id
        )
        for patient in found:
            encounter = open_visits.get(patient.id)
            if encounter is None:
                continue

            view = PatientRead.model_validate(patient)
            for hit in hits:
                if hit.kind is SearchHitKind.PATIENT and hit.patient_id == view.id:
                    hit.subtitle = f"{hit.subtitle} · {encounter.status.value}"
                    break

            if not can_open_charts:
                continue

            add(
                SearchHit(
                    kind=SearchHitKind.ENCOUNTER,
                    title=view.full_name,
                    subtitle=f"{encounter.encounter_number} · {encounter.status.value}",
                    patient_id=view.id,
                    patient_uhid=view.uhid,
                    encounter_id=encounter.id,
                    is_deceased=view.is_deceased,
                )
            )

    # --- 5. a doctor, weakest signal, last --------------------------------
    if has_letters:
        for doctor in await _doctors_by_name(session, term, hospital_id=hospital_id, limit=limit):
            add(
                SearchHit(
                    kind=SearchHitKind.DOCTOR,
                    title=doctor.display_name,
                    subtitle=doctor.specialty,
                    doctor_id=doctor.id,
                )
            )

    return hits[:limit]


async def get_appointment_by_number(
    session: AsyncSession, appointment_number: str, *, hospital_id: uuid.UUID
) -> Appointment | None:
    """A booking by the number on the patient's card, or None.

    Returns rather than raises: its caller is a search box, where "no such
    booking" is an ordinary result of typing.
    """
    number = appointment_number.strip().upper()
    if not number:
        return None

    return (
        (
            await session.execute(
                select(Appointment).where(
                    col(Appointment.appointment_number) == number,
                    col(Appointment.hospital_id) == hospital_id,
                    col(Appointment.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def _queue_entries_by_token(
    session: AsyncSession, token_number: int, *, hospital_id: uuid.UUID, limit: int
) -> list[QueueEntry]:
    """Today's tokens with this number — one per doctor, at most.

    Scoped to today because a token number is only unique per doctor per day;
    yesterday's 14 is not the patient standing at the desk. "Today" is the
    hospital's, so an evening search finds the evening's tokens.
    """
    today = await tenancy_service.local_today(session, hospital_id)
    rows = (
        (
            await session.execute(
                select(QueueEntry)
                .where(
                    col(QueueEntry.hospital_id) == hospital_id,
                    col(QueueEntry.queue_date) == today,
                    col(QueueEntry.token_number) == token_number,
                    col(QueueEntry.deleted_at).is_(None),
                )
                .order_by(col(QueueEntry.checked_in_at))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _doctors_by_name(
    session: AsyncSession, term: str, *, hospital_id: uuid.UUID, limit: int
) -> list[Doctor]:
    rows = (
        (
            await session.execute(
                select(Doctor)
                .where(
                    col(Doctor.hospital_id) == hospital_id,
                    col(Doctor.deleted_at).is_(None),
                    col(Doctor.is_active).is_(True),
                    col(Doctor.display_name).ilike(f"%{term}%"),
                )
                .order_by(col(Doctor.display_name))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
