"""Scheduling HTTP routes. Thin — logic lives in `service.py`."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.exceptions import NotFoundError, ValidationError
from app.core.models import utc_now
from app.core.pagination import Page, PageParams, page_params
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter
from app.modules.clinical.rbac import ClinicalPermissions
from app.modules.identity import service as identity_service
from app.modules.identity.service import AuthContext
from app.modules.patients import service as patients_service
from app.modules.patients.rbac import PatientPermissions
from app.modules.patients.schemas import PatientRead
from app.modules.scheduling import service
from app.modules.scheduling.models import (
    Appointment,
    AppointmentStatus,
    QueueEntry,
    QueueStatus,
)
from app.modules.scheduling.rbac import SchedulingPermissions
from app.modules.scheduling.schemas import (
    AppointmentBook,
    AppointmentCancelRequest,
    AppointmentRead,
    AppointmentReschedule,
    AvailabilityExceptionCreate,
    AvailabilityExceptionRead,
    CheckInRequest,
    DoctorAvailabilityCreate,
    DoctorAvailabilityRead,
    DoctorCreate,
    DoctorQueue,
    DoctorRead,
    DoctorUpdate,
    QueueBoardEntry,
    QueueEntryRead,
    QueueReassignRequest,
    QueueReorderRequest,
    QueueSummary,
    QuickOpdRequest,
    QuickOpdResponse,
    SearchResults,
    SkipRequest,
    SlotRead,
)
from app.modules.scheduling.transitions import TERMINAL_QUEUE_STATUSES
from app.modules.tenancy import service as tenancy_service

doctors_router = APIRouter(prefix="/doctors", tags=["scheduling"])
appointments_router = APIRouter(prefix="/appointments", tags=["scheduling"])
queue_router = APIRouter(prefix="/queue", tags=["queue"])
search_router = APIRouter(prefix="/search", tags=["search"])

CanReadDoctor = Annotated[AuthContext, Depends(require(SchedulingPermissions.DOCTOR_READ))]
CanManageDoctor = Annotated[AuthContext, Depends(require(SchedulingPermissions.DOCTOR_MANAGE))]
CanManageAvailability = Annotated[
    AuthContext, Depends(require(SchedulingPermissions.AVAILABILITY_MANAGE))
]
CanBook = Annotated[AuthContext, Depends(require(SchedulingPermissions.APPOINTMENT_CREATE))]
CanReadAppointment = Annotated[
    AuthContext, Depends(require(SchedulingPermissions.APPOINTMENT_READ))
]
CanUpdateAppointment = Annotated[
    AuthContext, Depends(require(SchedulingPermissions.APPOINTMENT_UPDATE))
]
CanCancel = Annotated[AuthContext, Depends(require(SchedulingPermissions.APPOINTMENT_CANCEL))]
CanReadQueue = Annotated[AuthContext, Depends(require(SchedulingPermissions.QUEUE_READ))]
CanManageQueue = Annotated[AuthContext, Depends(require(SchedulingPermissions.QUEUE_MANAGE))]
# Everything the universal search returns is patient identity, whichever module
# the match came from — so it is gated on `patient:read`, not on a permission
# from this module. A lab technician who may read a patient may find one.
CanSearch = Annotated[AuthContext, Depends(require(PatientPermissions.READ))]


# ---------------------------------------------------------------------------
# Universal search (CLAUDE.md §7b)
# ---------------------------------------------------------------------------
@search_router.get("", response_model=SearchResults, summary="One box for any identifier")
async def universal_search(
    session: SessionDep,
    request: Request,
    context: CanSearch,
    tenant_id: TenantId,
    q: Annotated[
        str,
        Query(
            # One character, not two. A single letter is too little to search
            # on and the service returns nothing for it — but a single *digit*
            # is token 4, and the first nine patients of every clinic have one.
            # The rule is about the shape of the term, so the service owns it.
            min_length=1,
            max_length=100,
            description="UHID, name, mobile, token, visit, booking or doctor",
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=service.MAX_SEARCH_HITS)] = service.MAX_SEARCH_HITS,
) -> SearchResults:
    """Resolve whatever a member of staff is holding, without asking them what
    kind of thing it is.

    Audited like `/patients/search` and for the same reason: under the DPDP Act
    a lookup of personal data is an access, whether or not the person opens the
    record afterwards. The query is recorded, the results are not.
    """
    hits = await service.search(
        session,
        q,
        hospital_id=tenant_id,
        limit=limit,
        # Whether an open visit is offered as a destination. Reception holds
        # `patient:read` and not `note:read`, and a hit that leads to a 403 is
        # worse than a missing one.
        can_open_charts=ClinicalPermissions.NOTE_READ in context.permissions,
    )
    await identity_service.record_audit(
        session,
        action="search.universal",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="search",
        changes={"query": q, "results": len(hits)},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return SearchResults(query=q, hits=hits)


# ---------------------------------------------------------------------------
# Doctors & availability
# ---------------------------------------------------------------------------
@doctors_router.post(
    "",
    response_model=DoctorRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a doctor profile",
)
async def create_doctor(
    payload: DoctorCreate,
    session: SessionDep,
    request: Request,
    context: CanManageDoctor,
    tenant_id: TenantId,
) -> DoctorRead:
    # The user is loaded through identity's service, never by joining its table.
    user = await identity_service.get_user(session, payload.user_id)
    if user.hospital_id != tenant_id:
        raise NotFoundError("User not found.", code="user_not_found")

    doctor = await service.create_doctor(
        session, payload, hospital_id=tenant_id, display_name=user.full_name
    )
    await identity_service.record_audit(
        session,
        action="doctor.create",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="doctor",
        resource_id=doctor.id,
        changes={"user_id": str(payload.user_id)},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return DoctorRead.model_validate(doctor)


@doctors_router.get("", response_model=Page[DoctorRead], summary="List doctors")
async def list_doctors(
    session: SessionDep,
    context: CanReadDoctor,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    department_id: uuid.UUID | None = None,
    accepting_only: bool = False,
) -> Page[DoctorRead]:
    doctors, total = await service.list_doctors(
        session,
        params,
        hospital_id=tenant_id,
        department_id=department_id,
        accepting_only=accepting_only,
    )
    return Page.build(
        [DoctorRead.model_validate(doctor) for doctor in doctors], total=total, params=params
    )


@doctors_router.get("/{doctor_id}", response_model=DoctorRead, summary="Get a doctor")
async def get_doctor(
    doctor_id: uuid.UUID, session: SessionDep, context: CanReadDoctor, tenant_id: TenantId
) -> DoctorRead:
    return DoctorRead.model_validate(
        await service.get_doctor(session, doctor_id, hospital_id=tenant_id)
    )


@doctors_router.patch("/{doctor_id}", response_model=DoctorRead, summary="Update a doctor")
async def update_doctor(
    doctor_id: uuid.UUID,
    payload: DoctorUpdate,
    session: SessionDep,
    context: CanManageDoctor,
    tenant_id: TenantId,
) -> DoctorRead:
    doctor = await service.get_doctor(session, doctor_id, hospital_id=tenant_id)
    updated = await service.update_doctor(session, doctor, payload)
    await session.commit()
    return DoctorRead.model_validate(updated)


async def _open_encounter_for(
    session: SessionDep,
    appointment: Appointment,
    *,
    context: AuthContext,
    tenant_id: uuid.UUID,
) -> Encounter:
    """Open the clinical Encounter that belongs to this check-in.

    Trade-off, stated because it is not the obvious choice: `scheduling` already
    publishes `PatientCheckedIn`, and `clinical` could have subscribed to it.
    We call the service directly instead. The event bus is deliberately
    fire-and-forget — a failing handler is logged and swallowed so one bad
    subscriber cannot undo a committed check-in — and that is exactly wrong for
    the chart itself. A patient holding a token with no encounter behind it has
    nowhere to record vitals, no orders and no bill, and nothing would have told
    anyone. So the spine of the system is created by an explicit call inside the
    same transaction, and the event stays published for subscribers whose
    failure is survivable.
    """
    encounter = await clinical_service.open_encounter(
        session,
        hospital_id=tenant_id,
        patient_id=appointment.patient_id,
        actor=context.user,
        appointment_id=appointment.id,
        doctor_id=appointment.doctor_id,
        department_id=appointment.department_id,
        chief_complaint=appointment.reason,
    )
    await service.link_encounter(session, appointment, encounter.id)
    return encounter


def _may_manage_doctor(context: AuthContext, doctor_user_id: uuid.UUID) -> bool:
    """A doctor may set their own hours; managing others needs doctor:manage."""
    return context.user.id == doctor_user_id or context.has_permission(
        SchedulingPermissions.DOCTOR_MANAGE
    )


@doctors_router.post(
    "/{doctor_id}/availability",
    response_model=DoctorAvailabilityRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a weekly clinic session",
)
async def add_availability(
    doctor_id: uuid.UUID,
    payload: DoctorAvailabilityCreate,
    session: SessionDep,
    context: CanManageAvailability,
    tenant_id: TenantId,
) -> DoctorAvailabilityRead:
    doctor = await service.get_doctor(session, doctor_id, hospital_id=tenant_id)
    if not _may_manage_doctor(context, doctor.user_id):
        raise NotFoundError("Doctor not found.", code="doctor_not_found")

    availability = await service.set_availability(session, doctor, payload)
    await session.commit()
    return DoctorAvailabilityRead.model_validate(availability)


@doctors_router.get(
    "/{doctor_id}/availability",
    response_model=list[DoctorAvailabilityRead],
    summary="Weekly clinic sessions",
)
async def list_availability(
    doctor_id: uuid.UUID, session: SessionDep, context: CanReadDoctor, tenant_id: TenantId
) -> list[DoctorAvailabilityRead]:
    await service.get_doctor(session, doctor_id, hospital_id=tenant_id)
    rows = await service.list_availability(session, doctor_id)
    return [DoctorAvailabilityRead.model_validate(row) for row in rows]


@doctors_router.post(
    "/{doctor_id}/exceptions",
    response_model=AvailabilityExceptionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record leave or an extra clinic",
)
async def add_exception(
    doctor_id: uuid.UUID,
    payload: AvailabilityExceptionCreate,
    session: SessionDep,
    context: CanManageAvailability,
    tenant_id: TenantId,
) -> AvailabilityExceptionRead:
    doctor = await service.get_doctor(session, doctor_id, hospital_id=tenant_id)
    if not _may_manage_doctor(context, doctor.user_id):
        raise NotFoundError("Doctor not found.", code="doctor_not_found")

    exception = await service.add_availability_exception(
        session, doctor, payload, recorded_by_id=context.user.id
    )
    await session.commit()
    return AvailabilityExceptionRead.model_validate(exception)


@doctors_router.get(
    "/{doctor_id}/slots",
    response_model=list[SlotRead],
    summary="Bookable slots on a date",
)
async def get_slots(
    doctor_id: uuid.UUID,
    session: SessionDep,
    context: CanReadDoctor,
    tenant_id: TenantId,
    on_date: Annotated[date, Query(alias="date")],
) -> list[SlotRead]:
    doctor = await service.get_doctor(session, doctor_id, hospital_id=tenant_id)
    return await service.get_available_slots(session, doctor, on_date)


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
@appointments_router.post(
    "",
    response_model=AppointmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Book an appointment",
)
async def book_appointment(
    payload: AppointmentBook,
    session: SessionDep,
    request: Request,
    context: CanBook,
    tenant_id: TenantId,
) -> AppointmentRead:
    appointment = await service.book_appointment(
        session, payload, hospital_id=tenant_id, booked_by_id=context.user.id
    )
    await identity_service.record_audit(
        session,
        action="appointment.book",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="appointment",
        resource_id=appointment.id,
        changes={
            "appointment_number": appointment.appointment_number,
            "patient_id": str(appointment.patient_id),
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return AppointmentRead.model_validate(appointment)


@appointments_router.get("", response_model=Page[AppointmentRead], summary="List appointments")
async def list_appointments(
    session: SessionDep,
    context: CanReadAppointment,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    doctor_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    on_date: Annotated[date | None, Query(alias="date")] = None,
    appointment_status: AppointmentStatus | None = None,
) -> Page[AppointmentRead]:
    appointments, total = await service.list_appointments(
        session,
        params,
        hospital_id=tenant_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        on_date=on_date,
        status=appointment_status,
    )
    return Page.build(
        [AppointmentRead.model_validate(item) for item in appointments],
        total=total,
        params=params,
    )


@appointments_router.get(
    "/{appointment_id}", response_model=AppointmentRead, summary="Get an appointment"
)
async def get_appointment(
    appointment_id: uuid.UUID,
    session: SessionDep,
    context: CanReadAppointment,
    tenant_id: TenantId,
) -> AppointmentRead:
    return AppointmentRead.model_validate(
        await service.get_appointment(session, appointment_id, hospital_id=tenant_id)
    )


@appointments_router.post(
    "/{appointment_id}/check-in",
    response_model=QueueEntryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Check a patient in and issue a token",
)
async def check_in(
    appointment_id: uuid.UUID,
    payload: CheckInRequest,
    session: SessionDep,
    request: Request,
    context: CanManageQueue,
    tenant_id: TenantId,
) -> QueueEntryRead:
    appointment = await service.get_appointment(session, appointment_id, hospital_id=tenant_id)
    entry = await service.check_in(
        session,
        appointment,
        priority=payload.priority,
        priority_reason=payload.priority_reason,
        actor_id=context.user.id,
    )
    # The chart opens with the token, not as a second step someone must remember.
    await _open_encounter_for(session, appointment, context=context, tenant_id=tenant_id)
    await identity_service.record_audit(
        session,
        action="appointment.check_in",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="appointment",
        resource_id=appointment.id,
        changes={"token": entry.token_number, "priority": entry.priority.name},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return QueueEntryRead.model_validate(entry)


@appointments_router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentRead,
    summary="Cancel an appointment",
)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentCancelRequest,
    session: SessionDep,
    request: Request,
    context: CanCancel,
    tenant_id: TenantId,
) -> AppointmentRead:
    appointment = await service.get_appointment(session, appointment_id, hospital_id=tenant_id)
    cancelled = await service.cancel_appointment(
        session,
        appointment,
        reason=payload.reason,
        cancelled_by_patient=payload.cancelled_by_patient,
        actor_id=context.user.id,
    )
    await identity_service.record_audit(
        session,
        action="appointment.cancel",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="appointment",
        resource_id=appointment.id,
        changes={"reason": payload.reason, "by_patient": payload.cancelled_by_patient},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return AppointmentRead.model_validate(cancelled)


@appointments_router.post(
    "/{appointment_id}/no-show",
    response_model=AppointmentRead,
    summary="Mark an appointment as a no-show",
)
async def mark_no_show(
    appointment_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanCancel,
    tenant_id: TenantId,
) -> AppointmentRead:
    appointment = await service.get_appointment(session, appointment_id, hospital_id=tenant_id)
    updated = await service.mark_no_show(session, appointment, actor_id=context.user.id)
    await identity_service.record_audit(
        session,
        action="appointment.no_show",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="appointment",
        resource_id=appointment.id,
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return AppointmentRead.model_validate(updated)


@appointments_router.post(
    "/{appointment_id}/reschedule",
    response_model=AppointmentRead,
    summary="Move an appointment to a new slot",
)
async def reschedule(
    appointment_id: uuid.UUID,
    payload: AppointmentReschedule,
    session: SessionDep,
    request: Request,
    context: CanUpdateAppointment,
    tenant_id: TenantId,
) -> AppointmentRead:
    appointment = await service.get_appointment(session, appointment_id, hospital_id=tenant_id)
    replacement = await service.reschedule_appointment(
        session,
        appointment,
        new_start=payload.scheduled_start,
        reason=payload.reason,
        override_availability=payload.override_availability,
        actor_id=context.user.id,
    )
    await identity_service.record_audit(
        session,
        action="appointment.reschedule",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="appointment",
        resource_id=appointment.id,
        changes={"to": replacement.appointment_number},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return AppointmentRead.model_validate(replacement)


# ---------------------------------------------------------------------------
# Queue — the OPD floor
# ---------------------------------------------------------------------------
@queue_router.post(
    "/quick-opd",
    response_model=QuickOpdResponse,
    status_code=status.HTTP_201_CREATED,
    summary="One-click OPD: book, check in and issue a token",
)
async def quick_opd(
    payload: QuickOpdRequest,
    session: SessionDep,
    request: Request,
    context: CanBook,
    tenant_id: TenantId,
) -> QuickOpdResponse:
    """CLAUDE.md §7b — the single most repeated action of the day, in one call."""
    appointment, entry = await service.quick_opd(
        session,
        hospital_id=tenant_id,
        patient_id=payload.patient_id,
        doctor_id=payload.doctor_id,
        reason=payload.reason,
        priority=payload.priority,
        priority_reason=payload.priority_reason,
        source=payload.source,
        actor_id=context.user.id,
    )
    encounter = await _open_encounter_for(
        session, appointment, context=context, tenant_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="queue.quick_opd",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="appointment",
        resource_id=appointment.id,
        changes={"token": entry.token_number, "patient_id": str(payload.patient_id)},
        ip_address=await get_client_ip(request),
    )
    await session.commit()

    doctor = await service.get_doctor(session, entry.doctor_id, hospital_id=tenant_id)
    department = None
    if doctor.department_id is not None:
        department = await tenancy_service.get_department(
            session, doctor.department_id, hospital_id=tenant_id
        )

    # Through `patients.service`, never a join across the boundary (§2). One
    # lookup, on the response to an action reception takes a hundred times a
    # day — but the slip is worthless without it, and it is the field the QR
    # is generated from.
    patient = await patients_service.get_patient(session, entry.patient_id)
    patient_view = PatientRead.model_validate(patient)

    return QuickOpdResponse(
        appointment=AppointmentRead.model_validate(appointment),
        queue_entry=QueueEntryRead.model_validate(entry),
        patient_name=patient_view.full_name,
        uhid=patient_view.uhid,
        token_number=entry.token_number,
        doctor_name=doctor.display_name,
        department_name=department.name if department else None,
        location=department.location if department else None,
        patients_ahead=await service.count_patients_ahead(session, entry),
        encounter_id=encounter.id,
        encounter_number=encounter.encounter_number,
    )


async def _to_board(
    session: SessionDep, entries: list[QueueEntry], *, hospital_id: uuid.UUID
) -> list[QueueBoardEntry]:
    """Attach patient identity and the open chart to queue rows.

    Two bulk lookups for the whole board, both through the owning module's
    service rather than a join across the boundary (CLAUDE.md §2). The cost is
    constant in the length of the queue, which is what keeps the screen staff
    refresh most often from being the slowest one in the system.
    """
    patients = await patients_service.get_patients_by_ids(
        session, [entry.patient_id for entry in entries], hospital_id=hospital_id
    )
    encounters = await clinical_service.get_encounters_by_appointments(
        session,
        [entry.appointment_id for entry in entries if entry.appointment_id is not None],
        hospital_id=hospital_id,
    )
    # Seen patients whose visit the doctor has since closed still need to name
    # it: reception sends them for admission from that row, and the admission
    # desk opens a new IPD visit off the closed one. Only for seen rows — a
    # live row's link is "the chart to open", and a closed chart is not one.
    closed_visits = await service.get_visit_links(
        session,
        [
            entry.appointment_id
            for entry in entries
            if entry.status is QueueStatus.COMPLETED
            and entry.appointment_id is not None
            and entry.appointment_id not in encounters
        ],
        hospital_id=hospital_id,
    )

    board: list[QueueBoardEntry] = []
    for entry in entries:
        row = QueueBoardEntry.model_validate(entry)

        encounter = (
            encounters.get(entry.appointment_id) if entry.appointment_id is not None else None
        )
        if encounter is not None:
            row.encounter_id = encounter.id
            row.encounter_number = encounter.encounter_number
        elif entry.appointment_id is not None and entry.appointment_id in closed_visits:
            row.encounter_id = closed_visits[entry.appointment_id]

        patient = patients.get(entry.patient_id)
        if patient is not None:
            # Projected through `PatientRead` rather than read off the ORM
            # object, so age is derived by the one implementation that owns
            # that rule instead of a second copy of the birthday arithmetic.
            view = PatientRead.model_validate(patient)
            row.patient_name = view.full_name
            row.patient_uhid = view.uhid
            row.patient_age_years = view.age_years
            row.patient_gender = str(view.gender)
            row.patient_is_deceased = view.is_deceased
        board.append(row)
    return board


@queue_router.get("", response_model=list[QueueBoardEntry], summary="The live queue")
async def get_queue(
    session: SessionDep,
    context: CanReadQueue,
    tenant_id: TenantId,
    doctor_id: uuid.UUID | None = None,
    queue_date: Annotated[date | None, Query(alias="date")] = None,
    active_only: bool = True,
) -> list[QueueBoardEntry]:
    entries = await service.get_queue(
        session,
        hospital_id=tenant_id,
        doctor_id=doctor_id,
        queue_date=queue_date,
        active_only=active_only,
    )
    return await _to_board(session, entries, hospital_id=tenant_id)


@queue_router.get("/mine", response_model=list[QueueBoardEntry], summary="A doctor's own queue")
async def my_queue(
    session: SessionDep, context: CanReadQueue, tenant_id: TenantId
) -> list[QueueBoardEntry]:
    """The doctor's screen. They should never have to know their own record id."""
    doctor = await service.get_doctor_by_user(session, context.user.id, hospital_id=tenant_id)
    if doctor is None:
        raise NotFoundError("You do not have a doctor profile.", code="not_a_doctor")

    entries = await service.get_queue(session, hospital_id=tenant_id, doctor_id=doctor.id)
    return await _to_board(session, entries, hospital_id=tenant_id)


@queue_router.get(
    "/board", response_model=list[DoctorQueue], summary="Today's queue, one column per doctor"
)
async def queue_board(
    session: SessionDep,
    context: CanReadQueue,
    tenant_id: TenantId,
    queue_date: Annotated[date | None, Query(alias="date")] = None,
) -> list[DoctorQueue]:
    """Reception's view (CLAUDE.md §7b, role-based dashboards).

    Every active doctor appears, with or without patients — the empty column
    is the one reception is looking for when a clinic is running behind and
    they need somewhere to send the next walk-in. Ordered busiest first, so
    the problem is at the top and the relief is at the bottom.

    Built from two bounded reads (all doctors, all of today's tokens) and one
    identity join, so it costs the same whether ten people are waiting or two
    hundred.
    """
    doctors = await service.list_all_active_doctors(session, hospital_id=tenant_id)
    entries = await service.get_queue(
        session, hospital_id=tenant_id, queue_date=queue_date, active_only=False
    )
    shown = [
        entry
        for entry in entries
        if entry.status not in TERMINAL_QUEUE_STATUSES or entry.status is QueueStatus.COMPLETED
    ]
    # One identity lookup for the line and the seen list together.
    rows = {row.id: row for row in await _to_board(session, shown, hospital_id=tenant_id)}

    department_ids = {doctor.department_id for doctor in doctors if doctor.department_id}
    departments = {
        department.id: department
        for department in await tenancy_service.get_departments_by_ids(
            session, department_ids, hospital_id=tenant_id
        )
    }

    now = utc_now()
    columns: dict[uuid.UUID, DoctorQueue] = {}
    for doctor in doctors:
        department = departments.get(doctor.department_id) if doctor.department_id else None
        columns[doctor.id] = DoctorQueue(
            doctor_id=doctor.id,
            doctor_name=doctor.display_name,
            specialty=doctor.specialty,
            department_id=doctor.department_id,
            department_name=department.name if department else None,
            is_accepting_appointments=doctor.is_accepting_appointments,
        )

    for entry in entries:
        column = columns.get(entry.doctor_id)
        if column is None:
            # A doctor retired since the token was issued. The token is still
            # real; the column is not. Skip rather than invent a doctor.
            continue
        if entry.status is QueueStatus.COMPLETED:
            column.completed += 1
        elif entry.status is QueueStatus.IN_CONSULTATION:
            column.in_consultation += 1
        elif entry.status in (QueueStatus.WAITING, QueueStatus.CALLED, QueueStatus.SKIPPED):
            column.waiting += 1
            waited = int((now - entry.checked_in_at).total_seconds() // 60)
            if column.longest_wait_minutes is None or waited > column.longest_wait_minutes:
                column.longest_wait_minutes = waited
        row = rows.get(entry.id)
        if row is None:
            continue
        if entry.status is QueueStatus.COMPLETED:
            column.seen.append(row)
        else:
            column.entries.append(row)

    for column in columns.values():
        # Most recent first: the one reception just marked is at the top.
        column.seen.sort(key=lambda row: row.completed_at or row.checked_in_at, reverse=True)

    return sorted(columns.values(), key=lambda column: (-column.waiting, column.doctor_name))


@queue_router.get("/summary", response_model=QueueSummary, summary="Queue counts for one doctor")
async def queue_summary(
    session: SessionDep,
    context: CanReadQueue,
    tenant_id: TenantId,
    doctor_id: uuid.UUID,
    queue_date: Annotated[date | None, Query(alias="date")] = None,
) -> QueueSummary:
    from app.modules.scheduling.models import QueueStatus

    doctor = await service.get_doctor(session, doctor_id, hospital_id=tenant_id)
    entries = await service.get_queue(
        session,
        hospital_id=tenant_id,
        doctor_id=doctor_id,
        queue_date=queue_date,
        active_only=False,
    )
    waiting = [e for e in entries if e.status is QueueStatus.WAITING]
    in_consult = [e for e in entries if e.status is QueueStatus.IN_CONSULTATION]
    completed = [e for e in entries if e.status is QueueStatus.COMPLETED]

    return QueueSummary(
        doctor_id=doctor.id,
        doctor_name=doctor.display_name,
        queue_date=queue_date or entries[0].queue_date if entries else (queue_date or date.today()),
        waiting=len(waiting),
        in_consultation=len(in_consult),
        completed=len(completed),
        current_token=in_consult[0].token_number if in_consult else None,
        next_token=waiting[0].token_number if waiting else None,
    )


@queue_router.post(
    "/call-next", response_model=QueueEntryRead | None, summary="Call the next patient"
)
async def call_next(
    session: SessionDep,
    context: CanManageQueue,
    tenant_id: TenantId,
    doctor_id: uuid.UUID | None = None,
) -> QueueEntryRead | None:
    target = doctor_id
    if target is None:
        doctor = await service.get_doctor_by_user(session, context.user.id, hospital_id=tenant_id)
        if doctor is None:
            raise ValidationError(
                "Specify a doctor, or call from a doctor's own account.",
                code="doctor_required",
            )
        target = doctor.id

    entry = await service.call_next_patient(session, hospital_id=tenant_id, doctor_id=target)
    await session.commit()
    return QueueEntryRead.model_validate(entry) if entry else None


@queue_router.post(
    "/{entry_id}/start", response_model=QueueEntryRead, summary="Start the consultation"
)
async def start_consultation(
    entry_id: uuid.UUID, session: SessionDep, context: CanManageQueue, tenant_id: TenantId
) -> QueueEntryRead:
    entry = await service.get_queue_entry(session, entry_id, hospital_id=tenant_id)
    updated = await service.start_consultation(session, entry, actor_id=context.user.id)
    await session.commit()
    return QueueEntryRead.model_validate(updated)


@queue_router.post(
    "/{entry_id}/complete", response_model=QueueEntryRead, summary="Finish the consultation"
)
async def complete_consultation(
    entry_id: uuid.UUID, session: SessionDep, context: CanManageQueue, tenant_id: TenantId
) -> QueueEntryRead:
    entry = await service.get_queue_entry(session, entry_id, hospital_id=tenant_id)
    updated = await service.complete_consultation(session, entry, actor_id=context.user.id)
    await session.commit()
    return QueueEntryRead.model_validate(updated)


@queue_router.post(
    "/{entry_id}/skip", response_model=QueueEntryRead, summary="Patient did not come forward"
)
async def skip(
    entry_id: uuid.UUID,
    payload: SkipRequest,
    session: SessionDep,
    context: CanManageQueue,
    tenant_id: TenantId,
) -> QueueEntryRead:
    entry = await service.get_queue_entry(session, entry_id, hospital_id=tenant_id)
    updated = await service.skip_token(session, entry, reason=payload.reason)
    await session.commit()
    return QueueEntryRead.model_validate(updated)


@queue_router.post(
    "/{entry_id}/reassign",
    response_model=QueueEntryRead,
    summary="Move a waiting patient to another doctor",
)
async def reassign(
    entry_id: uuid.UUID,
    payload: QueueReassignRequest,
    session: SessionDep,
    request: Request,
    context: CanManageQueue,
    tenant_id: TenantId,
) -> QueueEntryRead:
    """Reception's load-balancing action.

    Three records name the doctor — the token, the booking and the chart — and
    all three move in one transaction. The chart goes through `clinical`'s
    service (CLAUDE.md §2), which refuses once the consultation has begun; a
    refusal from either module rolls the whole move back, so a rejected move
    burns no token number in the new doctor's series.
    """
    entry = await service.get_queue_entry(session, entry_id, hospital_id=tenant_id)
    before = {"doctor_id": str(entry.doctor_id), "token": entry.token_number}

    encounter = None
    if entry.appointment_id is not None:
        encounter = await clinical_service.get_encounter_by_appointment(
            session, entry.appointment_id, hospital_id=tenant_id
        )

    updated = await service.reassign_queue_entry(
        session,
        entry,
        to_doctor_id=payload.doctor_id,
        reason=payload.reason,
        actor_id=context.user.id,
    )
    if encounter is not None:
        await clinical_service.reassign_doctor(
            session,
            encounter,
            doctor_id=updated.doctor_id,
            department_id=updated.department_id,
        )

    await identity_service.record_audit(
        session,
        action="queue.reassign",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="queue_entry",
        resource_id=updated.id,
        changes={
            "before": before,
            "after": {"doctor_id": str(updated.doctor_id), "token": updated.token_number},
            "patient_id": str(updated.patient_id),
            "reason": payload.reason,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return QueueEntryRead.model_validate(updated)


@queue_router.post(
    "/{entry_id}/reorder",
    response_model=QueueEntryRead,
    summary="Change a token's place in its doctor's line",
)
async def reorder(
    entry_id: uuid.UUID,
    payload: QueueReorderRequest,
    session: SessionDep,
    request: Request,
    context: CanManageQueue,
    tenant_id: TenantId,
) -> QueueEntryRead:
    """The drag-and-drop on reception's board (CLAUDE.md §7b).

    Changes rank only — never the priority tier, which stays a fact about the
    patient (senior citizen, emergency) rather than about where staff chose
    to put them today. Audited: jumping somebody up the line is a decision
    that has to be answerable afterwards.
    """
    entry = await service.get_queue_entry(session, entry_id, hospital_id=tenant_id)
    if payload.after_entry_id is not None:
        # Existence and tenant are checked here; "same line" is the service's.
        await service.get_queue_entry(session, payload.after_entry_id, hospital_id=tenant_id)
    before = entry.position

    updated = await service.reorder_queue_entry(
        session, entry, after_entry_id=payload.after_entry_id
    )
    await identity_service.record_audit(
        session,
        action="queue.reorder",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="queue_entry",
        resource_id=updated.id,
        changes={
            "before": {"position": before},
            "after": {"position": updated.position},
            "after_entry_id": str(payload.after_entry_id) if payload.after_entry_id else None,
            "doctor_id": str(updated.doctor_id),
            "patient_id": str(updated.patient_id),
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return QueueEntryRead.model_validate(updated)


@queue_router.post(
    "/{entry_id}/seen",
    response_model=QueueEntryRead,
    summary="Reception marks the patient as seen by the doctor",
)
async def mark_seen(
    entry_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanManageQueue,
    tenant_id: TenantId,
) -> QueueEntryRead:
    """The "Seen" button on reception's board.

    The token and booking close, and the chart moves out of `REGISTERED`
    through `clinical`'s state machine so the night's auto-close does not
    file the visit as a no-show. All in one transaction: a patient is either
    seen everywhere or nowhere.
    """
    entry = await service.get_queue_entry(session, entry_id, hospital_id=tenant_id)
    before = entry.status.value

    encounter = None
    if entry.appointment_id is not None:
        encounter = await clinical_service.get_encounter_by_appointment(
            session, entry.appointment_id, hospital_id=tenant_id
        )

    updated = await service.mark_seen(session, entry)
    if encounter is not None:
        await clinical_service.record_seen_by_staff(session, encounter, actor=context.user)

    await identity_service.record_audit(
        session,
        action="queue.mark_seen",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="queue_entry",
        resource_id=updated.id,
        changes={
            "before": {"status": before},
            "after": {"status": updated.status.value},
            "doctor_id": str(updated.doctor_id),
            "patient_id": str(updated.patient_id),
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return QueueEntryRead.model_validate(updated)


@queue_router.post(
    "/{entry_id}/left",
    response_model=QueueEntryRead,
    summary="Patient left without being seen",
)
async def left_without_being_seen(
    entry_id: uuid.UUID, session: SessionDep, context: CanManageQueue, tenant_id: TenantId
) -> QueueEntryRead:
    entry = await service.get_queue_entry(session, entry_id, hospital_id=tenant_id)
    updated = await service.mark_left_without_being_seen(session, entry)
    await session.commit()
    return QueueEntryRead.model_validate(updated)
