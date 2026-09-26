"""Inpatient HTTP routes. Thin — logic lives in `service.py`.

Four surfaces, because four different people use them:

* **wards / beds** — set up once by an administrator, then read constantly by
  everybody as the bed board.
* **admissions** — admissions desk and ward clerk: admit, transfer, discharge.
* **the chart** — doctors prescribe, nurses sign for doses. The busiest surface
  in the building, and the one held to §7b's 15-second target.
* **summaries** — compiled by the system, reviewed and signed by a doctor.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.exceptions import PermissionDeniedError
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import service as identity_service
from app.modules.identity.service import AuthContext
from app.modules.ipd import service
from app.modules.ipd.models import (
    Admission,
    AdmissionRequest,
    AdmissionRequestStatus,
    AdmissionStatus,
    BedClass,
    BedStatus,
    DoseStatus,
    MedicationAdministration,
    SummaryStatus,
)
from app.modules.ipd.rbac import SIGNING_ROLES, IpdPermissions
from app.modules.ipd.schemas import (
    AdmissionCreate,
    AdmissionRead,
    AdmissionRequestCancel,
    AdmissionRequestCreate,
    AdmissionRequestRead,
    AdmissionSummary,
    AdmissionUpdate,
    AdmitFromRequest,
    BedCreate,
    BedRead,
    BedUpdate,
    BoardWard,
    CancelAdmission,
    DischargeRequest,
    DoseRead,
    DoseRecord,
    MedicationOrderCreate,
    MedicationOrderRead,
    MedicationStop,
    OccupancyStats,
    OutOfServiceRequest,
    ReserveRequest,
    SignSummary,
    SummaryRead,
    SummaryUpdate,
    TransferRequest,
    WardCreate,
    WardRead,
    WardUpdate,
)
from app.modules.patients import service as patients_service
from app.modules.patients.schemas import PatientRead

wards_router = APIRouter(prefix="/ipd/wards", tags=["ipd"])
beds_router = APIRouter(prefix="/ipd/beds", tags=["ipd"])
admissions_router = APIRouter(prefix="/ipd/admissions", tags=["ipd"])
chart_router = APIRouter(prefix="/ipd", tags=["ipd"])
summaries_router = APIRouter(prefix="/ipd/summaries", tags=["ipd"])
admission_requests_router = APIRouter(prefix="/ipd/admission-requests", tags=["ipd"])

CanReadWard = Annotated[AuthContext, Depends(require(IpdPermissions.WARD_READ))]
CanManageWard = Annotated[AuthContext, Depends(require(IpdPermissions.WARD_MANAGE))]
CanReadBed = Annotated[AuthContext, Depends(require(IpdPermissions.BED_READ))]
CanAssignBed = Annotated[AuthContext, Depends(require(IpdPermissions.BED_ASSIGN))]
CanCleanBed = Annotated[AuthContext, Depends(require(IpdPermissions.BED_CLEAN))]
CanBlockBed = Annotated[AuthContext, Depends(require(IpdPermissions.BED_BLOCK))]
CanReadAdmission = Annotated[AuthContext, Depends(require(IpdPermissions.ADMISSION_READ))]
CanAdmit = Annotated[AuthContext, Depends(require(IpdPermissions.ADMISSION_CREATE))]
CanUpdateAdmission = Annotated[AuthContext, Depends(require(IpdPermissions.ADMISSION_UPDATE))]
CanDischarge = Annotated[AuthContext, Depends(require(IpdPermissions.ADMISSION_DISCHARGE))]
CanCancelAdmission = Annotated[AuthContext, Depends(require(IpdPermissions.ADMISSION_CANCEL))]
CanReadChart = Annotated[AuthContext, Depends(require(IpdPermissions.MEDICATION_READ))]
CanPrescribe = Annotated[AuthContext, Depends(require(IpdPermissions.MEDICATION_PRESCRIBE))]
CanAdminister = Annotated[AuthContext, Depends(require(IpdPermissions.MEDICATION_ADMINISTER))]
CanReadSummary = Annotated[AuthContext, Depends(require(IpdPermissions.SUMMARY_READ))]
CanWriteSummary = Annotated[AuthContext, Depends(require(IpdPermissions.SUMMARY_WRITE))]
CanSignSummary = Annotated[AuthContext, Depends(require(IpdPermissions.SUMMARY_SIGN))]
CanRequestAdmission = Annotated[AuthContext, Depends(require(IpdPermissions.ADMISSION_REQUEST))]
CanWorkDesk = Annotated[AuthContext, Depends(require(IpdPermissions.ADMISSION_DESK))]
# Either side of the hand-off: the counter that sent the patient needs to see
# what became of them, and either may withdraw a request sent in error.
CanSeeAdmissionRequests = Annotated[
    AuthContext,
    Depends(require(IpdPermissions.ADMISSION_REQUEST, IpdPermissions.ADMISSION_DESK)),
]


async def _render_admission(
    session: SessionDep, admission: Admission, *, hospital_id: uuid.UUID
) -> AdmissionRead:
    """The admission plus where the patient actually is.

    One response rather than three requests: a ward screen that fetches the bed
    separately is a ward screen showing a patient with no location for half a
    second, and staff read that as "the system has lost them".
    """
    rendered = AdmissionRead.model_validate(admission)
    rendered.length_of_stay_days = service.length_of_stay(admission)
    assignment = await service.current_assignment(session, admission.id)
    if assignment is not None:
        bed = await service.get_bed(session, assignment.bed_id, hospital_id=hospital_id)
        rendered.bed = BedRead.model_validate(bed)
        rendered.ward = WardRead.model_validate(
            await service.get_ward(session, bed.ward_id, hospital_id=hospital_id)
        )

    patients = await patients_service.get_patients_by_ids(
        session, [admission.patient_id], hospital_id=hospital_id
    )
    patient = patients.get(admission.patient_id)
    if patient is not None:
        # Through `PatientRead` so age comes from the one implementation that
        # owns that arithmetic (CLAUDE.md §2), not a second copy here.
        view = PatientRead.model_validate(patient)
        rendered.patient_name = view.full_name
        rendered.uhid = view.uhid
        rendered.patient_age_years = view.age_years
        rendered.patient_gender = str(view.gender)
        rendered.patient_is_deceased = view.is_deceased
    return rendered


async def _render_census(
    session: SessionDep, admissions: Sequence[Admission], *, hospital_id: uuid.UUID
) -> list[AdmissionSummary]:
    """Census rows with the patient and the bed attached, in bulk.

    Three lookups for the whole page rather than three per row — the same shape
    as the queue, lab and counter boards, and for the same reason: a ward round
    reads this list, and a list that costs a request per patient gets slower
    exactly as the ward gets busier.
    """
    patients = await patients_service.get_patients_by_ids(
        session, [row.patient_id for row in admissions], hospital_id=hospital_id
    )
    beds = await service.get_current_beds(
        session, [row.id for row in admissions], hospital_id=hospital_id
    )
    wards = await service.get_wards_by_ids(
        session, {bed.ward_id for bed in beds.values()}, hospital_id=hospital_id
    )

    rows: list[AdmissionSummary] = []
    for admission in admissions:
        row = AdmissionSummary.model_validate(admission)

        patient = patients.get(admission.patient_id)
        if patient is not None:
            view = PatientRead.model_validate(patient)
            row.patient_name = view.full_name
            row.uhid = view.uhid
            row.patient_age_years = view.age_years
            row.patient_gender = str(view.gender)

        bed = beds.get(admission.id)
        if bed is not None:
            row.bed_code = bed.code
            ward = wards.get(bed.ward_id)
            row.ward_name = ward.name if ward is not None else None
        rows.append(row)
    return rows


async def _render_doses(
    session: SessionDep, doses: Sequence[MedicationAdministration], *, hospital_id: uuid.UUID
) -> list[DoseRead]:
    """Doses with the patient and the bed attached, in bulk.

    Three lookups for the whole round rather than three per dose. This is the
    §7b fifteen-second action and the list is long — a ward of twenty patients
    on four drugs each is eighty rows before lunch.
    """
    patients = await patients_service.get_patients_by_ids(
        session, [dose.patient_id for dose in doses], hospital_id=hospital_id
    )
    beds = await service.get_current_beds(
        session, [dose.admission_id for dose in doses], hospital_id=hospital_id
    )
    wards = await service.get_wards_by_ids(
        session, {bed.ward_id for bed in beds.values()}, hospital_id=hospital_id
    )

    rows: list[DoseRead] = []
    for dose in doses:
        row = DoseRead.model_validate(dose)

        patient = patients.get(dose.patient_id)
        if patient is not None:
            view = PatientRead.model_validate(patient)
            row.patient_name = view.full_name
            row.uhid = view.uhid
            row.patient_is_deceased = view.is_deceased

        bed = beds.get(dose.admission_id)
        if bed is not None:
            row.bed_code = bed.code
            ward = wards.get(bed.ward_id)
            row.ward_name = ward.name if ward is not None else None
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Wards
# ---------------------------------------------------------------------------
@wards_router.post(
    "", response_model=WardRead, status_code=status.HTTP_201_CREATED, summary="Create a ward"
)
async def create_ward(
    payload: WardCreate, session: SessionDep, context: CanManageWard, tenant_id: TenantId
) -> WardRead:
    ward = await service.create_ward(session, payload, hospital_id=tenant_id)
    await session.commit()
    return WardRead.model_validate(ward)


@wards_router.get("", response_model=Page[WardRead], summary="List wards")
async def list_wards(
    session: SessionDep,
    context: CanReadWard,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    include_inactive: bool = False,
) -> Page[WardRead]:
    rows, total = await service.list_wards(
        session, params, hospital_id=tenant_id, include_inactive=include_inactive
    )
    return Page.build([WardRead.model_validate(row) for row in rows], total=total, params=params)


@wards_router.get("/board", response_model=list[BoardWard], summary="The bed board")
async def bed_board(
    session: SessionDep,
    context: CanReadBed,
    tenant_id: TenantId,
    ward_id: uuid.UUID | None = None,
) -> list[BoardWard]:
    """Every ward, every bed, who is in it — in one round trip.

    Not paginated, deliberately, and the one place in the system that is exempt
    from CLAUDE.md §11's rule. The response is bounded by the hospital's
    physical bed count, which is a number in the hundreds that changes when
    somebody builds a wing. A paginated bed board is a bed board nobody can see
    at a glance, which defeats its only purpose.
    """
    board = await service.build_board(session, hospital_id=tenant_id, ward_id=ward_id)
    return [BoardWard.model_validate(entry) for entry in board]


@wards_router.get("/occupancy", response_model=OccupancyStats, summary="Occupancy at a glance")
async def occupancy(
    session: SessionDep, context: CanReadBed, tenant_id: TenantId
) -> OccupancyStats:
    return OccupancyStats.model_validate(await service.occupancy(session, hospital_id=tenant_id))


@wards_router.get("/{ward_id}", response_model=WardRead, summary="One ward")
async def get_ward(
    ward_id: uuid.UUID, session: SessionDep, context: CanReadWard, tenant_id: TenantId
) -> WardRead:
    return WardRead.model_validate(await service.get_ward(session, ward_id, hospital_id=tenant_id))


@wards_router.patch("/{ward_id}", response_model=WardRead, summary="Edit a ward")
async def update_ward(
    ward_id: uuid.UUID,
    payload: WardUpdate,
    session: SessionDep,
    context: CanManageWard,
    tenant_id: TenantId,
) -> WardRead:
    ward = await service.get_ward(session, ward_id, hospital_id=tenant_id)
    updated = await service.update_ward(session, ward, payload)
    await session.commit()
    return WardRead.model_validate(updated)


# ---------------------------------------------------------------------------
# Beds
# ---------------------------------------------------------------------------
@beds_router.post(
    "", response_model=BedRead, status_code=status.HTTP_201_CREATED, summary="Add a bed"
)
async def create_bed(
    payload: BedCreate, session: SessionDep, context: CanManageWard, tenant_id: TenantId
) -> BedRead:
    bed = await service.create_bed(session, payload, hospital_id=tenant_id)
    await session.commit()
    return BedRead.model_validate(bed)


@beds_router.get("", response_model=Page[BedRead], summary="List beds")
async def list_beds(
    session: SessionDep,
    context: CanReadBed,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    ward_id: uuid.UUID | None = None,
    bed_status: Annotated[BedStatus | None, Query(alias="status")] = None,
    bed_class: BedClass | None = None,
    include_inactive: bool = False,
) -> Page[BedRead]:
    rows, total = await service.list_beds(
        session,
        params,
        hospital_id=tenant_id,
        ward_id=ward_id,
        status=bed_status,
        bed_class=bed_class,
        include_inactive=include_inactive,
    )
    return Page.build([BedRead.model_validate(row) for row in rows], total=total, params=params)


@beds_router.get("/{bed_id}", response_model=BedRead, summary="One bed")
async def get_bed(
    bed_id: uuid.UUID, session: SessionDep, context: CanReadBed, tenant_id: TenantId
) -> BedRead:
    return BedRead.model_validate(await service.get_bed(session, bed_id, hospital_id=tenant_id))


@beds_router.patch("/{bed_id}", response_model=BedRead, summary="Edit a bed")
async def update_bed(
    bed_id: uuid.UUID,
    payload: BedUpdate,
    session: SessionDep,
    context: CanManageWard,
    tenant_id: TenantId,
) -> BedRead:
    bed = await service.get_bed(session, bed_id, hospital_id=tenant_id)
    updated = await service.update_bed(session, bed, payload)
    await session.commit()
    return BedRead.model_validate(updated)


@beds_router.post("/{bed_id}/reserve", response_model=BedRead, summary="Hold a bed")
async def reserve_bed(
    bed_id: uuid.UUID,
    payload: ReserveRequest,
    session: SessionDep,
    context: CanAssignBed,
    tenant_id: TenantId,
) -> BedRead:
    bed = await service.get_bed(session, bed_id, hospital_id=tenant_id)
    updated = await service.reserve_bed(
        session, bed, patient_id=payload.patient_id, hospital_id=tenant_id
    )
    await session.commit()
    return BedRead.model_validate(updated)


@beds_router.post("/{bed_id}/cleaned", response_model=BedRead, summary="Mark a bed cleaned")
async def mark_cleaned(
    bed_id: uuid.UUID, session: SessionDep, context: CanCleanBed, tenant_id: TenantId
) -> BedRead:
    """Housekeeping's one action, and the rung that keeps the board honest."""
    bed = await service.get_bed(session, bed_id, hospital_id=tenant_id)
    updated = await service.mark_bed_cleaned(session, bed)
    await session.commit()
    return BedRead.model_validate(updated)


@beds_router.post(
    "/{bed_id}/out-of-service", response_model=BedRead, summary="Take a bed out of service"
)
async def take_out_of_service(
    bed_id: uuid.UUID,
    payload: OutOfServiceRequest,
    request: Request,
    session: SessionDep,
    context: CanBlockBed,
    tenant_id: TenantId,
) -> BedRead:
    bed = await service.get_bed(session, bed_id, hospital_id=tenant_id)
    updated = await service.take_bed_out_of_service(session, bed, reason=payload.reason)
    await identity_service.record_audit(
        session,
        action="bed.out_of_service",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="bed",
        resource_id=bed.id,
        changes={"bed": bed.code, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return BedRead.model_validate(updated)


@beds_router.post("/{bed_id}/restore", response_model=BedRead, summary="Return a bed to service")
async def restore_bed(
    bed_id: uuid.UUID, session: SessionDep, context: CanBlockBed, tenant_id: TenantId
) -> BedRead:
    bed = await service.get_bed(session, bed_id, hospital_id=tenant_id)
    updated = await service.restore_bed(session, bed)
    await session.commit()
    return BedRead.model_validate(updated)


# ---------------------------------------------------------------------------
# Admissions
# ---------------------------------------------------------------------------
@admissions_router.post(
    "", response_model=AdmissionRead, status_code=status.HTTP_201_CREATED, summary="Admit a patient"
)
async def admit(
    payload: AdmissionCreate,
    request: Request,
    session: SessionDep,
    context: CanAdmit,
    tenant_id: TenantId,
) -> AdmissionRead:
    admission = await service.admit_patient(
        session, payload, hospital_id=tenant_id, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="admission.create",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="admission",
        resource_id=admission.id,
        changes={
            "admission_number": admission.admission_number,
            "encounter_id": str(admission.encounter_id),
            "bed_id": str(payload.bed_id),
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_admission(session, admission, hospital_id=tenant_id)


@admissions_router.get("", response_model=Page[AdmissionSummary], summary="The ward census")
async def list_admissions(
    session: SessionDep,
    context: CanReadAdmission,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    admission_status: Annotated[AdmissionStatus | None, Query(alias="status")] = None,
    ward_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    attending_doctor_id: uuid.UUID | None = None,
    open_only: bool = False,
) -> Page[AdmissionSummary]:
    rows, total = await service.list_admissions(
        session,
        params,
        hospital_id=tenant_id,
        status=admission_status,
        ward_id=ward_id,
        patient_id=patient_id,
        attending_doctor_id=attending_doctor_id,
        open_only=open_only,
    )
    return Page.build(
        await _render_census(session, rows, hospital_id=tenant_id), total=total, params=params
    )


@admissions_router.get("/{admission_id}", response_model=AdmissionRead, summary="One admission")
async def get_admission(
    admission_id: uuid.UUID,
    session: SessionDep,
    context: CanReadAdmission,
    tenant_id: TenantId,
) -> AdmissionRead:
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    return await _render_admission(session, admission, hospital_id=tenant_id)


@admissions_router.patch(
    "/{admission_id}", response_model=AdmissionRead, summary="Edit admission details"
)
async def update_admission(
    admission_id: uuid.UUID,
    payload: AdmissionUpdate,
    session: SessionDep,
    context: CanUpdateAdmission,
    tenant_id: TenantId,
) -> AdmissionRead:
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    updated = await service.update_admission(session, admission, payload)
    await session.commit()
    return await _render_admission(session, updated, hospital_id=tenant_id)


@admissions_router.post(
    "/{admission_id}/transfer", response_model=AdmissionRead, summary="Move to another bed"
)
async def transfer(
    admission_id: uuid.UUID,
    payload: TransferRequest,
    request: Request,
    session: SessionDep,
    context: CanAssignBed,
    tenant_id: TenantId,
) -> AdmissionRead:
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    to_bed = await service.get_bed(session, payload.to_bed_id, hospital_id=tenant_id)
    await service.transfer_patient(
        session, admission, to_bed=to_bed, reason=payload.reason, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="admission.transfer",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="admission",
        resource_id=admission.id,
        changes={"to_bed": to_bed.code, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_admission(session, admission, hospital_id=tenant_id)


@admissions_router.post(
    "/{admission_id}/initiate-discharge",
    response_model=AdmissionRead,
    summary="Doctor says the patient can go",
)
async def initiate_discharge(
    admission_id: uuid.UUID,
    session: SessionDep,
    context: CanDischarge,
    tenant_id: TenantId,
) -> AdmissionRead:
    """Starts the paperwork and compiles the summary. The bed stays theirs."""
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    updated = await service.initiate_discharge(session, admission, actor=context.user)
    await session.commit()
    return await _render_admission(session, updated, hospital_id=tenant_id)


@admissions_router.post(
    "/{admission_id}/discharge", response_model=AdmissionRead, summary="Discharge the patient"
)
async def discharge(
    admission_id: uuid.UUID,
    payload: DischargeRequest,
    request: Request,
    session: SessionDep,
    context: CanDischarge,
    tenant_id: TenantId,
) -> AdmissionRead:
    """The patient leaves: bed freed, chart stopped, Encounter closed.

    A death or a self-discharge is refused here by the schema — both are
    recorded against the visit through `clinical`, which captures the metadata
    CLAUDE.md §6 requires, and this module follows automatically.
    """
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    updated = await service.discharge_patient(session, admission, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="admission.discharge",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="admission",
        resource_id=admission.id,
        changes={
            "admission_number": admission.admission_number,
            "discharge_type": payload.discharge_type.value,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_admission(session, updated, hospital_id=tenant_id)


@admissions_router.post(
    "/{admission_id}/cancel", response_model=AdmissionRead, summary="Cancel an admission in error"
)
async def cancel(
    admission_id: uuid.UUID,
    payload: CancelAdmission,
    request: Request,
    session: SessionDep,
    context: CanCancelAdmission,
    tenant_id: TenantId,
) -> AdmissionRead:
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    updated = await service.cancel_admission(
        session, admission, reason=payload.reason, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="admission.cancel",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="admission",
        resource_id=admission.id,
        changes={"admission_number": admission.admission_number, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_admission(session, updated, hospital_id=tenant_id)


# ---------------------------------------------------------------------------
# The medication chart
# ---------------------------------------------------------------------------
@chart_router.post(
    "/admissions/{admission_id}/medications",
    response_model=MedicationOrderRead,
    status_code=status.HTTP_201_CREATED,
    summary="Prescribe a drug",
)
async def prescribe(
    admission_id: uuid.UUID,
    payload: MedicationOrderCreate,
    request: Request,
    session: SessionDep,
    context: CanPrescribe,
    tenant_id: TenantId,
) -> MedicationOrderRead:
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    order = await service.prescribe(session, admission, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="medication.prescribe",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="medication_order",
        resource_id=order.id,
        changes={
            "drug": order.drug_name,
            "dose": order.dose,
            "frequency": order.frequency,
            "schedule": order.drug_schedule.value,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return MedicationOrderRead.model_validate(order)


@chart_router.get(
    "/admissions/{admission_id}/medications",
    response_model=Page[MedicationOrderRead],
    summary="The drug chart",
)
async def list_medications(
    admission_id: uuid.UUID,
    session: SessionDep,
    context: CanReadChart,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    active_only: bool = True,
) -> Page[MedicationOrderRead]:
    rows, total = await service.list_medication_orders(
        session,
        params,
        hospital_id=tenant_id,
        admission_id=admission_id,
        active_only=active_only,
    )
    return Page.build(
        [MedicationOrderRead.model_validate(row) for row in rows], total=total, params=params
    )


@chart_router.post(
    "/medications/{order_id}/stop", response_model=MedicationOrderRead, summary="Stop a drug"
)
async def stop_medication(
    order_id: uuid.UUID,
    payload: MedicationStop,
    request: Request,
    session: SessionDep,
    context: CanPrescribe,
    tenant_id: TenantId,
) -> MedicationOrderRead:
    order = await service.get_medication_order(session, order_id, hospital_id=tenant_id)
    updated = await service.stop_medication(
        session, order, reason=payload.reason, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="medication.stop",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="medication_order",
        resource_id=order.id,
        changes={"drug": order.drug_name, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return MedicationOrderRead.model_validate(updated)


@chart_router.get("/doses", response_model=Page[DoseRead], summary="Doses due")
async def list_doses(
    session: SessionDep,
    context: CanReadChart,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    admission_id: uuid.UUID | None = None,
    dose_status: Annotated[DoseStatus | None, Query(alias="status")] = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
) -> Page[DoseRead]:
    """The nurse's round: what is due, on whom, in this window."""
    rows, total = await service.list_doses(
        session,
        params,
        hospital_id=tenant_id,
        admission_id=admission_id,
        status=dose_status,
        due_from=due_from,
        due_to=due_to,
    )
    return Page.build(
        await _render_doses(session, rows, hospital_id=tenant_id), total=total, params=params
    )


@chart_router.post("/doses/{dose_id}", response_model=DoseRead, summary="Sign for a dose")
async def record_dose(
    dose_id: uuid.UUID,
    payload: DoseRecord,
    request: Request,
    session: SessionDep,
    context: CanAdminister,
    tenant_id: TenantId,
) -> DoseRead:
    """One tap at the bedside — §7b's 15-second nurse action.

    Audited every time, without exception. A medication administration record
    that cannot say who signed for a dose is not a medication administration
    record, and this is the row an incident review reads first.
    """
    dose = await service.get_dose(session, dose_id, hospital_id=tenant_id)
    updated = await service.record_dose(session, dose, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="medication.administer",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="medication_administration",
        resource_id=dose.id,
        changes={
            "drug": dose.drug_name,
            "dose": dose.dose,
            "outcome": payload.status.value,
            "reason": payload.reason,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return (await _render_doses(session, [updated], hospital_id=tenant_id))[0]


# ---------------------------------------------------------------------------
# Discharge summaries
# ---------------------------------------------------------------------------
@summaries_router.get(
    "", response_model=Page[SummaryRead], summary="Summaries, and what is unsigned"
)
async def list_summaries(
    session: SessionDep,
    context: CanReadSummary,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    summary_status: Annotated[SummaryStatus | None, Query(alias="status")] = None,
    patient_id: uuid.UUID | None = None,
) -> Page[SummaryRead]:
    """`?status=DRAFT` is the consultant's outstanding-paperwork worklist."""
    rows, total = await service.list_summaries(
        session, params, hospital_id=tenant_id, status=summary_status, patient_id=patient_id
    )
    return Page.build([SummaryRead.model_validate(row) for row in rows], total=total, params=params)


@summaries_router.post(
    "/compile/{admission_id}", response_model=SummaryRead, summary="Compile a draft summary"
)
async def compile_summary(
    admission_id: uuid.UUID,
    session: SessionDep,
    context: CanWriteSummary,
    tenant_id: TenantId,
) -> SummaryRead:
    """Assemble the draft from what is already recorded. Nothing is invented."""
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    summary = await service.compile_summary(session, admission, actor=context.user)
    await session.commit()
    return SummaryRead.model_validate(summary)


@summaries_router.get(
    "/by-admission/{admission_id}", response_model=SummaryRead, summary="A stay's summary"
)
async def get_summary(
    admission_id: uuid.UUID,
    session: SessionDep,
    context: CanReadSummary,
    tenant_id: TenantId,
) -> SummaryRead:
    admission = await service.get_admission(session, admission_id, hospital_id=tenant_id)
    summary = await service.get_summary(session, admission.id)
    if summary is None:
        summary = await service.compile_summary(session, admission, actor=context.user)
        await session.commit()
    return SummaryRead.model_validate(summary)


@summaries_router.patch("/{summary_id}", response_model=SummaryRead, summary="Edit a summary")
async def update_summary(
    summary_id: uuid.UUID,
    payload: SummaryUpdate,
    session: SessionDep,
    context: CanWriteSummary,
    tenant_id: TenantId,
) -> SummaryRead:
    summary = await service.get_summary_by_id(session, summary_id, hospital_id=tenant_id)
    updated = await service.update_summary(session, summary, payload)
    await session.commit()
    return SummaryRead.model_validate(updated)


@summaries_router.post("/{summary_id}/sign", response_model=SummaryRead, summary="Sign and release")
async def sign_summary(
    summary_id: uuid.UUID,
    payload: SignSummary,
    request: Request,
    session: SessionDep,
    context: CanSignSummary,
    tenant_id: TenantId,
) -> SummaryRead:
    """The clinical signature — and the one place a role check backs the permission.

    A signature carries a registration number and clinical responsibility. An
    administrator who holds `summary:sign` for operational reasons is not a
    clinician, so the role is checked as well.
    """
    if not (set(context.roles) & SIGNING_ROLES):
        raise PermissionDeniedError(
            "A discharge summary is signed by a doctor.",
            details={"roles": sorted(context.roles)},
        )

    summary = await service.get_summary_by_id(session, summary_id, hospital_id=tenant_id)
    signed = await service.sign_summary(
        session, summary, actor=context.user, registration_number=payload.registration_number
    )
    await identity_service.record_audit(
        session,
        action="summary.sign",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="discharge_summary",
        resource_id=summary.id,
        changes={
            "admission_id": str(summary.admission_id),
            "registration_number": payload.registration_number,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return SummaryRead.model_validate(signed)


# ---------------------------------------------------------------------------
# Admission requests — the OPD → admission desk hand-off
# ---------------------------------------------------------------------------
async def _render_requests(
    session: SessionDep, requests: Sequence[AdmissionRequest], *, hospital_id: uuid.UUID
) -> list[AdmissionRequestRead]:
    """Name the patients — one bulk lookup through `patients.service` (§2)."""
    patients = await patients_service.get_patients_by_ids(
        session, [request.patient_id for request in requests], hospital_id=hospital_id
    )
    rendered: list[AdmissionRequestRead] = []
    for request in requests:
        row = AdmissionRequestRead.model_validate(request)
        patient = patients.get(request.patient_id)
        if patient is not None:
            view = PatientRead.model_validate(patient)
            row.patient_name = view.full_name
            row.patient_uhid = view.uhid
            row.patient_age_years = view.age_years
            row.patient_gender = str(view.gender)
            row.patient_phone = view.phone
        rendered.append(row)
    return rendered


@admission_requests_router.post(
    "",
    response_model=AdmissionRequestRead,
    status_code=status.HTTP_201_CREATED,
    summary="Send a seen patient to the admission desk",
)
async def request_admission(
    payload: AdmissionRequestCreate,
    request: Request,
    session: SessionDep,
    context: CanRequestAdmission,
    tenant_id: TenantId,
) -> AdmissionRequestRead:
    created = await service.request_admission(
        session,
        hospital_id=tenant_id,
        encounter_id=payload.encounter_id,
        note=payload.note,
        actor=context.user,
    )
    await identity_service.record_audit(
        session,
        action="admission_request.create",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="admission_request",
        resource_id=created.id,
        changes={
            "encounter_id": str(created.encounter_id),
            "patient_id": str(created.patient_id),
            "note": created.note,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    [rendered] = await _render_requests(session, [created], hospital_id=tenant_id)
    return rendered


@admission_requests_router.get(
    "", response_model=Page[AdmissionRequestRead], summary="Patients sent for admission"
)
async def list_admission_requests(
    session: SessionDep,
    context: CanSeeAdmissionRequests,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    statuses: Annotated[list[AdmissionRequestStatus] | None, Query(alias="status")] = None,
    since: datetime | None = None,
    newest_first: bool = False,
) -> Page[AdmissionRequestRead]:
    """The desk's worklist (`?status=PENDING`), and the counter's view of what
    became of the patients it sent today (`?since=<midnight>`)."""
    requests, total = await service.list_admission_requests(
        session,
        params,
        hospital_id=tenant_id,
        statuses=statuses,
        since=since,
        newest_first=newest_first,
    )
    return Page.build(
        await _render_requests(session, requests, hospital_id=tenant_id),
        total=total,
        params=params,
    )


@admission_requests_router.post(
    "/{request_id}/admit",
    response_model=AdmissionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Admit a patient sent from OPD",
)
async def admit_from_request(
    request_id: uuid.UUID,
    payload: AdmitFromRequest,
    request: Request,
    session: SessionDep,
    context: CanWorkDesk,
    _can_admit: CanAdmit,
    tenant_id: TenantId,
) -> AdmissionRead:
    """The desk's Admit button. Needs both `admission:desk` and
    `admission:create` — working the list is not by itself licence to fill
    beds, and a custom role given one without the other should not do both."""
    waiting = await service.get_admission_request(session, request_id, hospital_id=tenant_id)
    admission = await service.admit_from_request(session, waiting, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="admission.create",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="admission",
        resource_id=admission.id,
        changes={
            "admission_number": admission.admission_number,
            "encounter_id": str(admission.encounter_id),
            "bed_id": str(payload.bed_id),
            "admission_request_id": str(waiting.id),
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render_admission(session, admission, hospital_id=tenant_id)


@admission_requests_router.post(
    "/{request_id}/cancel",
    response_model=AdmissionRequestRead,
    summary="Turn a request away",
)
async def cancel_admission_request(
    request_id: uuid.UUID,
    payload: AdmissionRequestCancel,
    request: Request,
    session: SessionDep,
    context: CanSeeAdmissionRequests,
    tenant_id: TenantId,
) -> AdmissionRequestRead:
    waiting = await service.get_admission_request(session, request_id, hospital_id=tenant_id)
    cancelled = await service.cancel_admission_request(
        session, waiting, reason=payload.reason, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="admission_request.cancel",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="admission_request",
        resource_id=cancelled.id,
        changes={"reason": payload.reason, "patient_id": str(cancelled.patient_id)},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    [rendered] = await _render_requests(session, [cancelled], hospital_id=tenant_id)
    return rendered
