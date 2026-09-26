"""Scheduling service: availability, booking, the queue, and the OPD day."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.events import event_bus
from app.core.exceptions import (
    ConflictError,
    IllegalStateTransitionError,
    NotFoundError,
    ValidationError,
)
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.scheduling import service
from app.modules.scheduling.events import (
    ConsultationCompleted,
    PatientCheckedIn,
    QueueEntryReassigned,
)
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    QueueEntry,
    QueuePriority,
    QueueStatus,
    Weekday,
)
from app.modules.scheduling.schemas import (
    AppointmentBook,
    AvailabilityExceptionCreate,
    DoctorAvailabilityCreate,
    DoctorCreate,
    DoctorUpdate,
)
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


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
async def doctor(session: AsyncSession, tenant: Hospital, doctor_user: User) -> Doctor:
    record = await service.create_doctor(
        session,
        DoctorCreate(user_id=doctor_user.id, specialty="General Medicine"),
        hospital_id=tenant.id,
        display_name="Dr Rao",
    )
    await session.commit()
    return record


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


def _next_weekday(weekday: Weekday) -> datetime:
    """The next occurrence of a weekday, at 10:00 UTC."""
    today = utc_now().date()
    ahead = (weekday.value - today.weekday()) % 7 or 7
    return datetime.combine(today + timedelta(days=ahead), time(10, 0), tzinfo=UTC)


class TestAvailability:
    async def test_a_weekly_session_produces_slots(
        self, session: AsyncSession, doctor: Doctor
    ) -> None:
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY,
                start_time=time(10, 0),
                end_time=time(13, 0),
                slot_minutes=15,
            ),
        )
        await session.commit()

        slots = await service.get_available_slots(
            session, doctor, _next_weekday(Weekday.MONDAY).date()
        )
        assert len(slots) == 12  # three hours in quarter-hour slots
        assert all(slot.is_available for slot in slots)

    async def test_overlapping_sessions_are_refused(
        self, session: AsyncSession, doctor: Doctor
    ) -> None:
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY, start_time=time(10, 0), end_time=time(13, 0)
            ),
        )
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service.set_availability(
                session,
                doctor,
                DoctorAvailabilityCreate(
                    weekday=Weekday.MONDAY, start_time=time(12, 0), end_time=time(15, 0)
                ),
            )
        assert excinfo.value.code == "availability_overlap"

    async def test_adjacent_sessions_are_fine(self, session: AsyncSession, doctor: Doctor) -> None:
        """Morning and evening clinics touch at the boundary; that is not a clash."""
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY, start_time=time(10, 0), end_time=time(13, 0)
            ),
        )
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY, start_time=time(13, 0), end_time=time(16, 0)
            ),
        )
        await session.commit()
        assert len(await service.list_availability(session, doctor.id)) == 2

    async def test_leave_removes_a_day_entirely(
        self, session: AsyncSession, doctor: Doctor
    ) -> None:
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY, start_time=time(10, 0), end_time=time(13, 0)
            ),
        )
        monday = _next_weekday(Weekday.MONDAY).date()
        await service.add_availability_exception(
            session,
            doctor,
            AvailabilityExceptionCreate(
                exception_date=monday, is_available=False, reason="Conference"
            ),
        )
        await session.commit()

        assert await service.get_available_slots(session, doctor, monday) == []

    async def test_an_extra_clinic_overrides_the_weekly_rule(
        self, session: AsyncSession, doctor: Doctor
    ) -> None:
        sunday = _next_weekday(Weekday.SUNDAY).date()
        await service.add_availability_exception(
            session,
            doctor,
            AvailabilityExceptionCreate(
                exception_date=sunday,
                is_available=True,
                start_time=time(9, 0),
                end_time=time(11, 0),
                slot_minutes=30,
                reason="Camp",
            ),
        )
        await session.commit()

        slots = await service.get_available_slots(session, doctor, sunday)
        assert len(slots) == 4

    async def test_a_session_outside_its_validity_window_yields_nothing(
        self, session: AsyncSession, doctor: Doctor
    ) -> None:
        """A retired clinic is closed by date, not deleted, so history still reads."""
        monday = _next_weekday(Weekday.MONDAY).date()
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY,
                start_time=time(10, 0),
                end_time=time(13, 0),
                valid_until=monday - timedelta(days=1),
            ),
        )
        await session.commit()

        assert await service.get_available_slots(session, doctor, monday) == []


class TestBooking:
    async def test_booking_inside_clinic_hours(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY, start_time=time(10, 0), end_time=time(13, 0)
            ),
        )
        await session.commit()

        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=_next_weekday(Weekday.MONDAY),
            ),
            hospital_id=tenant.id,
        )
        await session.commit()

        assert appointment.appointment_number.startswith("APT-")
        assert appointment.status is AppointmentStatus.SCHEDULED

    async def test_booking_outside_clinic_hours_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        with pytest.raises(ConflictError) as excinfo:
            await service.book_appointment(
                session,
                AppointmentBook(
                    patient_id=patient.id,
                    doctor_id=doctor.id,
                    scheduled_start=_next_weekday(Weekday.MONDAY),
                ),
                hospital_id=tenant.id,
            )
        assert excinfo.value.code == "outside_availability"

    async def test_the_override_exists_for_the_consultant_who_said_yes(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=_next_weekday(Weekday.MONDAY),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()
        assert appointment.id is not None

    async def test_double_booking_the_same_slot_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        slot = _next_weekday(Weekday.MONDAY)
        await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=slot,
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service.book_appointment(
                session,
                AppointmentBook(
                    patient_id=patient.id,
                    doctor_id=doctor.id,
                    scheduled_start=slot,
                    override_availability=True,
                ),
                hospital_id=tenant.id,
            )
        assert excinfo.value.code == "slot_taken"

    async def test_a_cancelled_slot_becomes_bookable_again(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        slot = _next_weekday(Weekday.MONDAY)
        first = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=slot,
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()
        await service.cancel_appointment(session, first, reason="Patient rang to cancel")
        await session.commit()

        second = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=slot,
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()
        assert second.id != first.id

    async def test_a_doctor_not_accepting_appointments_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        doctor.is_accepting_appointments = False
        session.add(doctor)
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service.book_appointment(
                session,
                AppointmentBook(
                    patient_id=patient.id,
                    doctor_id=doctor.id,
                    scheduled_start=_next_weekday(Weekday.MONDAY),
                    override_availability=True,
                ),
                hospital_id=tenant.id,
            )
        assert excinfo.value.code == "doctor_not_accepting"

    async def test_a_booked_slot_shows_as_unavailable(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        await service.set_availability(
            session,
            doctor,
            DoctorAvailabilityCreate(
                weekday=Weekday.MONDAY,
                start_time=time(10, 0),
                end_time=time(11, 0),
                slot_minutes=15,
            ),
        )
        slot = _next_weekday(Weekday.MONDAY)
        await service.book_appointment(
            session,
            AppointmentBook(patient_id=patient.id, doctor_id=doctor.id, scheduled_start=slot),
            hospital_id=tenant.id,
        )
        await session.commit()

        slots = await service.get_available_slots(session, doctor, slot.date())
        taken = [item for item in slots if not item.is_available]
        assert len(taken) == 1
        assert taken[0].start == slot


class TestCancellationAndNoShow:
    async def test_cancelling_records_who_and_why(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=_next_weekday(Weekday.MONDAY),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()

        cancelled = await service.cancel_appointment(
            session, appointment, reason="Doctor called to theatre", cancelled_by_patient=False
        )
        await session.commit()

        assert cancelled.status is AppointmentStatus.CANCELLED
        assert cancelled.cancellation_reason == "Doctor called to theatre"
        # Who cancelled decides who owes whom a new slot.
        assert cancelled.cancelled_by_patient is False

    async def test_cancelling_twice_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=_next_weekday(Weekday.MONDAY),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await service.cancel_appointment(session, appointment, reason="Changed mind")
        await session.commit()

        with pytest.raises(IllegalStateTransitionError):
            await service.cancel_appointment(session, appointment, reason="Again")

    async def test_overdue_appointments_are_written_off(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        stale = utc_now() - timedelta(hours=3)
        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=stale,
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()

        marked = await service.mark_overdue_no_shows(session, hospital_id=tenant.id)
        await session.commit()

        assert marked == 1
        await session.refresh(appointment)
        assert appointment.status is AppointmentStatus.NO_SHOW

    async def test_a_checked_in_patient_is_never_auto_marked_absent(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        """They are sitting in the corridor. The clock is not the authority."""
        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=utc_now() - timedelta(hours=3),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await service.check_in(session, appointment)
        await session.commit()

        assert await service.mark_overdue_no_shows(session, hospital_id=tenant.id) == 0
        await session.refresh(appointment)
        assert appointment.status is AppointmentStatus.CHECKED_IN

    async def test_a_recent_appointment_is_left_alone(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        """Indian OPDs run late; the grace period is generous on purpose."""
        await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=utc_now() - timedelta(minutes=20),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()

        assert await service.mark_overdue_no_shows(session, hospital_id=tenant.id) == 0


class TestReschedule:
    async def test_rescheduling_leaves_a_walkable_chain(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        """Three moves must not look like three cancellations plus three bookings."""
        original = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=_next_weekday(Weekday.MONDAY),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        await session.commit()

        replacement = await service.reschedule_appointment(
            session,
            original,
            new_start=_next_weekday(Weekday.TUESDAY),
            reason="Patient asked for a later day",
            override_availability=True,
        )
        await session.commit()

        assert original.status is AppointmentStatus.RESCHEDULED
        assert original.rescheduled_to_id == replacement.id
        assert replacement.status is AppointmentStatus.SCHEDULED


class TestQueue:
    async def test_check_in_issues_a_token_starting_at_one(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=utc_now(),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        entry = await service.check_in(session, appointment)
        await session.commit()

        assert entry.token_number == 1
        assert entry.status is QueueStatus.WAITING
        assert appointment.status is AppointmentStatus.CHECKED_IN

    async def test_quick_opd_does_everything_in_one_call(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        """CLAUDE.md §7b: the most repeated action of the day is one step."""
        appointment, entry = await service.quick_opd(
            session,
            hospital_id=tenant.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            reason="Fever",
        )
        await session.commit()

        assert appointment.status is AppointmentStatus.CHECKED_IN
        assert entry.status is QueueStatus.WAITING
        assert entry.token_number == 1

    async def test_check_in_publishes_the_event_clinical_will_listen_for(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        """`clinical` opens the Encounter from this, without scheduling knowing."""
        seen: list[PatientCheckedIn] = []
        event_bus.subscribe(PatientCheckedIn, lambda event: seen.append(event))
        try:
            _, entry = await service.quick_opd(
                session,
                hospital_id=tenant.id,
                patient_id=patient.id,
                doctor_id=doctor.id,
            )
            await session.commit()
        finally:
            event_bus.clear()

        assert len(seen) == 1
        assert seen[0].patient_id == patient.id
        assert seen[0].token_number == entry.token_number

    async def test_tokens_increment_per_doctor_per_day(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        tokens = []
        for index in range(3):
            record, _ = await patients_service.register_patient(
                session,
                PatientRegister(
                    full_name=f"Patient {index}",
                    phone=f"90000000{index:02d}",
                    gender=Gender.MALE,
                    age_years=30,
                ),
                hospital_id=tenant.id,
            )
            _, entry = await service.quick_opd(
                session,
                hospital_id=tenant.id,
                patient_id=record.id,
                doctor_id=doctor.id,
            )
            tokens.append(entry.token_number)
        await session.commit()

        assert tokens == [1, 2, 3]

    async def test_concurrent_check_ins_never_share_a_token(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        patients = []
        for index in range(5):
            record, _ = await patients_service.register_patient(
                session,
                PatientRegister(
                    full_name=f"Rush Patient {index}",
                    phone=f"91000000{index:02d}",
                    gender=Gender.FEMALE,
                    age_years=30,
                ),
                hospital_id=tenant.id,
            )
            patients.append(record.id)
        await session.commit()

        hospital_id, doctor_id = tenant.id, doctor.id

        async def arrive(patient_id: uuid.UUID) -> int:
            async with db.session_scope() as own:
                await db.set_tenant_context(own, hospital_id)
                _, entry = await service.quick_opd(
                    own,
                    hospital_id=hospital_id,
                    patient_id=patient_id,
                    doctor_id=doctor_id,
                )
                return entry.token_number

        tokens = await asyncio.gather(*(arrive(pid) for pid in patients))
        assert sorted(tokens) == [1, 2, 3, 4, 5]

    async def test_priority_patients_are_seen_first(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """Statutory in most Indian hospitals; also simply right."""
        first, _ = await patients_service.register_patient(
            session,
            PatientRegister(
                full_name="Ordinary Patient",
                phone="9200000001",
                gender=Gender.MALE,
                age_years=30,
            ),
            hospital_id=tenant.id,
        )
        elderly, _ = await patients_service.register_patient(
            session,
            PatientRegister(
                full_name="Senior Patient",
                phone="9200000002",
                gender=Gender.FEMALE,
                age_years=78,
            ),
            hospital_id=tenant.id,
        )
        await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=first.id, doctor_id=doctor.id
        )
        await service.quick_opd(
            session,
            hospital_id=tenant.id,
            patient_id=elderly.id,
            doctor_id=doctor.id,
            priority=QueuePriority.PRIORITY,
            priority_reason="Senior citizen",
        )
        await session.commit()

        queue = await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id)
        assert queue[0].patient_id == elderly.id

    async def test_priority_without_a_reason_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        """Jumping the queue is a decision someone must own by name."""
        appointment = await service.book_appointment(
            session,
            AppointmentBook(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_start=utc_now(),
                override_availability=True,
            ),
            hospital_id=tenant.id,
        )
        with pytest.raises(ValidationError) as excinfo:
            await service.check_in(session, appointment, priority=QueuePriority.EMERGENCY)
        assert excinfo.value.code == "priority_reason_required"

    async def test_patients_ahead_is_what_reception_tells_them(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        entries = []
        for index in range(3):
            record, _ = await patients_service.register_patient(
                session,
                PatientRegister(
                    full_name=f"Queued Patient {index}",
                    phone=f"93000000{index:02d}",
                    gender=Gender.MALE,
                    age_years=40,
                ),
                hospital_id=tenant.id,
            )
            _, entry = await service.quick_opd(
                session, hospital_id=tenant.id, patient_id=record.id, doctor_id=doctor.id
            )
            entries.append(entry)
        await session.commit()

        assert await service.count_patients_ahead(session, entries[0]) == 0
        assert await service.count_patients_ahead(session, entries[2]) == 2


class TestConsultationFlow:
    async def test_the_whole_opd_day(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        appointment, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()

        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None and called.id == entry.id
        assert called.status is QueueStatus.CALLED

        started = await service.start_consultation(session, called)
        assert started.status is QueueStatus.IN_CONSULTATION
        await session.refresh(appointment)
        assert appointment.status is AppointmentStatus.IN_CONSULTATION

        finished = await service.complete_consultation(session, started)
        await session.commit()
        assert finished.status is QueueStatus.COMPLETED
        await session.refresh(appointment)
        assert appointment.status is AppointmentStatus.COMPLETED

    async def test_calling_an_empty_queue_returns_nothing(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        assert (
            await service.call_next_patient(session, hospital_id=tenant.id, doctor_id=doctor.id)
            is None
        )

    async def test_a_skipped_patient_returns_to_the_queue(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None

        skipped = await service.skip_token(session, called, reason="Not in the corridor")
        await session.commit()
        assert skipped.status is QueueStatus.SKIPPED
        assert skipped.skip_count == 1

        # With nobody new waiting, the skipped patient is next.
        again = await service.call_next_patient(session, hospital_id=tenant.id, doctor_id=doctor.id)
        assert again is not None and again.id == entry.id

    async def test_completion_publishes_a_duration_for_reporting(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        seen: list[ConsultationCompleted] = []
        event_bus.subscribe(ConsultationCompleted, lambda event: seen.append(event))
        try:
            await service.quick_opd(
                session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
            )
            called = await service.call_next_patient(
                session, hospital_id=tenant.id, doctor_id=doctor.id
            )
            assert called is not None
            started = await service.start_consultation(session, called)
            await service.complete_consultation(session, started)
            await session.commit()
        finally:
            event_bus.clear()

        assert len(seen) == 1
        assert seen[0].duration_minutes >= 0

    async def test_cancelling_clears_a_live_token(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        """A cancelled appointment must not leave a ghost in the waiting room."""
        appointment, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()

        await service.cancel_appointment(
            session, appointment, reason="Patient left to go to another hospital"
        )
        await session.commit()

        await session.refresh(entry)
        assert entry.status is QueueStatus.LEFT_WITHOUT_BEING_SEEN
        queue = await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id)
        assert queue == []


class TestManualOrder:
    """Reception re-ranking a doctor's line by hand (CLAUDE.md §7b)."""

    async def _walk_in(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        *,
        phone: str,
        **kwargs: object,
    ) -> QueueEntry:
        record, _ = await patients_service.register_patient(
            session,
            PatientRegister(
                full_name=f"Patient {phone[-4:]}", phone=phone, gender=Gender.MALE, age_years=40
            ),
            hospital_id=tenant.id,
        )
        _, entry = await service.quick_opd(
            session,
            hospital_id=tenant.id,
            patient_id=record.id,
            doctor_id=doctor.id,
            **kwargs,  # type: ignore[arg-type]
        )
        return entry

    async def _order(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> list[uuid.UUID]:
        queue = await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id)
        return [row.id for row in queue]

    async def test_check_ins_are_numbered_in_arrival_order(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        first = await self._walk_in(session, tenant, doctor, phone="9000000101")
        second = await self._walk_in(session, tenant, doctor, phone="9000000102")
        third = await self._walk_in(session, tenant, doctor, phone="9000000103")
        await session.commit()

        assert (first.position, second.position, third.position) == (1, 2, 3)

    async def test_a_priority_check_in_goes_ahead_of_the_normals(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """The pre-existing rule, now stored: tier first, then arrival."""
        normal = await self._walk_in(session, tenant, doctor, phone="9000000111")
        emergency = await self._walk_in(
            session,
            tenant,
            doctor,
            phone="9000000112",
            priority=QueuePriority.EMERGENCY,
            priority_reason="Chest pain",
        )
        senior = await self._walk_in(
            session,
            tenant,
            doctor,
            phone="9000000113",
            priority=QueuePriority.PRIORITY,
            priority_reason="Senior citizen",
        )
        await session.commit()

        assert await self._order(session, tenant, doctor) == [emergency.id, senior.id, normal.id]
        assert await service.count_patients_ahead(session, normal) == 2

    async def test_moving_a_token_to_the_top(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        a = await self._walk_in(session, tenant, doctor, phone="9000000121")
        b = await self._walk_in(session, tenant, doctor, phone="9000000122")
        c = await self._walk_in(session, tenant, doctor, phone="9000000123")
        await session.commit()

        moved = await service.reorder_queue_entry(session, c, after_entry_id=None)
        await session.commit()

        assert moved.position == 1
        assert await self._order(session, tenant, doctor) == [c.id, a.id, b.id]
        # Renumbered contiguously — nothing is left at its old rank.
        for row in await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id):
            await session.refresh(row)
        assert [
            row.position
            for row in await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id)
        ] == [1, 2, 3]
        # And "how many ahead" agrees with the board.
        assert await service.count_patients_ahead(session, c) == 0
        assert await service.count_patients_ahead(session, b) == 2

    async def test_moving_a_token_after_another(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        a = await self._walk_in(session, tenant, doctor, phone="9000000131")
        b = await self._walk_in(session, tenant, doctor, phone="9000000132")
        c = await self._walk_in(session, tenant, doctor, phone="9000000133")
        await session.commit()

        await service.reorder_queue_entry(session, a, after_entry_id=b.id)
        await session.commit()
        assert await self._order(session, tenant, doctor) == [b.id, a.id, c.id]

        await service.reorder_queue_entry(session, a, after_entry_id=c.id)
        await session.commit()
        assert await self._order(session, tenant, doctor) == [b.id, c.id, a.id]

    async def test_a_hand_ranked_line_keeps_its_order_when_someone_new_arrives(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """A new walk-in must not undo the receptionist's work."""
        a = await self._walk_in(session, tenant, doctor, phone="9000000141")
        b = await self._walk_in(session, tenant, doctor, phone="9000000142")
        await service.reorder_queue_entry(session, b, after_entry_id=None)
        await session.commit()

        c = await self._walk_in(session, tenant, doctor, phone="9000000143")
        await session.commit()
        assert await self._order(session, tenant, doctor) == [b.id, a.id, c.id]

    async def test_the_doctor_calls_the_hand_ranked_order(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """The whole point: "call next" honours the drag, not the clock."""
        a = await self._walk_in(session, tenant, doctor, phone="9000000151")
        b = await self._walk_in(session, tenant, doctor, phone="9000000152")
        await service.reorder_queue_entry(session, b, after_entry_id=None)
        await session.commit()

        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None and called.id == b.id
        assert a.id != called.id

    async def test_dragging_does_not_change_the_priority_tier(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """Rank is where staff put them today; tier is a fact about them."""
        await self._walk_in(session, tenant, doctor, phone="9000000161")
        normal = await self._walk_in(session, tenant, doctor, phone="9000000162")
        moved = await service.reorder_queue_entry(session, normal, after_entry_id=None)
        assert moved.priority is QueuePriority.NORMAL
        assert moved.priority_reason is None

    async def test_a_patient_with_the_doctor_cannot_be_reordered(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        a = await self._walk_in(session, tenant, doctor, phone="9000000171")
        await self._walk_in(session, tenant, doctor, phone="9000000172")
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None and called.id == a.id
        started = await service.start_consultation(session, called)

        with pytest.raises(ConflictError) as excinfo:
            await service.reorder_queue_entry(session, started, after_entry_id=None)
        assert excinfo.value.code == "queue_entry_not_reorderable"

    async def test_the_anchor_must_be_in_the_same_line(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """Another doctor's token is not a place in this doctor's queue."""
        from tests.conftest import _make_user

        user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
        await db.set_tenant_context(session, tenant.id)
        other_doctor = await service.create_doctor(
            session,
            DoctorCreate(user_id=user.id),
            hospital_id=tenant.id,
            display_name="Dr Mehta",
        )
        mine = await self._walk_in(session, tenant, doctor, phone="9000000181")
        theirs = await self._walk_in(session, tenant, other_doctor, phone="9000000182")
        await session.commit()

        with pytest.raises(NotFoundError) as excinfo:
            await service.reorder_queue_entry(session, mine, after_entry_id=theirs.id)
        assert excinfo.value.code == "queue_anchor_not_found"

    async def test_placing_after_itself_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        a = await self._walk_in(session, tenant, doctor, phone="9000000191")
        with pytest.raises(ValidationError):
            await service.reorder_queue_entry(session, a, after_entry_id=a.id)

    async def test_a_skipped_patient_can_still_be_re_ranked(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """Skipped means "not in the corridor right now", not "gone" — they
        still hold a place, and reception may want to put them back on top
        when they reappear."""
        a = await self._walk_in(session, tenant, doctor, phone="9000000201")
        b = await self._walk_in(session, tenant, doctor, phone="9000000202")
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None and called.id == a.id
        skipped = await service.skip_token(session, called)
        await session.commit()

        moved = await service.reorder_queue_entry(session, skipped, after_entry_id=b.id)
        assert moved.position == 2
        assert await self._order(session, tenant, doctor) == [b.id, a.id]

    async def test_a_moved_patient_joins_the_new_line_by_the_check_in_rule(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """Reassignment across doctors: rank in the old line is forgotten."""
        from tests.conftest import _make_user

        user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
        await db.set_tenant_context(session, tenant.id)
        other_doctor = await service.create_doctor(
            session,
            DoctorCreate(user_id=user.id),
            hospital_id=tenant.id,
            display_name="Dr Mehta",
        )
        top = await self._walk_in(session, tenant, doctor, phone="9000000211")
        await self._walk_in(session, tenant, doctor, phone="9000000212")
        already = await self._walk_in(session, tenant, other_doctor, phone="9000000213")
        await session.commit()

        moved = await service.reassign_queue_entry(session, top, to_doctor_id=other_doctor.id)
        await session.commit()
        assert await self._order(session, tenant, other_doctor) == [already.id, moved.id]
        assert moved.position == 2


class TestMarkSeen:
    """Reception recording that the doctor has seen a patient."""

    async def _walk_in(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, *, phone: str
    ) -> tuple[Appointment, QueueEntry]:
        record, _ = await patients_service.register_patient(
            session,
            PatientRegister(
                full_name=f"Patient {phone[-4:]}", phone=phone, gender=Gender.FEMALE, age_years=30
            ),
            hospital_id=tenant.id,
        )
        return await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=record.id, doctor_id=doctor.id
        )

    async def test_a_waiting_patient_is_closed_as_seen(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        appointment, entry = await self._walk_in(session, tenant, doctor, phone="9000000301")
        await session.commit()

        seen = await service.mark_seen(session, entry)
        await session.commit()

        assert seen.status is QueueStatus.COMPLETED
        assert seen.completed_at is not None
        # No start time is invented: nobody recorded when they went in.
        assert seen.started_at is None

        await session.refresh(appointment)
        assert appointment.status is AppointmentStatus.COMPLETED
        assert appointment.completed_at is not None
        assert appointment.started_at is None

    async def test_seen_patients_leave_the_line(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """ "Call next" must not offer someone who has already been seen."""
        _, first = await self._walk_in(session, tenant, doctor, phone="9000000311")
        _, second = await self._walk_in(session, tenant, doctor, phone="9000000312")
        await service.mark_seen(session, first)
        await session.commit()

        queue = await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id)
        assert [row.id for row in queue] == [second.id]
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None and called.id == second.id

    async def test_a_called_patient_can_be_marked_seen(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        await self._walk_in(session, tenant, doctor, phone="9000000321")
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None
        assert (await service.mark_seen(session, called)).status is QueueStatus.COMPLETED

    async def test_a_skipped_patient_who_came_back_can_be_marked_seen(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        await self._walk_in(session, tenant, doctor, phone="9000000331")
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None
        skipped = await service.skip_token(session, called)
        assert (await service.mark_seen(session, skipped)).status is QueueStatus.COMPLETED

    async def test_a_patient_with_the_doctor_keeps_their_real_start_time(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        appointment, _ = await self._walk_in(session, tenant, doctor, phone="9000000341")
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None
        started = await service.start_consultation(session, called)
        began = started.started_at

        seen = await service.mark_seen(session, started)
        assert seen.status is QueueStatus.COMPLETED
        assert seen.started_at == began
        await session.refresh(appointment)
        assert appointment.status is AppointmentStatus.COMPLETED

    async def test_marking_seen_twice_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        _, entry = await self._walk_in(session, tenant, doctor, phone="9000000351")
        await service.mark_seen(session, entry)
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await service.mark_seen(session, entry)
        assert excinfo.value.code == "queue_entry_not_markable"

    async def test_a_patient_who_left_cannot_be_marked_seen(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        _, entry = await self._walk_in(session, tenant, doctor, phone="9000000361")
        left = await service.mark_left_without_being_seen(session, entry)
        with pytest.raises(ConflictError):
            await service.mark_seen(session, left)

    async def test_it_does_not_publish_a_consultation_duration(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor
    ) -> None:
        """Reception does not know how long the consultation took, and must
        not report zero minutes as if it had been measured."""
        _, entry = await self._walk_in(session, tenant, doctor, phone="9000000371")
        published: list[ConsultationCompleted] = []
        event_bus.subscribe(ConsultationCompleted, lambda event: published.append(event))
        try:
            await service.mark_seen(session, entry)
            await session.commit()
        finally:
            event_bus.clear()
        assert published == []


class TestReassignment:
    """Reception moving a patient between doctors' queues (CLAUDE.md §7b)."""

    @pytest.fixture
    async def second_doctor(self, session: AsyncSession, tenant: Hospital) -> Doctor:
        from tests.conftest import _make_user

        user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
        await db.set_tenant_context(session, tenant.id)
        record = await service.create_doctor(
            session,
            DoctorCreate(user_id=user.id, specialty="General Medicine"),
            hospital_id=tenant.id,
            display_name="Dr Mehta",
        )
        await session.commit()
        return record

    async def test_moving_a_patient_reissues_the_token_in_the_new_series(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        second_doctor: Doctor,
        patient: Patient,
    ) -> None:
        # Two ahead in Dr Rao's queue, so the moved patient holds token 3 there.
        for phone in ("9000000001", "9000000002"):
            other, _ = await patients_service.register_patient(
                session,
                PatientRegister(
                    full_name="Someone Else", phone=phone, gender=Gender.MALE, age_years=40
                ),
                hospital_id=tenant.id,
            )
            await service.quick_opd(
                session, hospital_id=tenant.id, patient_id=other.id, doctor_id=doctor.id
            )
        appointment, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()
        assert entry.token_number == 3
        arrived = entry.checked_in_at

        seen: list[QueueEntryReassigned] = []
        event_bus.subscribe(QueueEntryReassigned, lambda event: seen.append(event))
        try:
            moved = await service.reassign_queue_entry(
                session, entry, to_doctor_id=second_doctor.id, reason="Dr Rao running late"
            )
            await session.commit()
        finally:
            event_bus.clear()

        # Same row, new clinic, first token of Dr Mehta's day.
        assert moved.id == entry.id
        assert moved.doctor_id == second_doctor.id
        assert moved.token_number == 1
        assert moved.status is QueueStatus.WAITING
        # They keep the time they have already spent waiting.
        assert moved.checked_in_at == arrived

        await session.refresh(appointment)
        assert appointment.doctor_id == second_doctor.id

        old_queue = await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id)
        assert entry.id not in {e.id for e in old_queue}
        new_queue = await service.get_queue(
            session, hospital_id=tenant.id, doctor_id=second_doctor.id
        )
        assert [e.id for e in new_queue] == [entry.id]

        [event] = seen
        assert (event.from_token_number, event.to_token_number) == (3, 1)
        assert event.from_doctor_id == doctor.id
        assert event.reason == "Dr Rao running late"

    async def test_moving_into_a_queue_that_already_has_tokens(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        second_doctor: Doctor,
        patient: Patient,
    ) -> None:
        """Regression: the row briefly held the new doctor with the *old* token
        during allocation, and collided with whoever already had that number
        in the destination clinic. Token 1 to a queue whose token 1 is taken."""
        other, _ = await patients_service.register_patient(
            session,
            PatientRegister(
                full_name="Already Here", phone="9000000005", gender=Gender.MALE, age_years=45
            ),
            hospital_id=tenant.id,
        )
        await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=other.id, doctor_id=second_doctor.id
        )
        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()
        assert entry.token_number == 1

        moved = await service.reassign_queue_entry(session, entry, to_doctor_id=second_doctor.id)
        await session.commit()
        assert moved.token_number == 2

    async def test_the_old_series_is_not_reused(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        second_doctor: Doctor,
        patient: Patient,
    ) -> None:
        """Token 1 was read out for Dr Rao once already; the next patient gets 2."""
        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await service.reassign_queue_entry(session, entry, to_doctor_id=second_doctor.id)
        await session.commit()

        other, _ = await patients_service.register_patient(
            session,
            PatientRegister(
                full_name="Next Person", phone="9000000009", gender=Gender.MALE, age_years=50
            ),
            hospital_id=tenant.id,
        )
        _, next_entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=other.id, doctor_id=doctor.id
        )
        assert next_entry.token_number == 2

    async def test_a_skipped_patient_moves_as_plain_waiting(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        second_doctor: Doctor,
        patient: Patient,
    ) -> None:
        await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None
        skipped = await service.skip_token(session, called)

        moved = await service.reassign_queue_entry(session, skipped, to_doctor_id=second_doctor.id)
        assert moved.status is QueueStatus.WAITING
        assert moved.called_at is None

    async def test_a_called_patient_cannot_be_moved(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        second_doctor: Doctor,
        patient: Patient,
    ) -> None:
        """The doctor is expecting them to walk in. Skip first, then move."""
        await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None

        with pytest.raises(ConflictError) as excinfo:
            await service.reassign_queue_entry(session, called, to_doctor_id=second_doctor.id)
        assert excinfo.value.code == "queue_entry_not_movable"

    async def test_a_patient_with_the_doctor_cannot_be_moved(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        second_doctor: Doctor,
        patient: Patient,
    ) -> None:
        await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        called = await service.call_next_patient(
            session, hospital_id=tenant.id, doctor_id=doctor.id
        )
        assert called is not None
        started = await service.start_consultation(session, called)

        with pytest.raises(ConflictError):
            await service.reassign_queue_entry(session, started, to_doctor_id=second_doctor.id)

    async def test_moving_to_the_same_doctor_is_refused(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        with pytest.raises(ValidationError) as excinfo:
            await service.reassign_queue_entry(session, entry, to_doctor_id=doctor.id)
        assert excinfo.value.code == "same_doctor"
        # And nothing was burned: the token is unchanged.
        assert entry.token_number == 1

    async def test_a_doctor_not_accepting_patients_cannot_receive_one(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        second_doctor: Doctor,
        patient: Patient,
    ) -> None:
        await service.update_doctor(
            session, second_doctor, DoctorUpdate(is_accepting_appointments=False)
        )
        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        with pytest.raises(ConflictError) as excinfo:
            await service.reassign_queue_entry(session, entry, to_doctor_id=second_doctor.id)
        assert excinfo.value.code == "doctor_not_accepting"


class TestTenantIsolation:
    async def test_another_hospitals_queue_is_invisible(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()

        async with db.system_context(session):
            other = Hospital(code=f"S{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        await db.set_tenant_context(session, other_id)
        assert await service.get_queue(session, hospital_id=other_id) == []

    async def test_appointments_are_listed_per_tenant(
        self, session: AsyncSession, tenant: Hospital, doctor: Doctor, patient: Patient
    ) -> None:
        await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()

        _, total = await service.list_appointments(session, PageParams(), hospital_id=tenant.id)
        assert total == 1


class TestTheQueueEmpties:
    """The bug: it did not.

    Queue entries were closed only by cancelling or no-showing the appointment,
    so every visit that actually *finished* left its token on the live board as
    `WAITING`, forever. Completed, admitted, deceased, referred, absconded — all
    of them. The OPD queue accumulated the entire day and never drained.

    It hid for a long time because every screen that reads the queue looks for a
    named patient rather than at the whole list. It surfaced the day the
    terminal-outcome screens landed and a patient recorded as deceased was still
    showing first in line for a doctor.

    `scheduling` now subscribes to `clinical`'s closure events, which is the
    §2-shaped way to do it: `clinical` still knows nothing about a queue.
    """

    async def test_a_completed_visit_takes_its_token_off_the_board(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        doctor_user: User,
        patient: Patient,
    ) -> None:
        from app.modules.clinical import service as clinical_service

        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()

        encounter = await clinical_service.open_encounter(
            session,
            hospital_id=tenant.id,
            patient_id=patient.id,
            actor=doctor_user,
            appointment_id=entry.appointment_id,
        )
        await clinical_service.start_consultation(session, encounter, actor=doctor_user)
        await clinical_service.complete_consultation(session, encounter, actor=doctor_user)
        await session.commit()

        await session.refresh(entry)
        assert entry.status is QueueStatus.COMPLETED
        assert await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id) == []

    async def test_a_patient_who_dies_before_being_seen_left_without_being_seen(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        doctor_user: User,
        patient: Patient,
    ) -> None:
        """The closing status comes from whether a doctor started the consultation.

        Not from the token's own status, which was my first attempt and was
        wrong: in the real flow the doctor starts the consultation on the chart
        rather than on the queue board, so a token frequently still reads
        `WAITING` when the visit closes. Deriving from that would have filed
        every patient the doctor actually saw as "left without being seen" and
        corrupted the one dashboard figure that means somebody gave up and went
        home. `was_seen` uses the same definition `reporting` does.
        """
        from app.modules.clinical import service as clinical_service
        from app.modules.clinical.schemas import DeathRecord

        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()

        encounter = await clinical_service.open_encounter(
            session,
            hospital_id=tenant.id,
            patient_id=patient.id,
            actor=doctor_user,
            appointment_id=entry.appointment_id,
        )
        await clinical_service.record_death(
            session,
            encounter,
            DeathRecord(
                deceased_at=utc_now(),
                death_certified_by_id=doctor_user.id,
                cause_of_death="Cardiac arrest in the waiting room.",
            ),
            actor=doctor_user,
        )
        await session.commit()

        await session.refresh(entry)
        assert entry.status is QueueStatus.LEFT_WITHOUT_BEING_SEEN
        # The one that made this visible: a deceased patient first in line.
        assert await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id) == []

    async def test_an_admitted_patient_is_not_still_in_the_corridor(
        self,
        session: AsyncSession,
        tenant: Hospital,
        doctor: Doctor,
        doctor_user: User,
        patient: Patient,
    ) -> None:
        """`ADMITTED` is not terminal, so `EncounterClosed` never fires for it.

        Which is exactly how admitted patients became the largest group stuck on
        the board — they are the one ending that is not an ending.
        """
        from app.modules.clinical import service as clinical_service

        _, entry = await service.quick_opd(
            session, hospital_id=tenant.id, patient_id=patient.id, doctor_id=doctor.id
        )
        await session.commit()

        encounter = await clinical_service.open_encounter(
            session,
            hospital_id=tenant.id,
            patient_id=patient.id,
            actor=doctor_user,
            appointment_id=entry.appointment_id,
        )
        await clinical_service.start_consultation(session, encounter, actor=doctor_user)
        await clinical_service.admit(session, encounter, actor=doctor_user)
        await session.commit()

        await session.refresh(entry)
        assert entry.status is QueueStatus.COMPLETED
        assert await service.get_queue(session, hospital_id=tenant.id, doctor_id=doctor.id) == []
