"""Tenancy business logic — the module's public interface.

Other modules call these functions; nobody selects from `hospitals` directly.
"""

from __future__ import annotations

import logging
import uuid
import zoneinfo
from collections.abc import Collection
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import system_context
from app.core.exceptions import ConflictError, NotFoundError
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.tenancy.models import Department, Hospital
from app.modules.tenancy.schemas import (
    DepartmentCreate,
    DepartmentUpdate,
    HospitalCreate,
    HospitalUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

logger = logging.getLogger(__name__)

__all__ = [
    "count_hospitals",
    "create_department",
    "create_hospital",
    "get_department",
    "get_hospital",
    "get_hospital_by_code",
    "list_departments",
    "list_hospitals",
    "local_today",
    "set_hospital_active",
    "update_department",
    "update_hospital",
]


async def local_today(session: AsyncSession | SAAsyncSession, hospital_id: uuid.UUID) -> date:
    """The hospital's current date, not the server's.

    This lives in `tenancy` because it is a fact about a hospital — the
    timezone is a column on `hospitals`, and `tenancy` is the module that owns
    it. Every module that needs a "today" for a tenant calls this one.

    That centralisation is not tidiness. A hospital in `Asia/Kolkata` is five
    and a half hours ahead of UTC, so from 18:30 UTC the server's date is
    already tomorrow. Any module that reaches for `utc_now().date()` instead
    files an entire evening's work under the wrong day — and it does so
    *silently*, producing numbers that look plausible. `reporting` was careful
    about this from the start; `scheduling` was not, and every patient who
    checked in after half past six in the evening was filed under yesterday's
    queue, where the live board could not see them.
    """
    hospital = await get_hospital(session, hospital_id)
    try:
        zone = zoneinfo.ZoneInfo(hospital.timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        # A bad timezone string must not take a screen down; it degrades to UTC
        # and says so, loudly enough to be found and fixed.
        logger.warning(
            "hospital %s has an unknown timezone %r; falling back to UTC",
            hospital_id,
            hospital.timezone,
        )
        zone = zoneinfo.ZoneInfo("UTC")
    return datetime.now(zone).date()


async def create_hospital(
    session: AsyncSession | SAAsyncSession,
    payload: HospitalCreate,
) -> Hospital:
    """Register a new tenant. Platform-admin only — enforced at the router."""
    # Creating a tenant is by definition outside any tenant, so RLS has to be
    # lifted for the uniqueness probe and the insert.
    async with system_context(session):
        existing = await session.execute(select(Hospital).where(Hospital.code == payload.code))
        if existing.scalars().first() is not None:
            raise ConflictError(
                f"A hospital with code '{payload.code}' already exists.",
                code="hospital_code_taken",
            )

        hospital = Hospital(**payload.model_dump(), is_active=True, activated_at=utc_now())
        session.add(hospital)
        await session.flush()
        await session.refresh(hospital)
    return hospital


async def get_hospital(
    session: AsyncSession | SAAsyncSession,
    hospital_id: uuid.UUID,
    *,
    include_inactive: bool = True,
) -> Hospital:
    statement = select(Hospital).where(
        Hospital.id == hospital_id,
        col(Hospital.deleted_at).is_(None),
    )
    if not include_inactive:
        statement = statement.where(col(Hospital.is_active).is_(True))

    hospital = (await session.execute(statement)).scalars().first()
    if hospital is None:
        raise NotFoundError("Hospital not found.", code="hospital_not_found")
    return hospital


async def get_hospital_by_code(
    session: AsyncSession | SAAsyncSession, code: str
) -> Hospital | None:
    """Look a tenant up by its printed code. Used during onboarding."""
    async with system_context(session):
        result = await session.execute(
            select(Hospital).where(
                Hospital.code == code.strip().upper(),
                col(Hospital.deleted_at).is_(None),
            )
        )
        return result.scalars().first()


async def list_hospitals(
    session: AsyncSession | SAAsyncSession,
    params: PageParams,
    *,
    search: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[Hospital], int]:
    """Paginated tenant list. Platform-admin only."""
    filters: list[ColumnElement[bool]] = [col(Hospital.deleted_at).is_(None)]
    if not include_inactive:
        filters.append(col(Hospital.is_active).is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(col(Hospital.name).ilike(pattern) | col(Hospital.code).ilike(pattern))

    async with system_context(session):
        total = (
            await session.execute(select(func.count()).select_from(Hospital).where(*filters))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Hospital)
                    .where(*filters)
                    .order_by(Hospital.name)
                    .limit(params.limit)
                    .offset(params.offset)
                )
            )
            .scalars()
            .all()
        )
    return list(rows), total


async def count_hospitals(session: AsyncSession | SAAsyncSession) -> int:
    async with system_context(session):
        return (
            await session.execute(
                select(func.count()).select_from(Hospital).where(Hospital.deleted_at.is_(None))  # type: ignore[union-attr]
            )
        ).scalar_one()


async def update_hospital(
    session: AsyncSession | SAAsyncSession,
    hospital_id: uuid.UUID,
    payload: HospitalUpdate,
) -> Hospital:
    hospital = await get_hospital(session, hospital_id)
    # `exclude_unset` so an omitted field is left alone rather than nulled —
    # a PATCH must not silently wipe an address the caller never mentioned.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(hospital, field, value)
    hospital.updated_at = utc_now()
    session.add(hospital)
    await session.flush()
    await session.refresh(hospital)
    return hospital


async def set_hospital_active(
    session: AsyncSession | SAAsyncSession,
    hospital_id: uuid.UUID,
    *,
    active: bool,
) -> Hospital:
    """Suspend or reactivate a tenant.

    Never a delete: clinical and financial records must survive a tenant
    leaving the platform (CLAUDE.md §5).
    """
    hospital = await get_hospital(session, hospital_id)
    hospital.is_active = active
    if active and hospital.activated_at is None:
        hospital.activated_at = utc_now()
    hospital.updated_at = utc_now()
    session.add(hospital)
    await session.flush()
    await session.refresh(hospital)
    return hospital


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------
async def create_department(
    session: AsyncSession | SAAsyncSession,
    payload: DepartmentCreate,
    *,
    hospital_id: uuid.UUID,
) -> Department:
    existing = (
        (
            await session.execute(
                select(Department).where(
                    col(Department.hospital_id) == hospital_id,
                    col(Department.code) == payload.code,
                    col(Department.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise ConflictError(
            f"A department with code '{payload.code}' already exists.",
            code="department_code_taken",
        )

    department = Department(hospital_id=hospital_id, **payload.model_dump())
    session.add(department)
    await session.flush()
    await session.refresh(department)
    return department


async def get_department(
    session: AsyncSession | SAAsyncSession,
    department_id: uuid.UUID,
    *,
    hospital_id: uuid.UUID,
) -> Department:
    department = (
        (
            await session.execute(
                select(Department).where(
                    col(Department.id) == department_id,
                    col(Department.hospital_id) == hospital_id,
                    col(Department.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if department is None:
        raise NotFoundError("Department not found.", code="department_not_found")
    return department


async def get_departments_by_ids(
    session: AsyncSession | SAAsyncSession,
    department_ids: Collection[uuid.UUID],
    *,
    hospital_id: uuid.UUID,
) -> list[Department]:
    """Bulk lookup for screens that label many rows — one query, not one each."""
    if not department_ids:
        return []
    rows = (
        (
            await session.execute(
                select(Department).where(
                    col(Department.id).in_(list(department_ids)),
                    col(Department.hospital_id) == hospital_id,
                    col(Department.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def list_departments(
    session: AsyncSession | SAAsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    include_inactive: bool = False,
) -> tuple[list[Department], int]:
    filters: list[ColumnElement[bool]] = [
        col(Department.hospital_id) == hospital_id,
        col(Department.deleted_at).is_(None),
    ]
    if not include_inactive:
        filters.append(col(Department.is_active).is_(True))

    total = (
        await session.execute(select(func.count()).select_from(Department).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Department)
                .where(*filters)
                .order_by(col(Department.name))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def update_department(
    session: AsyncSession | SAAsyncSession,
    department: Department,
    payload: DepartmentUpdate,
) -> Department:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(department, field, value)
    department.updated_at = utc_now()
    session.add(department)
    await session.flush()
    await session.refresh(department)
    return department
