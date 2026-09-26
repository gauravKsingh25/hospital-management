"""Diagnostics HTTP routes. Thin — logic lives in `service.py`.

Three surfaces, because three different people use them: the catalogue is set
up once by an administrator, the specimen worklist is worked by nurses and
technicians all day, and the report is where results are entered and signed.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.clinical import service as clinical_service
from app.modules.diagnostics import service
from app.modules.diagnostics.models import (
    DiagnosticDiscipline,
    DiagnosticReport,
    ReportStatus,
    SpecimenStatus,
)
from app.modules.diagnostics.rbac import DiagnosticsPermissions
from app.modules.diagnostics.schemas import (
    AccessionRequest,
    AmendRequest,
    AnalyteCreate,
    AnalyteRead,
    CatalogueItemCreate,
    CatalogueItemRead,
    CatalogueItemUpdate,
    CollectRequest,
    CriticalCallback,
    NarrativeEntry,
    ReferenceRangeCreate,
    ReferenceRangeRead,
    RejectRequest,
    ReportRead,
    ReportSummary,
    ResultsSubmission,
    ResultValueRead,
    SpecimenRead,
    WorklistEntry,
)
from app.modules.identity import service as identity_service
from app.modules.identity.service import AuthContext
from app.modules.patients import service as patients_service
from app.modules.patients.schemas import PatientRead
from app.modules.scheduling import service as scheduling_service

catalogue_router = APIRouter(prefix="/diagnostics/catalogue", tags=["diagnostics"])
specimens_router = APIRouter(prefix="/diagnostics/specimens", tags=["diagnostics"])
reports_router = APIRouter(prefix="/diagnostics/reports", tags=["diagnostics"])
worklist_router = APIRouter(prefix="/diagnostics/worklist", tags=["diagnostics"])

CanReadCatalogue = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.CATALOGUE_READ))]
CanManageCatalogue = Annotated[
    AuthContext, Depends(require(DiagnosticsPermissions.CATALOGUE_MANAGE))
]
CanReadSpecimen = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.SPECIMEN_READ))]
CanCollect = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.SPECIMEN_COLLECT))]
CanReceive = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.SPECIMEN_RECEIVE))]
CanReadReport = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.REPORT_READ))]
CanEnterResult = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.RESULT_ENTER))]
CanVerify = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.RESULT_VERIFY))]
CanAmend = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.RESULT_AMEND))]
CanCancelReport = Annotated[AuthContext, Depends(require(DiagnosticsPermissions.REPORT_CANCEL))]
CanAcknowledge = Annotated[
    AuthContext, Depends(require(DiagnosticsPermissions.CRITICAL_ACKNOWLEDGE))
]


async def _render(
    session: SessionDep, report: DiagnosticReport, *, hospital_id: uuid.UUID
) -> ReportRead:
    """Assemble the full report: header, values and the sample it came from.

    One response rather than three: a report read in three requests is a report
    whose values arrive after the heading, which is how a clinician ends up
    looking at the wrong patient's numbers.
    """
    rendered = ReportRead.model_validate(report)
    rows = await service.list_results(session, report.id)
    rendered.results = [ResultValueRead.model_validate(row) for row in rows]
    if report.specimen_id is not None:
        specimen = await service.get_specimen(session, report.specimen_id, hospital_id=hospital_id)
        rendered.specimen = SpecimenRead.model_validate(specimen)

    patients = await patients_service.get_patients_by_ids(
        session, [report.patient_id], hospital_id=hospital_id
    )
    patient = patients.get(report.patient_id)
    if patient is not None:
        view = PatientRead.model_validate(patient)
        rendered.patient_name = view.full_name
        rendered.patient_uhid = view.uhid
        rendered.patient_age_years = view.age_years
        rendered.patient_gender = str(view.gender)
        rendered.patient_is_deceased = view.is_deceased
    return rendered


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
@catalogue_router.post(
    "",
    response_model=CatalogueItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an orderable investigation",
)
async def create_catalogue_item(
    payload: CatalogueItemCreate,
    session: SessionDep,
    context: CanManageCatalogue,
    tenant_id: TenantId,
) -> CatalogueItemRead:
    item = await service.create_catalogue_item(session, payload, hospital_id=tenant_id)
    await session.commit()
    return CatalogueItemRead.model_validate(item)


@catalogue_router.get("", response_model=Page[CatalogueItemRead], summary="The test catalogue")
async def list_catalogue_items(
    session: SessionDep,
    context: CanReadCatalogue,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    discipline: DiagnosticDiscipline | None = None,
    search: str | None = None,
    include_inactive: bool = False,
) -> Page[CatalogueItemRead]:
    items, total = await service.list_catalogue_items(
        session,
        params,
        hospital_id=tenant_id,
        discipline=discipline,
        search=search,
        include_inactive=include_inactive,
    )
    return Page.build(
        [CatalogueItemRead.model_validate(item) for item in items], total=total, params=params
    )


@catalogue_router.get("/{item_id}", response_model=CatalogueItemRead, summary="Get a test")
async def get_catalogue_item(
    item_id: uuid.UUID, session: SessionDep, context: CanReadCatalogue, tenant_id: TenantId
) -> CatalogueItemRead:
    return CatalogueItemRead.model_validate(
        await service.get_catalogue_item(session, item_id, hospital_id=tenant_id)
    )


@catalogue_router.patch("/{item_id}", response_model=CatalogueItemRead, summary="Edit a test")
async def update_catalogue_item(
    item_id: uuid.UUID,
    payload: CatalogueItemUpdate,
    session: SessionDep,
    context: CanManageCatalogue,
    tenant_id: TenantId,
) -> CatalogueItemRead:
    item = await service.get_catalogue_item(session, item_id, hospital_id=tenant_id)
    updated = await service.update_catalogue_item(session, item, payload)
    await session.commit()
    return CatalogueItemRead.model_validate(updated)


@catalogue_router.post(
    "/{item_id}/analytes",
    response_model=AnalyteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an analyte and its reference bands",
)
async def add_analyte(
    item_id: uuid.UUID,
    payload: AnalyteCreate,
    session: SessionDep,
    context: CanManageCatalogue,
    tenant_id: TenantId,
) -> AnalyteRead:
    item = await service.get_catalogue_item(session, item_id, hospital_id=tenant_id)
    analyte = await service.add_analyte(session, item, payload)
    await session.commit()
    return AnalyteRead.model_validate(analyte)


@catalogue_router.get(
    "/{item_id}/analytes", response_model=list[AnalyteRead], summary="Analytes on a test"
)
async def list_analytes(
    item_id: uuid.UUID, session: SessionDep, context: CanReadCatalogue, tenant_id: TenantId
) -> list[AnalyteRead]:
    await service.get_catalogue_item(session, item_id, hospital_id=tenant_id)
    rows = await service.list_analytes(session, item_id)
    return [AnalyteRead.model_validate(row) for row in rows]


@catalogue_router.get(
    "/analytes/{analyte_id}/ranges",
    response_model=list[ReferenceRangeRead],
    summary="Reference bands for an analyte",
)
async def list_ranges(
    analyte_id: uuid.UUID, session: SessionDep, context: CanReadCatalogue, tenant_id: TenantId
) -> list[ReferenceRangeRead]:
    rows = await service.list_reference_ranges(session, analyte_id)
    return [ReferenceRangeRead.model_validate(row) for row in rows]


@catalogue_router.post(
    "/analytes/{analyte_id}/ranges",
    response_model=ReferenceRangeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a reference band",
)
async def add_range(
    analyte_id: uuid.UUID,
    payload: ReferenceRangeCreate,
    session: SessionDep,
    context: CanManageCatalogue,
    tenant_id: TenantId,
) -> ReferenceRangeRead:
    analyte = await service.get_analyte(session, analyte_id, hospital_id=tenant_id)
    band = await service.add_reference_range(session, analyte, payload)
    await session.commit()
    return ReferenceRangeRead.model_validate(band)


# ---------------------------------------------------------------------------
# Specimens
# ---------------------------------------------------------------------------
@specimens_router.get("", response_model=Page[SpecimenRead], summary="The sample worklist")
async def list_specimens(
    session: SessionDep,
    context: CanReadSpecimen,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    specimen_status: SpecimenStatus | None = None,
    patient_id: uuid.UUID | None = None,
    pending_only: bool = False,
) -> Page[SpecimenRead]:
    """`pending_only` is the phlebotomy round: everything still to be taken or
    still to reach the bench."""
    rows, total = await service.list_specimens(
        session,
        params,
        hospital_id=tenant_id,
        status=specimen_status,
        patient_id=patient_id,
        pending_only=pending_only,
    )
    return Page.build(
        [SpecimenRead.model_validate(row) for row in rows], total=total, params=params
    )


@specimens_router.get("/{specimen_id}", response_model=SpecimenRead, summary="Get a sample")
async def get_specimen(
    specimen_id: uuid.UUID, session: SessionDep, context: CanReadSpecimen, tenant_id: TenantId
) -> SpecimenRead:
    return SpecimenRead.model_validate(
        await service.get_specimen(session, specimen_id, hospital_id=tenant_id)
    )


@specimens_router.post(
    "/{specimen_id}/collect", response_model=SpecimenRead, summary="Sample taken"
)
async def collect_specimen(
    specimen_id: uuid.UUID,
    payload: CollectRequest,
    session: SessionDep,
    context: CanCollect,
    tenant_id: TenantId,
) -> SpecimenRead:
    specimen = await service.get_specimen(session, specimen_id, hospital_id=tenant_id)
    updated = await service.collect_specimen(session, specimen, payload, actor=context.user)
    await session.commit()
    return SpecimenRead.model_validate(updated)


@specimens_router.post(
    "/{specimen_id}/receive", response_model=SpecimenRead, summary="Sample received at the lab"
)
async def receive_specimen(
    specimen_id: uuid.UUID, session: SessionDep, context: CanReceive, tenant_id: TenantId
) -> SpecimenRead:
    specimen = await service.get_specimen(session, specimen_id, hospital_id=tenant_id)
    updated = await service.receive_specimen(session, specimen, actor=context.user)
    await session.commit()
    return SpecimenRead.model_validate(updated)


@specimens_router.post(
    "/{specimen_id}/reject", response_model=SpecimenRead, summary="Sample unsuitable"
)
async def reject_specimen(
    specimen_id: uuid.UUID,
    payload: RejectRequest,
    session: SessionDep,
    request: Request,
    context: CanReceive,
    tenant_id: TenantId,
) -> SpecimenRead:
    """The order stays open: the test was still asked for and still has to
    happen. A fresh sample is collected against the same order."""
    specimen = await service.get_specimen(session, specimen_id, hospital_id=tenant_id)
    updated = await service.reject_specimen(
        session, specimen, reason=payload.reason, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="specimen.reject",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="specimen",
        resource_id=specimen.id,
        changes={"accession": specimen.accession_number, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return SpecimenRead.model_validate(updated)


# ---------------------------------------------------------------------------
# The bench worklist
# ---------------------------------------------------------------------------
@worklist_router.get("", response_model=Page[WorklistEntry], summary="The diagnostics worklist")
async def get_worklist(
    session: SessionDep,
    context: CanReadReport,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    discipline: DiagnosticDiscipline | None = None,
) -> Page[WorklistEntry]:
    """Every open lab and radiology request, in the order the bench should work
    them, each carrying the one thing it is waiting for.

    Built from *orders*, not reports. A request that nobody has accessioned yet
    has no report to list, and it is exactly the one most likely to be
    forgotten — a worklist assembled from reports only ever shows work somebody
    already remembered to start.

    Three bulk lookups decorate the whole page — reports, samples, patients —
    rather than three per row. The cost is flat in the length of the list, which
    is what stops the busiest screen in the building from being the slowest.
    """
    orders, total = await service.list_worklist_orders(
        session, params, hospital_id=tenant_id, discipline=discipline
    )
    order_ids = [order.id for order in orders]

    reports = await service.get_reports_by_orders(session, order_ids, hospital_id=tenant_id)
    specimens = await service.get_specimens_by_orders(session, order_ids, hospital_id=tenant_id)
    patients = await patients_service.get_patients_by_ids(
        session, [order.patient_id for order in orders], hospital_id=tenant_id
    )

    entries: list[WorklistEntry] = []
    for order in orders:
        report = reports.get(order.id)
        specimen = specimens.get(order.id)
        entry = WorklistEntry(
            order_id=order.id,
            encounter_id=order.encounter_id,
            patient_id=order.patient_id,
            order_type=order.order_type,
            order_status=order.status,
            priority=order.priority,
            item_name=order.item_name,
            item_code=order.item_code,
            instructions=order.instructions,
            review_in_visit=order.review_in_visit,
            ordered_at=order.ordered_at,
            stage=service.worklist_stage(order, report, specimen),
        )
        if report is not None:
            entry.report_id = report.id
            entry.report_number = report.report_number
            entry.report_status = report.status
            entry.catalogue_item_id = report.catalogue_item_id
            entry.has_critical_result = report.has_critical_result
        if specimen is not None:
            entry.specimen_id = specimen.id
            entry.accession_number = specimen.accession_number
            entry.specimen_status = specimen.status

        patient = patients.get(order.patient_id)
        if patient is not None:
            # Through `PatientRead` so age comes from the one implementation
            # that owns that arithmetic, not a second copy of it here.
            view = PatientRead.model_validate(patient)
            entry.patient_name = view.full_name
            entry.patient_uhid = view.uhid
            entry.patient_age_years = view.age_years
            entry.patient_gender = str(view.gender)
            entry.patient_is_deceased = view.is_deceased
        entries.append(entry)

    return Page.build(entries, total=total, params=params)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@reports_router.post(
    "",
    response_model=ReportRead,
    status_code=status.HTTP_201_CREATED,
    summary="Accession an order onto the lab bench",
)
async def accession(
    payload: AccessionRequest,
    session: SessionDep,
    request: Request,
    context: CanEnterResult,
    tenant_id: TenantId,
) -> ReportRead:
    """Creates the report and, for a laboratory test, the sample to be taken.

    The clinical order moves to in-progress, so the doctor's screen and the
    encounter's pending list both say the lab has it.
    """
    order = await clinical_service.get_order(session, payload.order_id, hospital_id=tenant_id)
    clinical_service.assert_may_fulfil(order, context.roles)

    report, _ = await service.accession_order(
        session, order, payload, actor=context.user, hospital_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="diagnostics.accession",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="diagnostic_report",
        resource_id=report.id,
        changes={"report_number": report.report_number, "test": report.test_name},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, report, hospital_id=tenant_id)


@reports_router.get("", response_model=Page[ReportSummary], summary="The report worklist")
async def list_reports(
    session: SessionDep,
    context: CanReadReport,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    report_status: ReportStatus | None = None,
    discipline: DiagnosticDiscipline | None = None,
    patient_id: uuid.UUID | None = None,
    encounter_id: uuid.UUID | None = None,
    open_only: bool = False,
    critical_only: bool = False,
) -> Page[ReportSummary]:
    """Criticals sort to the top, then oldest first."""
    rows, total = await service.list_reports(
        session,
        params,
        hospital_id=tenant_id,
        status=report_status,
        discipline=discipline,
        patient_id=patient_id,
        encounter_id=encounter_id,
        open_only=open_only,
        critical_only=critical_only,
    )
    return Page.build(
        [ReportSummary.model_validate(row) for row in rows], total=total, params=params
    )


@reports_router.get("/{report_id}", response_model=ReportRead, summary="A report and its values")
async def get_report(
    report_id: uuid.UUID, session: SessionDep, context: CanReadReport, tenant_id: TenantId
) -> ReportRead:
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    return await _render(session, report, hospital_id=tenant_id)


@reports_router.post(
    "/{report_id}/results", response_model=ReportRead, summary="Enter measured values"
)
async def enter_results(
    report_id: uuid.UUID,
    payload: ResultsSubmission,
    session: SessionDep,
    context: CanEnterResult,
    tenant_id: TenantId,
) -> ReportRead:
    """Values are flagged against the reference band that fits this patient's
    sex and age, and the band used is copied onto each result."""
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    await service.enter_results(session, report, payload, actor=context.user)
    await session.commit()
    return await _render(session, report, hospital_id=tenant_id)


@reports_router.post(
    "/{report_id}/narrative", response_model=ReportRead, summary="Enter findings and impression"
)
async def enter_narrative(
    report_id: uuid.UUID,
    payload: NarrativeEntry,
    session: SessionDep,
    context: CanEnterResult,
    tenant_id: TenantId,
) -> ReportRead:
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    updated = await service.enter_narrative(session, report, payload, actor=context.user)
    await session.commit()
    return await _render(session, updated, hospital_id=tenant_id)


@reports_router.post(
    "/{report_id}/preliminary", response_model=ReportRead, summary="Release provisionally"
)
async def release_preliminary(
    report_id: uuid.UUID, session: SessionDep, context: CanEnterResult, tenant_id: TenantId
) -> ReportRead:
    """Visible to clinicians, clearly provisional, and it does not close the
    visit — a provisional number is something to look at, not to act on."""
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    updated = await service.release_preliminary(session, report, actor=context.user)
    await session.commit()
    return await _render(session, updated, hospital_id=tenant_id)


@reports_router.post(
    "/{report_id}/verify", response_model=ReportRead, summary="Verify and release the report"
)
async def verify_report(
    report_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanVerify,
    tenant_id: TenantId,
) -> ReportRead:
    """Signing off clears the clinical order — and if it was the last item
    outstanding, the visit closes itself (CLAUDE.md §6)."""
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    service.assert_may_verify(report, context.roles)

    # The signatory's NMC registration number, read through `scheduling`'s
    # service rather than by joining its table. A report without it is not a
    # valid document.
    doctor = await scheduling_service.get_doctor_by_user(
        session, context.user.id, hospital_id=tenant_id
    )
    updated = await service.verify_report(
        session,
        report,
        actor=context.user,
        hospital_id=tenant_id,
        registration_number=doctor.registration_number if doctor else None,
    )
    await identity_service.record_audit(
        session,
        action="diagnostics.verify",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="diagnostic_report",
        resource_id=report.id,
        changes={"report_number": report.report_number, "critical": report.has_critical_result},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, updated, hospital_id=tenant_id)


@reports_router.post(
    "/{report_id}/amend", response_model=ReportRead, summary="Supersede a released report"
)
async def amend_report(
    report_id: uuid.UUID,
    payload: AmendRequest,
    session: SessionDep,
    request: Request,
    context: CanAmend,
    tenant_id: TenantId,
) -> ReportRead:
    """Returns the *replacement*. The original survives as `AMENDED` — somebody
    may already have treated the patient on the strength of it."""
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    service.assert_may_verify(report, context.roles)

    replacement = await service.amend_report(session, report, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="diagnostics.amend",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="diagnostic_report",
        resource_id=report.id,
        changes={"superseded_by": replacement.report_number, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, replacement, hospital_id=tenant_id)


@reports_router.post(
    "/{report_id}/cancel", response_model=ReportRead, summary="Cancel the investigation"
)
async def cancel_report(
    report_id: uuid.UUID,
    payload: AmendRequest,
    session: SessionDep,
    request: Request,
    context: CanCancelReport,
    tenant_id: TenantId,
) -> ReportRead:
    """Cancels the clinical order too, so an abandoned study stops holding the
    visit open."""
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    updated = await service.cancel_report(
        session, report, reason=payload.reason, actor=context.user, hospital_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="diagnostics.cancel",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="diagnostic_report",
        resource_id=report.id,
        changes={"reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, updated, hospital_id=tenant_id)


@reports_router.post(
    "/{report_id}/critical-callback",
    response_model=ReportRead,
    summary="Record a critical-value callback",
)
async def acknowledge_critical(
    report_id: uuid.UUID,
    payload: CriticalCallback,
    session: SessionDep,
    request: Request,
    context: CanAcknowledge,
    tenant_id: TenantId,
) -> ReportRead:
    """NABH wants the call documented: who was told, and when."""
    report = await service.get_report(session, report_id, hospital_id=tenant_id)
    updated = await service.acknowledge_critical(session, report, payload, actor=context.user)
    await identity_service.record_audit(
        session,
        action="diagnostics.critical_callback",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="diagnostic_report",
        resource_id=report.id,
        changes={"notified_to": payload.notified_to},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, updated, hospital_id=tenant_id)
