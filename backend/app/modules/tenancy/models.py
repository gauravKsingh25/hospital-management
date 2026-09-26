"""Tenant records. One row per hospital (CLAUDE.md §3), plus its departments."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, text
from sqlmodel import Field

from app.core.models import AuditedTenantModel, IdentifiedModel, SoftDeleteMixin

__all__ = ["Department", "DepartmentType", "Hospital"]


class Hospital(IdentifiedModel, SoftDeleteMixin, table=True):
    """A tenant.

    Platform-level: no `hospital_id` of its own. Never hard-deleted — every
    clinical and financial record in the system points back here, and a tenant
    is retired by clearing `is_active`, not by DELETE.
    """

    __tablename__ = "hospitals"

    # Short human-typed identifier used in UHIDs, invoice numbers and logins.
    # Immutable once issued: it is baked into printed patient records.
    code: str = Field(
        min_length=2,
        max_length=16,
        unique=True,
        index=True,
        description="Short uppercase tenant code, e.g. 'KMC'. Immutable.",
    )
    name: str = Field(min_length=2, max_length=200, index=True)
    legal_name: str | None = Field(default=None, max_length=200)

    # --- ABDM / ABHA readiness (CLAUDE.md §9) ------------------------------
    # Reserved now so the column exists before the integration does; ABDM
    # registration is a paperwork exercise, not a schema change.
    hfr_facility_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Health Facility Registry ID, assigned on ABDM registration.",
    )

    # --- Contact & statutory ------------------------------------------------
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100, index=True)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = Field(default=None, max_length=10)
    country: str = Field(default="IN", max_length=2)

    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=255)

    gstin: str | None = Field(default=None, max_length=15, description="GST identifier.")

    # IANA name. Stored per tenant because a multi-hospital deployment may
    # eventually span timezones; all timestamps stay UTC in the database and
    # are rendered in this zone.
    timezone: str = Field(default="Asia/Kolkata", max_length=64)

    is_active: bool = Field(
        default=True,
        index=True,
        description="Cleared to suspend a tenant without deleting its records.",
    )
    activated_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )


class DepartmentType(enum.StrEnum):
    OPD = "OPD"
    IPD = "IPD"
    EMERGENCY = "EMERGENCY"
    DIAGNOSTIC = "DIAGNOSTIC"
    PHARMACY = "PHARMACY"
    SUPPORT = "SUPPORT"


class Department(AuditedTenantModel, table=True):
    """A clinical or operational unit within a hospital.

    Lives in `tenancy` rather than `scheduling` because it is facility
    structure, not a scheduling concept: `clinical`, `billing`, `diagnostics`
    and `ipd` all need to say which department something happened in. Putting
    it in the first module that happened to need it would have forced four
    later modules to reach across a boundary for it.
    """

    __tablename__ = "departments"
    __table_args__ = (
        Index(
            "uq_departments_code_live",
            "hospital_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    code: str = Field(max_length=24, index=True, description="Short code, e.g. 'CARD'.")
    name: str = Field(max_length=120, index=True)
    department_type: DepartmentType = Field(default=DepartmentType.OPD, index=True)
    description: str | None = Field(default=None, max_length=500)

    # Where patients are told to go. Printed on the token slip, so it matters
    # more than it looks.
    location: str | None = Field(
        default=None, max_length=120, description="Floor / block / room, shown to patients."
    )
    phone_extension: str | None = Field(default=None, max_length=10)

    is_active: bool = Field(default=True, index=True)

    head_doctor_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="users.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
