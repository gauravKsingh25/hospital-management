"""Request/response DTOs for `identity`.

Note what is absent: no schema anywhere exposes `password_hash`, `totp_secret`
or the raw refresh token. Read models are explicit allow-lists, not
`model_config = from_attributes` over the whole table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

__all__ = [
    "AuditLogRead",
    "ChangePasswordRequest",
    "LoginRequest",
    "PermissionRead",
    "RefreshRequest",
    "RoleAssignmentRequest",
    "RoleCreate",
    "RoleRead",
    "TokenPair",
    "UserCreate",
    "UserRead",
    "UserUpdate",
]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=256)]


class RefreshRequest(BaseModel):
    refresh_token: Annotated[str, Field(min_length=1, max_length=512)]


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int = Field(description="Access token lifetime in seconds.")
    must_change_password: bool = Field(
        default=False,
        description="When true the client must send the user to a password change screen.",
    )


class ChangePasswordRequest(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: Annotated[str, Field(min_length=1, max_length=256)]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class UserCreate(BaseModel):
    email: EmailStr
    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    phone: Annotated[str | None, Field(max_length=20)] = None
    password: Annotated[str, Field(min_length=1, max_length=256)]
    role_codes: Annotated[
        list[str],
        Field(default_factory=list, description="System or hospital role codes to assign."),
    ]
    # Platform admins may create a user for any hospital; a hospital admin's
    # own tenant is forced by the service regardless of what is sent.
    hospital_id: uuid.UUID | None = None

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class UserUpdate(BaseModel):
    full_name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    phone: Annotated[str | None, Field(max_length=20)] = None
    is_active: bool | None = None


class RoleAssignmentRequest(BaseModel):
    role_codes: Annotated[
        list[str],
        Field(description="The user's complete role set after this call."),
    ]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hospital_id: uuid.UUID | None
    email: str
    full_name: str
    phone: str | None
    is_active: bool
    must_change_password: bool
    two_factor_enabled: bool
    last_login_at: datetime | None
    created_at: datetime

    # The roles live in a join table, so `from_attributes` cannot reach them and
    # the router has to supply them. Without this the staff screen cannot answer
    # the question it exists to answer — who is a doctor here — and role
    # assignment returns a body that does not show what it just changed.
    #
    # **Required, not defaulted.** A default would let a call site forget, and a
    # `roles: []` that means "nobody bothered to load them" is worse than an
    # error: it renders as an account with no access at all. Being required
    # makes `model_validate(user)` fail loudly, which is why every route in this
    # module goes through `_render` instead.
    roles: list[str]


class CurrentUserRead(UserRead):
    """`/auth/me` — adds the resolved authorisation context.

    The client uses `permissions` to decide which controls to render. It is
    never the authorisation decision itself; that is re-checked server-side on
    every request.
    """

    roles: list[str]
    permissions: list[str]


# ---------------------------------------------------------------------------
# Roles & permissions
# ---------------------------------------------------------------------------
class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    module: str
    description: str


class RoleCreate(BaseModel):
    code: Annotated[str, Field(min_length=2, max_length=64)]
    name: Annotated[str, Field(min_length=2, max_length=128)]
    description: Annotated[str | None, Field(max_length=500)] = None
    permission_codes: Annotated[list[str], Field(default_factory=list)]

    @field_validator("code")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper().replace(" ", "_")


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hospital_id: uuid.UUID | None
    code: str
    name: str
    description: str | None
    is_system: bool


class RoleDetail(RoleRead):
    permissions: list[str]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hospital_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    action: str
    resource_type: str | None
    resource_id: uuid.UUID | None
    changes: dict[str, Any] | None
    succeeded: bool
    ip_address: str | None
    created_at: datetime
