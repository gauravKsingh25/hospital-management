"""Reporting HTTP routes. Thin — logic lives in `service.py`.

Every route is a `GET`. This module has nothing to write.

Two things differ from the rest of the API and are worth knowing:

* **No pagination.** CLAUDE.md §11 requires it on list endpoints because those
  grow without bound; these responses do not. A footfall report has one row per
  day in a window the schema caps, one per department and one per doctor — all
  bounded by the hospital's own size. What keeps these endpoints honest is the
  *window* cap, which bounds the scan rather than the response.
* **`/dashboard` returns whatever the caller may see.** It requires only
  `report:operational`, and the sections above that are filled in by permission.
  A single endpoint with a role-shaped response is what §7b's role-based
  dashboards need; three endpoints would push that composition into the
  frontend, where it becomes three round trips and a layout that reflows.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError as PydanticValidationError

from app.core.deps import SessionDep, TenantId, require
from app.core.exceptions import ValidationError
from app.modules.identity.service import AuthContext
from app.modules.reporting import service
from app.modules.reporting.rbac import ReportingPermissions
from app.modules.reporting.schemas import (
    Dashboard,
    DateWindow,
    FollowUpCompliance,
    FootfallReport,
    InpatientReport,
    OccupancyReport,
    QueueSnapshot,
    RevenueReport,
)

router = APIRouter(prefix="/reports", tags=["reporting"])

CanReadOperational = Annotated[AuthContext, Depends(require(ReportingPermissions.OPERATIONAL))]
CanReadClinical = Annotated[AuthContext, Depends(require(ReportingPermissions.CLINICAL))]
CanReadRevenue = Annotated[AuthContext, Depends(require(ReportingPermissions.REVENUE))]

# Default trailing window when the caller does not name one. Thirty days is the
# span a monthly review actually looks at, and it keeps the default request off
# a scan of the whole table.
DEFAULT_TRAILING_DAYS = 30


async def _window(
    session: SessionDep,
    hospital_id: uuid.UUID,
    date_from: date | None,
    date_to: date | None,
) -> DateWindow:
    """Resolve the requested range, defaulting to the trailing month.

    "Today" is the *hospital's* today, not the server's — see
    `service.local_today`. A dashboard that rolls over at 05:30 IST because the
    server is on UTC is a dashboard staff learn to distrust.
    """
    today = await service.local_today(session, hospital_id)
    end = date_to or today
    start = date_from or (end - timedelta(days=DEFAULT_TRAILING_DAYS - 1))
    try:
        return DateWindow(date_from=start, date_to=end)
    except PydanticValidationError as exc:
        # The window is assembled here rather than bound from the request, so
        # its rules fire inside the handler where an uncaught pydantic error
        # would surface as a 500. Re-raised as the app's own exception type so
        # it maps to 422 centrally, like every other invalid input (§11).
        raise ValidationError(
            "; ".join(error["msg"].removeprefix("Value error, ") for error in exc.errors()),
            code="invalid_report_window",
        ) from exc


@router.get("/dashboard", response_model=Dashboard, summary="The role's dashboard")
async def dashboard(
    session: SessionDep,
    context: CanReadOperational,
    tenant_id: TenantId,
    trailing_days: Annotated[int, Query(ge=1, le=90)] = DEFAULT_TRAILING_DAYS,
) -> Dashboard:
    """Everything this user is allowed to see, in one round trip (CLAUDE.md §7b)."""
    return await service.build_dashboard(
        session,
        hospital_id=tenant_id,
        permissions=set(context.permissions),
        trailing_days=trailing_days,
    )


@router.get("/footfall", response_model=FootfallReport, summary="Patient volumes")
async def footfall(
    session: SessionDep,
    context: CanReadOperational,
    tenant_id: TenantId,
    date_from: date | None = None,
    date_to: date | None = None,
    department_id: uuid.UUID | None = None,
    doctor_id: uuid.UUID | None = None,
) -> FootfallReport:
    window = await _window(session, tenant_id, date_from, date_to)
    return await service.footfall(session, window, department_id=department_id, doctor_id=doctor_id)


@router.get("/occupancy", response_model=OccupancyReport, summary="Bed occupancy now")
async def occupancy(
    session: SessionDep, context: CanReadOperational, tenant_id: TenantId
) -> OccupancyReport:
    return await service.occupancy(session)


@router.get("/queue", response_model=QueueSnapshot, summary="Live queue and delay alerts")
async def queue(
    session: SessionDep,
    context: CanReadOperational,
    tenant_id: TenantId,
    queue_date: date | None = None,
    delay_threshold_minutes: Annotated[int | None, Query(ge=5, le=480)] = None,
) -> QueueSnapshot:
    """`running_late` is §7b's doctor delay alert, ready for reception's screen."""
    day = queue_date or await service.local_today(session, tenant_id)
    return await service.queue_snapshot(
        session, queue_date=day, delay_threshold_minutes=delay_threshold_minutes
    )


@router.get("/inpatient", response_model=InpatientReport, summary="Admissions, ALOS and outcomes")
async def inpatient(
    session: SessionDep,
    context: CanReadClinical,
    tenant_id: TenantId,
    date_from: date | None = None,
    date_to: date | None = None,
) -> InpatientReport:
    window = await _window(session, tenant_id, date_from, date_to)
    return await service.inpatient_summary(session, window)


@router.get("/follow-up", response_model=FollowUpCompliance, summary="Follow-up compliance")
async def follow_up(
    session: SessionDep,
    context: CanReadClinical,
    tenant_id: TenantId,
    date_from: date | None = None,
    date_to: date | None = None,
) -> FollowUpCompliance:
    window = await _window(session, tenant_id, date_from, date_to)
    today = await service.local_today(session, tenant_id)
    return await service.follow_up_compliance(session, window, today=today)


@router.get("/revenue", response_model=RevenueReport, summary="Revenue and collections")
async def revenue(
    session: SessionDep,
    context: CanReadRevenue,
    tenant_id: TenantId,
    date_from: date | None = None,
    date_to: date | None = None,
) -> RevenueReport:
    """Earned, collected and outstanding — three numbers, deliberately not summed."""
    window = await _window(session, tenant_id, date_from, date_to)
    return await service.revenue(session, window)
