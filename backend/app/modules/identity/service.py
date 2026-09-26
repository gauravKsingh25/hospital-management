"""Identity business logic: authentication, RBAC resolution, audit.

This is the module's public interface. Other modules import from here — never
from `models.py` or `router.py`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.config import settings
from app.core.database import system_context
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    fingerprint,
    generate_opaque_token,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.modules.identity import cache
from app.modules.identity.models import (
    AuditLog,
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.modules.identity.rbac import Roles
from app.modules.identity.schemas import (
    RoleCreate,
    UserCreate,
    UserUpdate,
)

logger = logging.getLogger(__name__)

# After this many consecutive failures the account is locked for the window
# below. Slows credential stuffing without letting an attacker lock out a real
# clinician indefinitely by guessing at their email.
MAX_FAILED_LOGINS = 5
LOCKOUT_DURATION = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The authenticated caller, resolved fresh from the database per request."""

    user: User
    roles: frozenset[str]
    permissions: frozenset[str]

    @property
    def hospital_id(self) -> uuid.UUID | None:
        return self.user.hospital_id

    @property
    def is_platform_admin(self) -> bool:
        return Roles.PLATFORM_ADMIN in self.roles

    def has_permission(self, *codes: str) -> bool:
        return any(code in self.permissions for code in codes)


# ---------------------------------------------------------------------------
# Audit (CLAUDE.md §8 — append-only, every sensitive action)
# ---------------------------------------------------------------------------
async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor: User | None = None,
    hospital_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    changes: dict[str, Any] | None = None,
    succeeded: bool = True,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """Append an audit row.

    Written in the caller's transaction on purpose: an audit entry that can be
    lost while the action it describes commits is worse than no audit at all.
    That is also why this is a direct call rather than a domain event — the
    event bus is deliberately fire-and-forget.
    """
    entry = AuditLog(
        hospital_id=(
            hospital_id if hospital_id is not None else (actor.hospital_id if actor else None)
        ),
        actor_user_id=actor.id if actor else None,
        # Snapshotted so the trail still reads correctly after a rename.
        actor_email=actor.email if actor else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        changes=changes,
        succeeded=succeeded,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
    )
    async with system_context(session):
        session.add(entry)
        await session.flush()
    return entry


async def list_audit_logs(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID | None,
    action: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[list[AuditLog], int]:
    filters: list[ColumnElement[bool]] = []
    if hospital_id is not None:
        filters.append(col(AuditLog.hospital_id) == hospital_id)
    if action:
        filters.append(col(AuditLog.action) == action)
    if actor_user_id:
        filters.append(col(AuditLog.actor_user_id) == actor_user_id)

    total = (
        await session.execute(select(func.count()).select_from(AuditLog).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(*filters)
                .order_by(col(AuditLog.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


# ---------------------------------------------------------------------------
# RBAC resolution
# ---------------------------------------------------------------------------
async def resolve_permissions(
    session: AsyncSession, user: User
) -> tuple[frozenset[str], frozenset[str]]:
    """Return the user's (role codes, permission codes).

    Resolved server-side rather than baked into the JWT: a revoked role has to
    take effect now, not whenever an access token happens to expire. A
    suspended clinician must not keep prescribing for another 15 minutes.

    The join is cached in Redis for 60s with explicit invalidation on every
    change (see `identity.cache` for why the users row deliberately is not
    cached). A Redis outage degrades to this query — slower, still correct.
    """
    cached = await cache.get_permissions(str(user.id))
    if cached is not None:
        return cached

    async with system_context(session):
        rows = (
            await session.execute(
                select(Role.code, Permission.code)
                .join(UserRole, col(UserRole.role_id) == col(Role.id))
                .outerjoin(RolePermission, col(RolePermission.role_id) == col(Role.id))
                .outerjoin(Permission, col(Permission.id) == col(RolePermission.permission_id))
                .where(
                    UserRole.user_id == user.id,
                    col(Role.deleted_at).is_(None),
                )
            )
        ).all()

    roles = frozenset(row[0] for row in rows)
    permissions = frozenset(row[1] for row in rows if row[1] is not None)
    await cache.set_permissions(str(user.id), roles, permissions)
    return roles, permissions


async def build_auth_context(session: AsyncSession, user: User) -> AuthContext:
    roles, permissions = await resolve_permissions(session, user)
    return AuthContext(user=user, roles=roles, permissions=permissions)


def require_permission(context: AuthContext, *codes: str) -> None:
    """Raise unless the caller holds at least one of `codes`."""
    if not context.has_permission(*codes):
        raise PermissionDeniedError(
            "You do not have permission to perform this action.",
            details={"required": list(codes)},
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    async with system_context(session):
        user = (
            (
                await session.execute(
                    select(User).where(
                        User.id == user_id,
                        col(User.deleted_at).is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
    if user is None:
        raise NotFoundError("User not found.", code="user_not_found")
    return user


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Look up by login identifier.

    Runs with isolation lifted because at login time we do not yet know which
    tenant the caller belongs to — that is precisely what this query answers.
    """
    async with system_context(session):
        return (
            (
                await session.execute(
                    select(User).where(
                        User.email == email.strip().lower(),
                        col(User.deleted_at).is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )


async def create_user(
    session: AsyncSession,
    payload: UserCreate,
    *,
    actor: User | None = None,
    hospital_id: uuid.UUID | None = None,
    must_change_password: bool = True,
) -> User:
    """Create a staff account and assign its roles.

    `hospital_id` is passed in by the caller (the router derives it from the
    actor's tenant) rather than trusted from the request body — otherwise a
    hospital admin could create users inside someone else's hospital.
    """
    validate_password_strength(payload.password)

    if await get_user_by_email(session, payload.email) is not None:
        raise ConflictError(
            "An account with that email address already exists.",
            code="email_taken",
        )

    user = User(
        hospital_id=hospital_id,
        email=payload.email.strip().lower(),
        full_name=payload.full_name.strip(),
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        must_change_password=must_change_password,
        password_changed_at=utc_now(),
        is_active=True,
    )
    async with system_context(session):
        session.add(user)
        await session.flush()

    if payload.role_codes:
        await assign_roles(session, user, payload.role_codes, actor=actor)

    await record_audit(
        session,
        action="user.create",
        actor=actor,
        hospital_id=hospital_id,
        resource_type="user",
        resource_id=user.id,
        changes={"email": user.email, "roles": payload.role_codes},
    )
    async with system_context(session):
        await session.refresh(user)
    return user


async def list_users(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID | None,
    search: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[User], int]:
    filters: list[ColumnElement[bool]] = [col(User.deleted_at).is_(None)]
    if hospital_id is not None:
        filters.append(col(User.hospital_id) == hospital_id)
    if not include_inactive:
        filters.append(col(User.is_active).is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(col(User.full_name).ilike(pattern) | col(User.email).ilike(pattern))

    total = (
        await session.execute(select(func.count()).select_from(User).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(User)
                .where(*filters)
                .order_by(User.full_name)
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def get_roles_for_users(
    session: AsyncSession, user_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Every user's role codes, in one query, keyed by user.

    The staff administration screen exists to answer "who is a doctor here?",
    and `UserRead` could not answer it — the roles are in a join table and the
    schema never carried them. A client would have to ask per row, which on a
    hospital's full staff list is a request per person on a screen that already
    knows every id it needs.

    Deliberately **not** routed through `resolve_permissions`: that reads the
    Redis permission cache, which is keyed per user and would be a round trip
    each, and it also returns permissions this caller does not want. This is a
    plain join, run once.
    """
    if not user_ids:
        return {}

    async with system_context(session):
        rows = (
            await session.execute(
                select(UserRole.user_id, Role.code)
                .join(Role, col(Role.id) == col(UserRole.role_id))
                .where(
                    col(UserRole.user_id).in_(set(user_ids)),
                    col(Role.deleted_at).is_(None),
                )
                .order_by(Role.code)
            )
        ).all()

    grouped: dict[uuid.UUID, list[str]] = {}
    for user_id, code in rows:
        grouped.setdefault(user_id, []).append(code)
    return grouped


async def update_user(
    session: AsyncSession,
    user: User,
    payload: UserUpdate,
    *,
    actor: User | None = None,
) -> User:
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(user, field, value)
    user.updated_at = utc_now()
    async with system_context(session):
        session.add(user)
        await session.flush()

    await record_audit(
        session,
        action="user.update",
        actor=actor,
        hospital_id=user.hospital_id,
        resource_type="user",
        resource_id=user.id,
        changes=changes,
    )
    async with system_context(session):
        await session.refresh(user)
    return user


async def set_user_active(
    session: AsyncSession,
    user: User,
    *,
    active: bool,
    actor: User | None = None,
) -> User:
    """Enable or disable an account.

    Deactivating revokes every refresh token immediately, so the longest a
    disabled account can still act is the remaining access-token lifetime.
    """
    user.is_active = active
    user.updated_at = utc_now()
    async with system_context(session):
        session.add(user)
        if not active:
            await revoke_all_refresh_tokens(session, user.id, reason="user_deactivated")
        await session.flush()

    await cache.invalidate_user(str(user.id))
    await record_audit(
        session,
        action="user.deactivate" if not active else "user.activate",
        actor=actor,
        hospital_id=user.hospital_id,
        resource_type="user",
        resource_id=user.id,
    )
    async with system_context(session):
        await session.refresh(user)
    return user


async def change_password(
    session: AsyncSession,
    user: User,
    *,
    current_password: str,
    new_password: str,
) -> None:
    if not verify_password(current_password, user.password_hash):
        await record_audit(
            session,
            action="user.change_password",
            actor=user,
            resource_type="user",
            resource_id=user.id,
            succeeded=False,
        )
        raise AuthenticationError("Current password is incorrect.", code="invalid_credentials")

    validate_password_strength(new_password)
    if verify_password(new_password, user.password_hash):
        raise ValidationError(
            "The new password must be different from the current one.",
            code="password_reused",
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = utc_now()
    user.updated_at = utc_now()
    async with system_context(session):
        session.add(user)
        # Changing a password ends every other session — the standard
        # expectation after "I think someone has my password".
        await revoke_all_refresh_tokens(session, user.id, reason="password_changed")
        await session.flush()

    await record_audit(
        session,
        action="user.change_password",
        actor=user,
        hospital_id=user.hospital_id,
        resource_type="user",
        resource_id=user.id,
    )


async def reset_password(
    session: AsyncSession,
    user: User,
    *,
    new_password: str,
    actor: User | None = None,
) -> None:
    """Administrative reset. Forces a change at next login."""
    validate_password_strength(new_password)
    user.password_hash = hash_password(new_password)
    user.must_change_password = True
    user.password_changed_at = utc_now()
    user.updated_at = utc_now()
    async with system_context(session):
        session.add(user)
        await revoke_all_refresh_tokens(session, user.id, reason="password_reset")
        await session.flush()

    await record_audit(
        session,
        action="user.reset_password",
        actor=actor,
        hospital_id=user.hospital_id,
        resource_type="user",
        resource_id=user.id,
    )


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
async def get_role_by_code(
    session: AsyncSession, code: str, *, hospital_id: uuid.UUID | None
) -> Role | None:
    """Resolve a role code, preferring a tenant's own role over the system one."""
    async with system_context(session):
        result = await session.execute(
            select(Role)
            .where(
                Role.code == code,
                col(Role.deleted_at).is_(None),
                (col(Role.hospital_id) == hospital_id) | col(Role.hospital_id).is_(None),
            )
            # A hospital-specific role sorts first, so it shadows the system one.
            .order_by(col(Role.hospital_id).is_(None))
        )
        return result.scalars().first()


async def list_roles(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID | None,
) -> tuple[list[Role], int]:
    """System roles plus any belonging to this tenant."""
    scope: ColumnElement[bool] = col(Role.hospital_id).is_(None)
    if hospital_id is not None:
        scope = scope | (col(Role.hospital_id) == hospital_id)
    filters: list[ColumnElement[bool]] = [col(Role.deleted_at).is_(None), scope]

    async with system_context(session):
        total = (
            await session.execute(select(func.count()).select_from(Role).where(*filters))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Role)
                    .where(*filters)
                    .order_by(Role.code)
                    .limit(params.limit)
                    .offset(params.offset)
                )
            )
            .scalars()
            .all()
        )
    return list(rows), total


async def get_role_permissions(session: AsyncSession, role_id: uuid.UUID) -> list[str]:
    async with system_context(session):
        rows = (
            await session.execute(
                select(Permission.code)
                .join(RolePermission, col(RolePermission.permission_id) == col(Permission.id))
                .where(RolePermission.role_id == role_id)
                .order_by(Permission.code)
            )
        ).all()
    return [row[0] for row in rows]


async def create_role(
    session: AsyncSession,
    payload: RoleCreate,
    *,
    hospital_id: uuid.UUID,
    actor: User | None = None,
) -> Role:
    """Define a hospital-specific role.

    System roles are seeded by migration and are not creatable here — that is
    what keeps `PLATFORM_ADMIN` from being redefined by a tenant.
    """
    existing = await get_role_by_code(session, payload.code, hospital_id=hospital_id)
    if existing is not None and existing.hospital_id == hospital_id:
        raise ConflictError(f"Role '{payload.code}' already exists.", code="role_code_taken")

    role = Role(
        hospital_id=hospital_id,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        is_system=False,
    )
    async with system_context(session):
        session.add(role)
        await session.flush()

        if payload.permission_codes:
            permissions = (
                (
                    await session.execute(
                        select(Permission).where(col(Permission.code).in_(payload.permission_codes))
                    )
                )
                .scalars()
                .all()
            )
            found = {perm.code for perm in permissions}
            unknown = set(payload.permission_codes) - found
            if unknown:
                raise ValidationError(
                    "Unknown permission code(s).",
                    code="unknown_permission",
                    details={"codes": sorted(unknown)},
                )
            for permission in permissions:
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        await session.flush()

    await record_audit(
        session,
        action="role.create",
        actor=actor,
        hospital_id=hospital_id,
        resource_type="role",
        resource_id=role.id,
        changes={"code": role.code, "permissions": payload.permission_codes},
    )
    # A role's permissions affect every holder, so retire the whole cache
    # rather than trying to enumerate them.
    await cache.invalidate_all()
    async with system_context(session):
        await session.refresh(role)
    return role


async def assign_roles(
    session: AsyncSession,
    user: User,
    role_codes: list[str],
    *,
    actor: User | None = None,
) -> list[str]:
    """Replace the user's role set with `role_codes`.

    Idempotent and total rather than incremental: an access review reads
    "these are their roles", so the API that sets them should say the same.
    """
    resolved: list[Role] = []
    for code in role_codes:
        role = await get_role_by_code(session, code, hospital_id=user.hospital_id)
        if role is None:
            raise ValidationError(f"Unknown role '{code}'.", code="unknown_role")
        if role.code == Roles.PLATFORM_ADMIN and user.hospital_id is not None:
            # PLATFORM_ADMIN is the only cross-tenant role (CLAUDE.md §8); a
            # tenant-scoped user holding it would be an isolation breach.
            raise ValidationError(
                "PLATFORM_ADMIN cannot be granted to a hospital-scoped user.",
                code="invalid_role_assignment",
            )
        resolved.append(role)

    async with system_context(session):
        current = (
            (await session.execute(select(UserRole).where(UserRole.user_id == user.id)))
            .scalars()
            .all()
        )
        for link in current:
            await session.delete(link)
        await session.flush()

        for role in resolved:
            session.add(
                UserRole(
                    user_id=user.id,
                    role_id=role.id,
                    granted_by_id=actor.id if actor else None,
                )
            )
        await session.flush()

    codes = [role.code for role in resolved]
    # The caller's next request must see the new grants, not the old ones.
    await cache.invalidate_user(str(user.id))
    await record_audit(
        session,
        action="user.assign_roles",
        actor=actor,
        hospital_id=user.hospital_id,
        resource_type="user",
        resource_id=user.id,
        changes={"roles": codes},
    )
    return codes


async def list_permissions(session: AsyncSession) -> list[Permission]:
    async with system_context(session):
        rows = (await session.execute(select(Permission).order_by(Permission.code))).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> User:
    """Verify credentials and return the user.

    Every failure path raises the same error with the same message. Telling a
    caller "no such account" versus "wrong password" hands them a way to
    enumerate which clinicians work here.
    """
    generic = AuthenticationError("Incorrect email or password.", code="invalid_credentials")

    user = await get_user_by_email(session, email)
    if user is None:
        # Still spend the hashing time so a missing account is not detectably
        # faster than a wrong password.
        verify_password(password, _DUMMY_HASH)
        await record_audit(
            session,
            action="auth.login",
            resource_type="user",
            succeeded=False,
            ip_address=ip_address,
            user_agent=user_agent,
            changes={"email": email.strip().lower(), "reason": "unknown_account"},
        )
        raise generic

    now = datetime.now(UTC)
    if user.locked_until is not None and user.locked_until > now:
        await record_audit(
            session,
            action="auth.login",
            actor=user,
            resource_type="user",
            resource_id=user.id,
            succeeded=False,
            ip_address=ip_address,
            user_agent=user_agent,
            changes={"reason": "locked"},
        )
        raise AuthenticationError(
            "This account is temporarily locked after too many failed attempts.",
            code="account_locked",
        )

    if not verify_password(password, user.password_hash):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT_DURATION
        # Authentication happens before any tenant is bound — we do not know
        # which hospital the caller belongs to until this row is read — so the
        # write has to be made with isolation lifted.
        async with system_context(session):
            session.add(user)
            await session.flush()
        await record_audit(
            session,
            action="auth.login",
            actor=user,
            resource_type="user",
            resource_id=user.id,
            succeeded=False,
            ip_address=ip_address,
            user_agent=user_agent,
            changes={"reason": "bad_password", "attempts": user.failed_login_attempts},
        )
        raise generic

    if not user.is_active:
        await record_audit(
            session,
            action="auth.login",
            actor=user,
            resource_type="user",
            resource_id=user.id,
            succeeded=False,
            ip_address=ip_address,
            user_agent=user_agent,
            changes={"reason": "inactive"},
        )
        raise AuthenticationError("This account has been deactivated.", code="account_inactive")

    # Transparent work-factor upgrade when the Argon2 parameters have risen.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    async with system_context(session):
        session.add(user)
        await session.flush()
    return user


# Hash of a random value, used only to equalise timing on unknown accounts.
_DUMMY_HASH = hash_password(generate_opaque_token())


async def issue_token_pair(
    session: AsyncSession,
    user: User,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, int]:
    """Mint an access/refresh pair and persist the refresh token's hash."""
    access_token, _ = create_access_token(user.id, hospital_id=user.hospital_id)

    refresh_jwt, claims = create_refresh_token(user.id, hospital_id=user.hospital_id)
    # The client receives the JWT; we store only a hash of it, so a database
    # read cannot mint a session.
    stored = RefreshToken(
        user_id=user.id,
        hospital_id=user.hospital_id,
        token_hash=fingerprint(refresh_jwt),
        jti=claims.jti,
        expires_at=claims.expires_at,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
    )
    async with system_context(session):
        session.add(stored)
        await session.flush()

    return access_token, refresh_jwt, settings.ACCESS_TOKEN_TTL_MINUTES * 60


async def rotate_refresh_token(
    session: AsyncSession,
    refresh_token: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str, str, int]:
    """Exchange a refresh token for a new pair, invalidating the old one.

    Reuse detection: presenting a token that has already been rotated is the
    signature of a stolen one — the legitimate client would have moved on. We
    cannot tell victim from thief, so every session for that user is revoked
    and both parties must log in again.
    """
    claims = decode_token(refresh_token, expected_type="refresh")
    token_hash = fingerprint(refresh_token)

    async with system_context(session):
        stored = (
            (
                await session.execute(
                    select(RefreshToken).where(RefreshToken.token_hash == token_hash)
                )
            )
            .scalars()
            .first()
        )

    if stored is None:
        raise AuthenticationError("Invalid credentials.", code="invalid_token")

    # Signature and stored row must agree about whose session this is. They
    # only diverge if one of them has been tampered with.
    if stored.user_id != claims.subject or stored.jti != claims.jti:
        raise AuthenticationError("Invalid credentials.", code="invalid_token")

    if stored.revoked_at is not None:
        # Why the revocation happened decides how severely we react. Replaying
        # a token that was retired by *rotation* means two parties hold it —
        # the legitimate client already moved on, so someone copied it. We
        # cannot tell victim from thief, so the whole family goes.
        #
        # A token retired by logout, password change or deactivation is a
        # different story: the user did that on purpose, and an old tab
        # retrying is not evidence of theft. Killing their other sessions there
        # would be a self-inflicted denial of service.
        if stored.revoked_reason == "rotated":
            logger.warning(
                "refresh token reuse detected user_id=%s jti=%s", stored.user_id, stored.jti
            )
            await revoke_all_refresh_tokens(session, stored.user_id, reason="reuse_detected")
            await record_audit(
                session,
                action="auth.refresh",
                hospital_id=stored.hospital_id,
                resource_type="user",
                resource_id=stored.user_id,
                succeeded=False,
                ip_address=ip_address,
                user_agent=user_agent,
                changes={"reason": "token_reuse"},
            )
            raise AuthenticationError("Your session has expired.", code="token_reused")

        raise AuthenticationError("Your session has ended.", code="token_revoked")

    if stored.expires_at <= datetime.now(UTC):
        raise AuthenticationError("Your session has expired.", code="token_expired")

    user = await get_user(session, stored.user_id)
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.", code="account_inactive")

    access_token, new_refresh, expires_in = await issue_token_pair(
        session, user, ip_address=ip_address, user_agent=user_agent
    )

    async with system_context(session):
        replacement = (
            (
                await session.execute(
                    select(RefreshToken).where(RefreshToken.token_hash == fingerprint(new_refresh))
                )
            )
            .scalars()
            .first()
        )
        stored.revoked_at = utc_now()
        stored.revoked_reason = "rotated"
        stored.replaced_by_id = replacement.id if replacement else None
        session.add(stored)
        await session.flush()

    return user, access_token, new_refresh, expires_in


async def revoke_refresh_token(
    session: AsyncSession, refresh_token: str, *, reason: str = "logout"
) -> None:
    """Log out one session. Unknown or already-revoked tokens are a no-op."""
    token_hash = fingerprint(refresh_token)
    async with system_context(session):
        stored = (
            (
                await session.execute(
                    select(RefreshToken).where(RefreshToken.token_hash == token_hash)
                )
            )
            .scalars()
            .first()
        )
        if stored is not None and stored.revoked_at is None:
            stored.revoked_at = utc_now()
            stored.revoked_reason = reason
            session.add(stored)
            await session.flush()


async def revoke_all_refresh_tokens(
    session: AsyncSession, user_id: uuid.UUID, *, reason: str
) -> int:
    """End every active session for a user. Returns how many were revoked."""
    async with system_context(session):
        rows = (
            (
                await session.execute(
                    select(RefreshToken).where(
                        RefreshToken.user_id == user_id,
                        col(RefreshToken.revoked_at).is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        now = utc_now()
        for token in rows:
            token.revoked_at = now
            token.revoked_reason = reason
            session.add(token)
        await session.flush()
    return len(rows)


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str, str, int]:
    """Full login flow: authenticate, mint tokens, audit."""
    user = await authenticate(
        session, email=email, password=password, ip_address=ip_address, user_agent=user_agent
    )
    access_token, refresh_token, expires_in = await issue_token_pair(
        session, user, ip_address=ip_address, user_agent=user_agent
    )
    await record_audit(
        session,
        action="auth.login",
        actor=user,
        hospital_id=user.hospital_id,
        resource_type="user",
        resource_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return user, access_token, refresh_token, expires_in


async def user_from_access_token(session: AsyncSession, token: str) -> User:
    """Resolve a bearer token to a live, active user."""
    claims = decode_token(token, expected_type="access")
    user = await get_user(session, claims.subject)
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.", code="account_inactive")
    return user
