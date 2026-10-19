"""Tenancy HTTP routes. Thin — all logic lives in `service.py`.

Every route takes its `AuthContext` from `require(...)`, so the permission
check and the authenticated caller arrive together and a route cannot be
written that reads one without enforcing the other.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import service as identity_service
from app.modules.identity.rbac import Permissions
from app.modules.identity.service import AuthContext
from app.modules.scheduling.rbac import SchedulingPermissions
from app.modules.tenancy import service
from app.modules.tenancy.schemas import (
    DepartmentCreate,
    DepartmentRead,
    DepartmentUpdate,
    HospitalCreate,
    HospitalRead,
    HospitalUpdate,
    LogoUploaded,
)

router = APIRouter(prefix="/hospitals", tags=["tenancy"])

CanCreate = Annotated[AuthContext, Depends(require(Permissions.HOSPITAL_CREATE))]
CanRead = Annotated[AuthContext, Depends(require(Permissions.HOSPITAL_READ))]
CanUpdate = Annotated[AuthContext, Depends(require(Permissions.HOSPITAL_UPDATE))]
CanDeactivate = Annotated[AuthContext, Depends(require(Permissions.HOSPITAL_DEACTIVATE))]


def _assert_visible(context: AuthContext, hospital_id: uuid.UUID) -> None:
    """A tenant-scoped caller may only ever address its own hospital.

    Reported as 404, not 403 — confirming that another tenant exists is itself
    a cross-tenant leak.
    """
    if not context.is_platform_admin and hospital_id != context.hospital_id:
        raise NotFoundError("Hospital not found.", code="hospital_not_found")


@router.post(
    "",
    response_model=HospitalRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a hospital tenant",
)
async def create_hospital(
    payload: HospitalCreate,
    session: SessionDep,
    request: Request,
    context: CanCreate,
) -> HospitalRead:
    hospital = await service.create_hospital(session, payload)
    await identity_service.record_audit(
        session,
        action="hospital.create",
        actor=context.user,
        hospital_id=hospital.id,
        resource_type="hospital",
        resource_id=hospital.id,
        changes={"code": hospital.code, "name": hospital.name},
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return HospitalRead.model_validate(hospital)


@router.get("", response_model=Page[HospitalRead], summary="List hospitals")
async def list_hospitals(
    session: SessionDep,
    context: CanRead,
    params: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=100)] = None,
    include_inactive: bool = False,
) -> Page[HospitalRead]:
    # A hospital-scoped user sees exactly one row: their own.
    if not context.is_platform_admin:
        if context.hospital_id is None:
            return Page.build([], total=0, params=params)
        hospital = await service.get_hospital(session, context.hospital_id)
        return Page.build([HospitalRead.model_validate(hospital)], total=1, params=params)

    hospitals, total = await service.list_hospitals(
        session, params, search=search, include_inactive=include_inactive
    )
    return Page.build(
        [HospitalRead.model_validate(item) for item in hospitals], total=total, params=params
    )


@router.get("/{hospital_id}", response_model=HospitalRead, summary="Get a hospital")
async def get_hospital(
    hospital_id: uuid.UUID,
    session: SessionDep,
    context: CanRead,
) -> HospitalRead:
    _assert_visible(context, hospital_id)
    hospital = await service.get_hospital(session, hospital_id)
    return HospitalRead.model_validate(hospital)


@router.patch("/{hospital_id}", response_model=HospitalRead, summary="Update a hospital")
async def update_hospital(
    hospital_id: uuid.UUID,
    payload: HospitalUpdate,
    session: SessionDep,
    request: Request,
    context: CanUpdate,
) -> HospitalRead:
    _assert_visible(context, hospital_id)
    hospital = await service.update_hospital(session, hospital_id, payload)
    await identity_service.record_audit(
        session,
        action="hospital.update",
        actor=context.user,
        hospital_id=hospital.id,
        resource_type="hospital",
        resource_id=hospital.id,
        changes=payload.model_dump(exclude_unset=True, mode="json"),
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return HospitalRead.model_validate(hospital)


@router.put(
    "/{hospital_id}/logo",
    response_model=LogoUploaded,
    summary="Upload the hospital's logo (raw PNG or JPEG body)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def upload_logo(
    hospital_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanUpdate,
) -> LogoUploaded:
    """The file itself is the request body, with its own Content-Type — no
    multipart form. A new upload is a new version; issued bills keep theirs."""
    _assert_visible(context, hospital_id)
    declared = int(request.headers.get("content-length") or 0)
    if declared > service.LOGO_MAX_BYTES:
        # Refused before the body is read: nobody waits for 20 MB to arrive
        # only to be told the limit is 500 KB.
        raise ValidationError("The logo must be 500 KB or smaller.", code="logo_invalid")
    data = await request.body()
    version = await service.save_logo(
        session,
        hospital_id,
        data=data,
        content_type=request.headers.get("content-type", ""),
    )
    await identity_service.record_audit(
        session,
        action="hospital.logo.upload",
        actor=context.user,
        hospital_id=hospital_id,
        resource_type="hospital",
        resource_id=hospital_id,
        changes={"version": version, "bytes": len(data)},
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return LogoUploaded(version=version)


@router.get(
    "/{hospital_id}/logo",
    response_class=Response,
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}}}},
    summary="The hospital's logo",
)
async def get_logo(
    hospital_id: uuid.UUID,
    session: SessionDep,
    context: CanRead,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """A versioned logo never changes, so it may be cached for a long time."""
    _assert_visible(context, hospital_id)
    logo = await service.get_logo(session, hospital_id, version=version)
    return Response(
        content=logo.data,
        media_type=logo.content_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"}
        if version is not None
        else {"Cache-Control": "private, no-cache"},
    )


@router.post(
    "/{hospital_id}/deactivate",
    response_model=HospitalRead,
    summary="Suspend a hospital tenant",
)
async def deactivate_hospital(
    hospital_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanDeactivate,
) -> HospitalRead:
    hospital = await service.set_hospital_active(session, hospital_id, active=False)
    await identity_service.record_audit(
        session,
        action="hospital.deactivate",
        actor=context.user,
        hospital_id=hospital.id,
        resource_type="hospital",
        resource_id=hospital.id,
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return HospitalRead.model_validate(hospital)


@router.post(
    "/{hospital_id}/activate",
    response_model=HospitalRead,
    summary="Reactivate a hospital tenant",
)
async def activate_hospital(
    hospital_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanDeactivate,
) -> HospitalRead:
    hospital = await service.set_hospital_active(session, hospital_id, active=True)
    await identity_service.record_audit(
        session,
        action="hospital.activate",
        actor=context.user,
        hospital_id=hospital.id,
        resource_type="hospital",
        resource_id=hospital.id,
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return HospitalRead.model_validate(hospital)


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------
departments_router = APIRouter(prefix="/departments", tags=["tenancy"])

CanReadDepartment = Annotated[AuthContext, Depends(require(SchedulingPermissions.DEPARTMENT_READ))]
CanManageDepartment = Annotated[
    AuthContext, Depends(require(SchedulingPermissions.DEPARTMENT_MANAGE))
]


@departments_router.post(
    "",
    response_model=DepartmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
)
async def create_department(
    payload: DepartmentCreate,
    session: SessionDep,
    request: Request,
    context: CanManageDepartment,
    tenant_id: TenantId,
) -> DepartmentRead:
    department = await service.create_department(session, payload, hospital_id=tenant_id)
    await identity_service.record_audit(
        session,
        action="department.create",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="department",
        resource_id=department.id,
        changes={"code": department.code, "name": department.name},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return DepartmentRead.model_validate(department)


@departments_router.get("", response_model=Page[DepartmentRead], summary="List departments")
async def list_departments(
    session: SessionDep,
    context: CanReadDepartment,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    include_inactive: bool = False,
) -> Page[DepartmentRead]:
    departments, total = await service.list_departments(
        session, params, hospital_id=tenant_id, include_inactive=include_inactive
    )
    return Page.build(
        [DepartmentRead.model_validate(item) for item in departments],
        total=total,
        params=params,
    )


@departments_router.get(
    "/{department_id}", response_model=DepartmentRead, summary="Get a department"
)
async def get_department(
    department_id: uuid.UUID,
    session: SessionDep,
    context: CanReadDepartment,
    tenant_id: TenantId,
) -> DepartmentRead:
    return DepartmentRead.model_validate(
        await service.get_department(session, department_id, hospital_id=tenant_id)
    )


@departments_router.patch(
    "/{department_id}", response_model=DepartmentRead, summary="Update a department"
)
async def update_department(
    department_id: uuid.UUID,
    payload: DepartmentUpdate,
    session: SessionDep,
    context: CanManageDepartment,
    tenant_id: TenantId,
) -> DepartmentRead:
    department = await service.get_department(session, department_id, hospital_id=tenant_id)
    updated = await service.update_department(session, department, payload)
    await session.commit()
    return DepartmentRead.model_validate(updated)
