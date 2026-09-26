"""Identity HTTP routes: authentication, users, roles, audit log."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import CurrentUser, SessionDep, get_client_ip, require, resolve_target_hospital
from app.core.exceptions import NotFoundError
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import service
from app.modules.identity.models import User
from app.modules.identity.rbac import Permissions
from app.modules.identity.schemas import (
    AuditLogRead,
    ChangePasswordRequest,
    CurrentUserRead,
    LoginRequest,
    PermissionRead,
    RefreshRequest,
    RoleAssignmentRequest,
    RoleCreate,
    RoleDetail,
    RoleRead,
    TokenPair,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.modules.identity.service import AuthContext

auth_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])
roles_router = APIRouter(prefix="/roles", tags=["roles"])
audit_router = APIRouter(prefix="/audit-logs", tags=["audit"])

CanCreateUser = Annotated[AuthContext, Depends(require(Permissions.USER_CREATE))]
CanReadUser = Annotated[AuthContext, Depends(require(Permissions.USER_READ))]
CanUpdateUser = Annotated[AuthContext, Depends(require(Permissions.USER_UPDATE))]
CanDeactivateUser = Annotated[AuthContext, Depends(require(Permissions.USER_DEACTIVATE))]
CanResetPassword = Annotated[AuthContext, Depends(require(Permissions.USER_RESET_PASSWORD))]
CanReadRole = Annotated[AuthContext, Depends(require(Permissions.ROLE_READ))]
CanCreateRole = Annotated[AuthContext, Depends(require(Permissions.ROLE_CREATE))]
CanAssignRole = Annotated[AuthContext, Depends(require(Permissions.ROLE_ASSIGN))]
CanReadAudit = Annotated[AuthContext, Depends(require(Permissions.AUDIT_READ))]


# ---------------------------------------------------------------------------
# Authentication — the only unauthenticated routes in the system
# ---------------------------------------------------------------------------
@auth_router.post("/login", response_model=TokenPair, summary="Exchange credentials for tokens")
async def login(payload: LoginRequest, session: SessionDep, request: Request) -> TokenPair:
    user, access_token, refresh_token, expires_in = await service.login(
        session,
        email=payload.email,
        password=payload.password,
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        must_change_password=user.must_change_password,
    )


@auth_router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
async def refresh(payload: RefreshRequest, session: SessionDep, request: Request) -> TokenPair:
    user, access_token, refresh_token, expires_in = await service.rotate_refresh_token(
        session,
        payload.refresh_token,
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        must_change_password=user.must_change_password,
    )


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the current session",
)
async def logout(payload: RefreshRequest, session: SessionDep) -> None:
    # Unauthenticated on purpose: a client whose access token has already
    # expired must still be able to invalidate its refresh token.
    await service.revoke_refresh_token(session, payload.refresh_token)
    await session.commit()


@auth_router.get("/me", response_model=CurrentUserRead, summary="The authenticated user")
async def me(context: CurrentUser) -> CurrentUserRead:
    # Not `_render`: the roles are already resolved on the auth context that
    # authenticated this very request, so re-querying them would be a round
    # trip to learn something we are holding.
    return CurrentUserRead.model_validate(
        {
            **context.user.model_dump(),
            "roles": sorted(context.roles),
            "permissions": sorted(context.permissions),
        }
    )


@auth_router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your own password",
)
async def change_password(
    payload: ChangePasswordRequest,
    session: SessionDep,
    context: CurrentUser,
) -> None:
    await service.change_password(
        session,
        context.user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
async def _render(session: SessionDep, user: User) -> UserRead:
    """One account, with its roles attached.

    `UserRead.roles` is required precisely so this cannot be skipped: the roles
    are in a join table, and a response that omits them renders as an account
    with no access at all.
    """
    grouped = await service.get_roles_for_users(session, [user.id])
    return _with_roles(user, grouped.get(user.id, []))


async def _render_many(session: SessionDep, users: Sequence[User]) -> list[UserRead]:
    """A page of accounts and their roles, in one extra query rather than one
    per row — the same bulk-decoration shape as the queue and lab boards."""
    grouped = await service.get_roles_for_users(session, [user.id for user in users])
    return [_with_roles(user, grouped.get(user.id, [])) for user in users]


def _with_roles(user: User, roles: list[str]) -> UserRead:
    """Validate through a dict rather than `from_attributes`.

    `roles` is a required field with no counterpart on the ORM row, so
    `model_validate(user)` cannot satisfy it — which is the point of making it
    required. Extra keys from the row (the password hash among them) are
    dropped by Pydantic and have no field to land on.
    """
    return UserRead.model_validate({**user.model_dump(), "roles": roles})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@users_router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a staff account",
)
async def create_user(
    payload: UserCreate,
    session: SessionDep,
    request: Request,
    context: CanCreateUser,
) -> UserRead:
    # The tenant comes from the caller's context, not the request body.
    hospital_id = resolve_target_hospital(context, payload.hospital_id)
    user = await service.create_user(session, payload, actor=context.user, hospital_id=hospital_id)
    await session.commit()
    return await _render(session, user)


@users_router.get("", response_model=Page[UserRead], summary="List staff accounts")
async def list_users(
    session: SessionDep,
    context: CanReadUser,
    params: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=100)] = None,
    include_inactive: bool = False,
) -> Page[UserRead]:
    # A platform admin listing without a tenant sees everyone; anyone else is
    # confined to their own hospital.
    scope = None if context.is_platform_admin else context.hospital_id
    users, total = await service.list_users(
        session, params, hospital_id=scope, search=search, include_inactive=include_inactive
    )
    return Page.build(await _render_many(session, users), total=total, params=params)


async def _load_visible_user(session: SessionDep, context: AuthContext, user_id: uuid.UUID) -> User:
    """Load a user, or 404 if the caller is not entitled to see them.

    A 404 rather than a 403 on the cross-tenant case, deliberately: a 403 would
    confirm that the account exists in some other hospital.
    """
    user = await service.get_user(session, user_id)
    if not context.is_platform_admin and user.hospital_id != context.hospital_id:
        raise NotFoundError("User not found.", code="user_not_found")
    return user


@users_router.get("/{user_id}", response_model=UserRead, summary="Get a staff account")
async def get_user(user_id: uuid.UUID, session: SessionDep, context: CanReadUser) -> UserRead:
    user = await _load_visible_user(session, context, user_id)
    return await _render(session, user)


@users_router.patch("/{user_id}", response_model=UserRead, summary="Update a staff account")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    session: SessionDep,
    context: CanUpdateUser,
) -> UserRead:
    user = await _load_visible_user(session, context, user_id)
    updated = await service.update_user(session, user, payload, actor=context.user)
    await session.commit()
    return await _render(session, updated)


@users_router.post(
    "/{user_id}/deactivate",
    response_model=UserRead,
    summary="Deactivate a staff account",
)
async def deactivate_user(
    user_id: uuid.UUID,
    session: SessionDep,
    context: CanDeactivateUser,
) -> UserRead:
    user = await _load_visible_user(session, context, user_id)
    updated = await service.set_user_active(session, user, active=False, actor=context.user)
    await session.commit()
    return await _render(session, updated)


@users_router.post(
    "/{user_id}/roles",
    response_model=UserRead,
    summary="Replace a user's role assignments",
)
async def assign_roles(
    user_id: uuid.UUID,
    payload: RoleAssignmentRequest,
    session: SessionDep,
    context: CanAssignRole,
) -> UserRead:
    user = await _load_visible_user(session, context, user_id)
    await service.assign_roles(session, user, payload.role_codes, actor=context.user)
    await session.commit()
    return await _render(session, user)


# ---------------------------------------------------------------------------
# Roles & permissions
# ---------------------------------------------------------------------------
@roles_router.get("", response_model=Page[RoleRead], summary="List roles")
async def list_roles(
    session: SessionDep,
    context: CanReadRole,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[RoleRead]:
    roles, total = await service.list_roles(session, params, hospital_id=context.hospital_id)
    return Page.build([RoleRead.model_validate(r) for r in roles], total=total, params=params)


@roles_router.post(
    "",
    response_model=RoleDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Define a hospital-specific role",
)
async def create_role(
    payload: RoleCreate,
    session: SessionDep,
    context: CanCreateRole,
) -> RoleDetail:
    hospital_id = resolve_target_hospital(context, None)
    if hospital_id is None:
        raise NotFoundError("A hospital context is required to define a role.")
    role = await service.create_role(session, payload, hospital_id=hospital_id, actor=context.user)
    await session.commit()
    permissions = await service.get_role_permissions(session, role.id)
    return RoleDetail(**RoleRead.model_validate(role).model_dump(), permissions=permissions)


@roles_router.get(
    "/permissions",
    response_model=list[PermissionRead],
    summary="Permission catalogue",
)
async def list_permissions(session: SessionDep, context: CanReadRole) -> list[PermissionRead]:
    permissions = await service.list_permissions(session)
    return [PermissionRead.model_validate(p) for p in permissions]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@audit_router.get("", response_model=Page[AuditLogRead], summary="Read the audit log")
async def list_audit_logs(
    session: SessionDep,
    context: CanReadAudit,
    params: Annotated[PageParams, Depends(page_params)],
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor_user_id: uuid.UUID | None = None,
) -> Page[AuditLogRead]:
    scope = None if context.is_platform_admin else context.hospital_id
    entries, total = await service.list_audit_logs(
        session, params, hospital_id=scope, action=action, actor_user_id=actor_user_id
    )
    return Page.build(
        [AuditLogRead.model_validate(entry) for entry in entries], total=total, params=params
    )
