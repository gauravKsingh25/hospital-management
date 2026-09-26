"""Base model mixins shared by every table in the system.

CLAUDE.md §5 (DB integration rules) requires that every table carries:
    id (UUID) · hospital_id (except platform tables) · created_at · updated_at
    · deleted_at (soft delete, for records that must never be hard-deleted)

Rather than repeat those columns in ten modules, they are composed from the
mixins below. Modules import the ready-made bases at the bottom of this file.

Implementation note — why `sa_type` + `sa_column_kwargs` instead of `sa_column`:
a `sa_column=Column(...)` instance can only ever be attached to ONE table, so a
mixin using it explodes the moment a second model inherits it. Passing the type
and the column kwargs separately lets SQLModel construct a fresh Column per
subclass, which is what makes these mixins reusable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlmodel import Field, SQLModel

from app.core.ids import new_id

__all__ = [
    "AuditedTenantModel",
    "IdentifiedModel",
    "SoftDeleteMixin",
    "TenantMixin",
    "TenantModel",
    "TimestampMixin",
    "UUIDMixin",
    "utc_now",
]


# ---------------------------------------------------------------------------
# Constraint naming convention.
#
# Postgres auto-names unnamed constraints, and those names differ between the
# DB and the model metadata — which makes Alembic autogenerate produce noisy,
# sometimes un-revertable diffs. Fixing a convention up front means every index,
# FK, unique and check constraint has a deterministic name in both places.
# This MUST run before any table class is defined, hence its position here.
# ---------------------------------------------------------------------------
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """Timezone-aware UTC now. Never use naive datetimes anywhere."""
    return datetime.now(UTC)


class UUIDMixin(SQLModel):
    """UUIDv7 primary key (see `core.ids` for the v7-over-v4 rationale)."""

    id: uuid.UUID = Field(
        default_factory=new_id,
        primary_key=True,
        nullable=False,
        description="Internal record identifier. Never displayed to staff.",
    )


class TimestampMixin(SQLModel):
    """Row lifecycle timestamps, stored as `timestamptz`.

    `created_at` also carries a server default so rows inserted outside the ORM
    (migrations, seed scripts, bulk COPY) still get a value.

    `updated_at` is maintained by SQLAlchemy on ORM flush. That is sufficient
    because CLAUDE.md §11 routes all writes through the service layer; if we
    ever need it to survive raw SQL updates too, the answer is a DB trigger,
    not application code.
    """

    # `sa_type` is annotated as `type[Any]` in SQLModel's stubs but accepts a
    # configured type instance at runtime — which is the only way to get
    # `timestamptz` rather than a naive `timestamp`. Hence the ignores below.
    created_at: datetime = Field(
        default_factory=utc_now,
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: datetime = Field(
        default_factory=utc_now,
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
    )


class SoftDeleteMixin(SQLModel):
    """Marks a table as never hard-deleted.

    Applied to every clinical and financial record (CLAUDE.md §5). A row is
    retired by stamping `deleted_at`; queries must filter it out. Indexed
    because that filter appears in essentially every read.
    """

    deleted_at: datetime | None = Field(
        default=None,
        nullable=True,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        description="Set to retire the record. NULL means live.",
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def mark_deleted(self, *, at: datetime | None = None) -> None:
        """Stamp the row as deleted. Callers must still commit the session."""
        self.deleted_at = at or utc_now()


class TenantMixin(SQLModel):
    """Row-level multi-tenancy anchor (CLAUDE.md §3).

    Every tenant-scoped table carries `hospital_id`. Platform-level tables
    (hospitals themselves, global role definitions, platform admins) inherit
    `IdentifiedModel` instead and deliberately omit this.

    The FK target `hospitals.id` is created by the `tenancy` module in Phase 1.
    `RESTRICT` is intentional: deleting a hospital must never cascade away
    patient records — a tenant is retired by soft-delete, not by DROP.
    """

    hospital_id: uuid.UUID = Field(
        foreign_key="hospitals.id",
        ondelete="RESTRICT",
        nullable=False,
        index=True,
        description="Owning tenant. Injected from the request's tenant context.",
    )


# ---------------------------------------------------------------------------
# Ready-made bases. Modules should subclass one of these and add `table=True`.
#
#   class Hospital(IdentifiedModel, table=True):        # platform-level
#   class Patient(AuditedTenantModel, table=True):      # tenant + soft delete
#
# ---------------------------------------------------------------------------
class IdentifiedModel(UUIDMixin, TimestampMixin):
    """Platform-level record: id + timestamps, no tenant column."""


class TenantModel(UUIDMixin, TimestampMixin, TenantMixin):
    """Tenant-scoped record that may be hard-deleted (lookups, joins, caches)."""


class AuditedTenantModel(UUIDMixin, TimestampMixin, TenantMixin, SoftDeleteMixin):
    """Tenant-scoped record that must never be hard-deleted.

    The default base for clinical and financial tables.
    """
