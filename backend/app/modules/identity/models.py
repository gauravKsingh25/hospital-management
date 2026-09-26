"""Identity tables: users, data-driven RBAC, refresh tokens, audit log.

Two deliberate departures from the standard mixins, both because identity sits
partly *above* tenancy:

* `User` and `Role` carry a **nullable** `hospital_id` rather than inheriting
  `TenantMixin`. NULL means platform-level — the `PLATFORM_ADMIN` user who
  creates hospitals, and the seeded system roles every tenant shares. Every
  other table in the system uses the non-null mixin.
* `AuditLog` has no `updated_at` and no soft delete. It is append-only
  (CLAUDE.md §8/§12), enforced by a database trigger rather than convention.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.core.models import IdentifiedModel, SoftDeleteMixin, UUIDMixin, utc_now

__all__ = [
    "AuditLog",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
]


class User(IdentifiedModel, SoftDeleteMixin, table=True):
    """A staff account.

    Email is unique across the whole deployment, not per tenant. One person,
    one login — simpler to reason about, and it means a login form never has
    to ask "which hospital?". The cost is that a clinician working at two
    hospitals in a future multi-tenant deployment needs a user↔hospital
    membership table rather than a second account; that is a additive change,
    which is why this is safe to defer.
    """

    __tablename__ = "users"
    __table_args__ = (
        # Uniqueness applies only to live rows, so a soft-deleted account does
        # not permanently burn its email address.
        Index(
            "uq_users_email_live",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_users_hospital_id_is_active", "hospital_id", "is_active"),
    )

    # NULL only for PLATFORM_ADMIN, the one cross-tenant role (CLAUDE.md §8).
    hospital_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="hospitals.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )

    email: str = Field(max_length=255, description="Login identifier. Stored lowercased.")
    full_name: str = Field(min_length=1, max_length=200, index=True)
    phone: str | None = Field(default=None, max_length=20, index=True)

    # Argon2id. Never leaves the service layer — no schema exposes it.
    password_hash: str = Field(max_length=255)
    # Forces a change on next login: set for admin-created and reset accounts.
    must_change_password: bool = Field(default=True)
    password_changed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    is_active: bool = Field(default=True, index=True)

    # --- Brute-force resistance --------------------------------------------
    failed_login_attempts: int = Field(default=0)
    locked_until: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    last_login_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # --- 2FA (CLAUDE.md §12) ------------------------------------------------
    # Columns reserved now so enabling TOTP for admin and doctor roles later is
    # a feature, not a migration of a live user table. The enrolment and
    # challenge flow is deliberately NOT part of this phase.
    two_factor_enabled: bool = Field(default=False)
    totp_secret: str | None = Field(default=None, max_length=64)


class Role(IdentifiedModel, SoftDeleteMixin, table=True):
    """A named bundle of permissions.

    Data-driven by design (CLAUDE.md §8): adding a role is an INSERT, never a
    deploy. `hospital_id IS NULL` marks the seeded system roles that every
    tenant shares; a tenant may also define its own.
    """

    __tablename__ = "roles"
    __table_args__ = (
        # Two partial indexes rather than one composite unique: Postgres treats
        # NULLs as distinct, so a plain UNIQUE(hospital_id, code) would happily
        # allow two system roles both called DOCTOR.
        Index(
            "uq_roles_code_system",
            "code",
            unique=True,
            postgresql_where=text("hospital_id IS NULL AND deleted_at IS NULL"),
        ),
        Index(
            "uq_roles_code_tenant",
            "hospital_id",
            "code",
            unique=True,
            postgresql_where=text("hospital_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    hospital_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="hospitals.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
        description="NULL for a system role shared by every tenant.",
    )

    code: str = Field(max_length=64, description="Stable machine name, e.g. 'DOCTOR'.")
    name: str = Field(max_length=128, description="Display name shown to staff.")
    description: str | None = Field(default=None, max_length=500)

    # System roles are seeded by migration and may not be edited or deleted by
    # a hospital admin — only their assignment to users is a tenant decision.
    is_system: bool = Field(default=False, index=True)


class Permission(IdentifiedModel, table=True):
    """A single gated action, e.g. `patient:create`.

    Global catalogue — permissions describe what the software can do, which is
    identical for every tenant, so this table has no `hospital_id`. Each module
    adds its own rows in its own migration as it is built.
    """

    __tablename__ = "permissions"

    code: str = Field(
        max_length=100,
        unique=True,
        index=True,
        description="`resource:action`, e.g. 'user:create'.",
    )
    module: str = Field(max_length=50, index=True, description="Owning module.")
    description: str = Field(max_length=255)


class RolePermission(SQLModel, table=True):
    """Role → permission grant."""

    __tablename__ = "role_permissions"

    role_id: uuid.UUID = Field(
        foreign_key="roles.id", ondelete="CASCADE", primary_key=True, index=True
    )
    permission_id: uuid.UUID = Field(
        foreign_key="permissions.id", ondelete="CASCADE", primary_key=True, index=True
    )
    created_at: datetime = Field(
        default_factory=utc_now,
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        sa_column_kwargs={"server_default": func.now()},
    )


class UserRole(SQLModel, table=True):
    """User → role assignment. A user may hold several (CLAUDE.md §8)."""

    __tablename__ = "user_roles"

    user_id: uuid.UUID = Field(
        foreign_key="users.id", ondelete="CASCADE", primary_key=True, index=True
    )
    role_id: uuid.UUID = Field(
        foreign_key="roles.id", ondelete="RESTRICT", primary_key=True, index=True
    )
    # Who granted it — an access-review question auditors always ask.
    granted_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    created_at: datetime = Field(
        default_factory=utc_now,
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        sa_column_kwargs={"server_default": func.now()},
    )


class RefreshToken(IdentifiedModel, table=True):
    """One issued refresh token, rotated on every use.

    The raw token is never stored — only its SHA-256. `replaced_by_id` chains
    a rotation family together so that presenting an already-rotated token
    (the signature of a stolen one) can revoke the entire chain.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),)

    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    hospital_id: uuid.UUID | None = Field(
        default=None, foreign_key="hospitals.id", ondelete="RESTRICT", nullable=True, index=True
    )

    token_hash: str = Field(max_length=64, unique=True, index=True)
    jti: uuid.UUID = Field(unique=True, index=True)

    expires_at: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    revoked_reason: str | None = Field(default=None, max_length=64)
    replaced_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="refresh_tokens.id", ondelete="SET NULL", nullable=True
    )

    # Session provenance — shown on a "your active sessions" screen later.
    ip_address: str | None = Field(default=None, max_length=45)
    user_agent: str | None = Field(default=None, max_length=255)


class AuditLog(UUIDMixin, table=True):
    """Append-only record of every sensitive action (CLAUDE.md §8, §12).

    Actor identity is snapshotted (`actor_email`) rather than only referenced:
    an audit trail must still read correctly after the account is renamed or
    retired. UPDATE and DELETE are refused by a database trigger, so this is
    append-only in fact and not merely by intention.

    `actor_user_id` is `ON DELETE RESTRICT`, which is the only referential
    action consistent with that trigger. `SET NULL` — what this column carried
    until it was corrected — would have the database issue an UPDATE against
    the audit row, and the append-only trigger refuses UPDATE. The declared
    behaviour was therefore unreachable: deleting a staff account would not have
    anonymised the trail, it would have failed with a trigger error at the one
    moment nobody wants a surprise, during offboarding.

    RESTRICT states the real rule plainly: an account that has done anything
    cannot be erased, because an audit trail whose actor can be deleted is not
    an audit trail. Offboarding sets `users.deleted_at` instead (`User` carries
    `SoftDeleteMixin`), and the snapshotted `actor_email` keeps the history
    readable afterwards. It also matches the sibling `hospital_id` FK, which was
    RESTRICT from the start.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_hospital_id_created_at", "hospital_id", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    hospital_id: uuid.UUID | None = Field(
        default=None, foreign_key="hospitals.id", ondelete="RESTRICT", nullable=True, index=True
    )

    actor_user_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="users.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
        description="NULL for actions taken by a background worker, never by deletion.",
    )
    actor_email: str | None = Field(default=None, max_length=255)

    action: str = Field(max_length=100, index=True, description="e.g. 'user.login'.")
    resource_type: str | None = Field(default=None, max_length=64)
    resource_id: uuid.UUID | None = Field(default=None)

    # Before/after snapshots. JSONB so audits stay queryable without a schema
    # per audited table.
    changes: dict[str, Any] | None = Field(default=None, sa_type=JSONB)

    succeeded: bool = Field(default=True, index=True)
    ip_address: str | None = Field(default=None, max_length=45)
    user_agent: str | None = Field(default=None, max_length=255)

    created_at: datetime = Field(
        default_factory=utc_now,
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        sa_column_kwargs={"server_default": func.now()},
    )
