"""Patient HTTP routes. Thin — logic lives in `service.py`.

Every read of an individual patient record writes an audit row: DPDP Act 2023
requires access to personal data to be traceable, not just modification of it.
Searches are audited as one row carrying the query term rather than one row per
result, which keeps the trail answerable ("who looked this person up?") without
the audit table outgrowing the data it describes.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import Field

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.exceptions import NotFoundError
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import service as identity_service
from app.modules.identity.service import AuthContext
from app.modules.patients import service
from app.modules.patients.models import Patient
from app.modules.patients.rbac import PatientPermissions
from app.modules.patients.schemas import (
    AlertCreate,
    ConsentCreate,
    DuplicateCandidate,
    IdentifierCreate,
    PatientAlertRead,
    PatientConsentRead,
    PatientDetail,
    PatientIdentifierRead,
    PatientMergeRequest,
    PatientRead,
    PatientRegister,
    PatientUpdate,
)

router = APIRouter(prefix="/patients", tags=["patients"])

CanCreate = Annotated[AuthContext, Depends(require(PatientPermissions.CREATE))]
CanRead = Annotated[AuthContext, Depends(require(PatientPermissions.READ))]
CanUpdate = Annotated[AuthContext, Depends(require(PatientPermissions.UPDATE))]
CanMerge = Annotated[AuthContext, Depends(require(PatientPermissions.MERGE))]
CanReadIds = Annotated[AuthContext, Depends(require(PatientPermissions.READ_IDENTIFIERS))]
CanManageIds = Annotated[AuthContext, Depends(require(PatientPermissions.MANAGE_IDENTIFIERS))]
CanManageAlerts = Annotated[AuthContext, Depends(require(PatientPermissions.MANAGE_ALERTS))]
CanManageConsent = Annotated[AuthContext, Depends(require(PatientPermissions.MANAGE_CONSENT))]


class RegistrationResponse(PatientDetail):
    """The new patient plus any near-duplicates worth a second look."""

    possible_duplicates: list[DuplicateCandidate] = Field(default_factory=list)


async def _detail(
    session: SessionDep, patient_id: uuid.UUID, *, include_consents: bool = True
) -> PatientDetail:
    patient = await service.get_patient(session, patient_id)
    alerts = await service.get_patient_alerts(session, patient.id)
    consents = await service.get_consents(session, patient.id) if include_consents else []
    return PatientDetail(
        **PatientRead.model_validate(patient).model_dump(),
        alternate_phone=patient.alternate_phone,
        email=patient.email,
        marital_status=patient.marital_status,
        occupation=patient.occupation,
        nationality=patient.nationality,
        guardian_name=patient.guardian_name,
        guardian_relation=patient.guardian_relation,
        guardian_phone=patient.guardian_phone,
        address_line1=patient.address_line1,
        address_line2=patient.address_line2,
        district=patient.district,
        state=patient.state,
        pincode=patient.pincode,
        country=patient.country,
        abha_id=patient.abha_id,
        abha_address=patient.abha_address,
        preferred_language=patient.preferred_language,
        deceased_at=patient.deceased_at,
        alerts=[PatientAlertRead.model_validate(alert) for alert in alerts],
        consents=[PatientConsentRead.model_validate(consent) for consent in consents],
    )


async def _load(session: SessionDep, tenant_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
    patient = await service.get_patient(session, patient_id)
    if patient.hospital_id != tenant_id:
        raise NotFoundError("Patient not found.", code="patient_not_found")
    return patient


# ---------------------------------------------------------------------------
# Registration & duplicate detection
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a patient (4 required fields)",
)
async def register_patient(
    payload: PatientRegister,
    session: SessionDep,
    request: Request,
    context: CanCreate,
    tenant_id: TenantId,
) -> RegistrationResponse:
    patient, soft_matches = await service.register_patient(session, payload, hospital_id=tenant_id)
    await identity_service.record_audit(
        session,
        action="patient.register",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id,
        changes={"uhid": patient.uhid, "confirmed_not_duplicate": payload.confirm_not_duplicate},
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    detail = await _detail(session, patient.id)
    return RegistrationResponse(
        **detail.model_dump(),
        possible_duplicates=[
            DuplicateCandidate(
                patient=PatientRead.model_validate(match.patient),
                score=round(match.score, 3),
                reason=match.reason,
                is_exact=match.is_exact,
            )
            for match in soft_matches
        ],
    )


@router.get(
    "/check-duplicates",
    response_model=list[DuplicateCandidate],
    summary="Suggest existing records while reception types",
)
async def check_duplicates(
    session: SessionDep,
    context: CanCreate,
    tenant_id: TenantId,
    full_name: Annotated[str, Query(min_length=1, max_length=200)],
    phone: Annotated[str, Query(min_length=3, max_length=20)],
) -> list[DuplicateCandidate]:
    matches = await service.find_duplicates(
        session, hospital_id=tenant_id, full_name=full_name, phone=phone
    )
    return [
        DuplicateCandidate(
            patient=PatientRead.model_validate(match.patient),
            score=round(match.score, 3),
            reason=match.reason,
            is_exact=match.is_exact,
        )
        for match in matches
    ]


# ---------------------------------------------------------------------------
# Search & list
# ---------------------------------------------------------------------------
@router.get("/search", response_model=Page[PatientRead], summary="Universal patient search")
async def search_patients(
    session: SessionDep,
    request: Request,
    context: CanRead,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str, Query(min_length=1, max_length=100, description="UHID, mobile or name")],
) -> Page[PatientRead]:
    patients, total = await service.search_patients(session, q, params, hospital_id=tenant_id)
    await identity_service.record_audit(
        session,
        action="patient.search",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        changes={"query": q, "results": total},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return Page.build(
        [PatientRead.model_validate(patient) for patient in patients],
        total=total,
        params=params,
    )


@router.get("", response_model=Page[PatientRead], summary="List patients")
async def list_patients(
    session: SessionDep,
    context: CanRead,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    include_inactive: bool = False,
) -> Page[PatientRead]:
    patients, total = await service.list_patients(
        session, params, hospital_id=tenant_id, include_inactive=include_inactive
    )
    return Page.build(
        [PatientRead.model_validate(patient) for patient in patients],
        total=total,
        params=params,
    )


@router.get(
    "/by-uhid/{uhid}",
    response_model=PatientDetail,
    summary="Resolve one UHID — the scanned-card lookup",
)
async def get_patient_by_uhid(
    uhid: Annotated[str, Path(min_length=3, max_length=64)],
    session: SessionDep,
    request: Request,
    context: CanRead,
    tenant_id: TenantId,
) -> PatientDetail:
    """Open the record a card names (CLAUDE.md §7b: QR-based patient lookup).

    Declared **above** `/{patient_id}`: FastAPI matches in order, and a literal
    segment registered after a `uuid.UUID` parameter would never be reached —
    `by-uhid` would be parsed as a malformed UUID and 422 instead.

    Exact match, not the search box's substring one, and it follows a merge so
    a card printed before two records were joined still opens the surviving
    one, allergies included.

    Audited separately from `patient.view` because a scan is a distinct access
    pattern: an auditor asking "who looked this patient up, and how" gets a
    different answer for a card presented at a counter than for a click on a
    worklist. The scanned value is recorded even when nothing is found — an
    unrecognised card is exactly the event somebody investigates later.
    """
    patient = await service.get_patient_by_uhid(session, uhid, hospital_id=tenant_id)

    await identity_service.record_audit(
        session,
        action="patient.lookup.uhid",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id if patient is not None else None,
        changes={"uhid": uhid, "found": patient is not None},
        ip_address=await get_client_ip(request),
    )
    await session.commit()

    if patient is None:
        # 404 rather than 403 for another hospital's card, on the same rule the
        # rest of the API follows: confirming a record exists elsewhere is
        # itself a cross-tenant leak. An unknown UHID and another tenant's UHID
        # are indistinguishable from here, and that is the point.
        raise NotFoundError("No patient with that UHID.", code="patient_not_found")

    return await _detail(session, patient.id)


@router.get("/{patient_id}", response_model=PatientDetail, summary="Get a patient")
async def get_patient(
    patient_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanRead,
    tenant_id: TenantId,
) -> PatientDetail:
    patient = await _load(session, tenant_id, patient_id)
    await identity_service.record_audit(
        session,
        action="patient.view",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id,
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _detail(session, patient.id)


@router.patch("/{patient_id}", response_model=PatientDetail, summary="Update a patient")
async def update_patient(
    patient_id: uuid.UUID,
    payload: PatientUpdate,
    session: SessionDep,
    request: Request,
    context: CanUpdate,
    tenant_id: TenantId,
) -> PatientDetail:
    patient = await _load(session, tenant_id, patient_id)
    await service.update_patient(session, patient, payload)
    await identity_service.record_audit(
        session,
        action="patient.update",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id,
        changes=payload.model_dump(exclude_unset=True, mode="json"),
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _detail(session, patient.id)


# ---------------------------------------------------------------------------
# Safety alerts
# ---------------------------------------------------------------------------
@router.get(
    "/{patient_id}/alerts",
    response_model=list[PatientAlertRead],
    summary="Safety banner alerts",
)
async def list_alerts(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CanRead,
    tenant_id: TenantId,
    active_only: bool = True,
) -> list[PatientAlertRead]:
    patient = await _load(session, tenant_id, patient_id)
    alerts = await service.get_patient_alerts(session, patient.id, active_only=active_only)
    return [PatientAlertRead.model_validate(alert) for alert in alerts]


@router.post(
    "/{patient_id}/alerts",
    response_model=PatientAlertRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a safety alert",
)
async def create_alert(
    patient_id: uuid.UUID,
    payload: AlertCreate,
    session: SessionDep,
    request: Request,
    context: CanManageAlerts,
    tenant_id: TenantId,
) -> PatientAlertRead:
    patient = await _load(session, tenant_id, patient_id)
    alert = await service.record_alert(session, patient, payload, recorded_by_id=context.user.id)
    await identity_service.record_audit(
        session,
        action="patient.alert_added",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id,
        changes={"type": payload.alert_type.value, "label": payload.label},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return PatientAlertRead.model_validate(alert)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
@router.post(
    "/{patient_id}/consents",
    response_model=PatientConsentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Capture consent",
)
async def create_consent(
    patient_id: uuid.UUID,
    payload: ConsentCreate,
    session: SessionDep,
    request: Request,
    context: CanManageConsent,
    tenant_id: TenantId,
) -> PatientConsentRead:
    patient = await _load(session, tenant_id, patient_id)
    consent = await service.record_consent(
        session, patient, payload, recorded_by_id=context.user.id
    )
    await identity_service.record_audit(
        session,
        action="patient.consent_recorded",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id,
        changes={"type": payload.consent_type.value, "granted": payload.granted},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return PatientConsentRead.model_validate(consent)


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------
@router.get(
    "/{patient_id}/identifiers",
    response_model=list[PatientIdentifierRead],
    summary="Government and scheme identifiers",
)
async def list_identifiers(
    patient_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanReadIds,
    tenant_id: TenantId,
) -> list[PatientIdentifierRead]:
    patient = await _load(session, tenant_id, patient_id)
    identifiers = await service.get_identifiers(session, patient.id)
    # Narrower right than reading demographics, so audited separately.
    await identity_service.record_audit(
        session,
        action="patient.view_identifiers",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id,
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return [PatientIdentifierRead.model_validate(item) for item in identifiers]


@router.post(
    "/{patient_id}/identifiers",
    response_model=PatientIdentifierRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an identifier",
)
async def add_identifier(
    patient_id: uuid.UUID,
    payload: IdentifierCreate,
    session: SessionDep,
    request: Request,
    context: CanManageIds,
    tenant_id: TenantId,
) -> PatientIdentifierRead:
    patient = await _load(session, tenant_id, patient_id)
    identifier = await service.add_identifier(session, patient, payload)
    await identity_service.record_audit(
        session,
        action="patient.identifier_added",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=patient.id,
        changes={"type": identifier.identifier_type},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return PatientIdentifierRead.model_validate(identifier)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
@router.post(
    "/{patient_id}/merge",
    response_model=PatientDetail,
    summary="Merge a duplicate into this record",
)
async def merge_patients(
    patient_id: uuid.UUID,
    payload: PatientMergeRequest,
    session: SessionDep,
    request: Request,
    context: CanMerge,
    tenant_id: TenantId,
) -> PatientDetail:
    survivor = await _load(session, tenant_id, patient_id)
    duplicate = await _load(session, tenant_id, payload.duplicate_id)

    await service.merge_patients(
        session, survivor=survivor, duplicate=duplicate, reason=payload.reason
    )
    await identity_service.record_audit(
        session,
        action="patient.merge",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="patient",
        resource_id=survivor.id,
        changes={
            "merged_uhid": duplicate.uhid,
            "into_uhid": survivor.uhid,
            "reason": payload.reason,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _detail(session, survivor.id)
