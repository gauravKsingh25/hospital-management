"""Shared FastAPI dependencies: authentication, tenant context, RBAC.

Every protected route depends on `require(...)`. That single choke point is
what makes "enforce tenant isolation AND role permission on every request"
(CLAUDE.md §8) something the type system nudges you towards rather than
something each router has to remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session, set_bypass_rls, set_tenant_context
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.modules.identity.service import AuthContext, build_auth_context, user_from_access_token

# auto_error=False so a missing header produces our own typed 401 with the
# standard error envelope rather than FastAPI's bare `{"detail": ...}`.
_bearer = HTTPBearer(auto_error=False, description="JWT access token")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_client_ip(request: Request) -> str | None:
    """Best-effort client address for the audit log.

    `X-Forwarded-For` is only meaningful behind a proxy we control; it is
    recorded as provenance, never used for an access decision.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None


async def get_auth_context(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthContext:
    """Resolve the bearer token to a user and bind the request to its tenant.

    The tenant is taken from the user row, never from the token's `hid` claim
    or a request header — a claim is only as trustworthy as the moment it was
    signed, and a user may have been moved or suspended since.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication is required.")

    user = await user_from_access_token(session, credentials.credentials)
    context = await build_auth_context(session, user)

    # Bind the database session for the rest of the request so RLS backstops
    # any query that forgets its own filter.
    if context.is_platform_admin:
        # PLATFORM_ADMIN is the one role CLAUDE.md §3 permits to cross tenants;
        # its queries legitimately span hospitals, so isolation is lifted for
        # the request rather than fought around query by query.
        await set_bypass_rls(session, enabled=True)
    elif context.hospital_id is not None:
        await set_tenant_context(session, context.hospital_id)

    request.state.auth = context
    return context


CurrentUser = Annotated[AuthContext, Depends(get_auth_context)]


def require(*permissions: str) -> Callable[[AuthContext], Awaitable[AuthContext]]:
    """Dependency factory gating a route on one or more permission codes.

    Holding *any* of the listed permissions is enough — routes that need a
    conjunction should depend on `require()` twice.

        @router.post("/users", dependencies=[Depends(require(Permissions.USER_CREATE))])
    """

    async def dependency(context: CurrentUser) -> AuthContext:
        if not context.has_permission(*permissions):
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={"required": list(permissions)},
            )
        return context

    return dependency


def resolve_target_hospital(context: AuthContext, requested: uuid.UUID | None) -> uuid.UUID | None:
    """Decide which tenant a write applies to.

    A platform admin may act on any hospital and must say which. Everyone else
    is pinned to their own, whatever the request body claims — this is the
    check that stops a hospital admin from creating users inside a competitor's
    tenant.
    """
    if context.is_platform_admin:
        return requested if requested is not None else context.hospital_id
    if requested is not None and requested != context.hospital_id:
        raise PermissionDeniedError("You cannot act on behalf of another hospital.")
    return context.hospital_id


async def require_tenant(context: CurrentUser) -> uuid.UUID:
    """For routes that are meaningless without a tenant (staff-facing screens)."""
    if context.hospital_id is None:
        raise PermissionDeniedError(
            "This action requires a hospital context.",
            code="tenant_required",
        )
    return context.hospital_id


TenantId = Annotated[uuid.UUID, Depends(require_tenant)]
