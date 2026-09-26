"""Permission catalogue and the seeded role definitions (CLAUDE.md §8).

RBAC is **data-driven**: at runtime, roles and permissions are rows, and a new
role needs an INSERT rather than a deploy. This file is the source those rows
are seeded *from*, and the place route handlers import permission codes from so
a typo is an import error rather than a silent 403.

Scope rule for future phases: each module defines the permissions it enforces
and seeds them in its own migration, extending `SYSTEM_ROLE_PERMISSIONS` for
the roles that should hold them. Do not pre-seed permissions for actions the
software cannot yet perform — an unenforced permission is a lie in an access
review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

MODULE: Final[str] = "identity"
TENANCY_MODULE: Final[str] = "tenancy"


@dataclass(frozen=True, slots=True)
class PermissionDef:
    code: str
    module: str
    description: str


# ---------------------------------------------------------------------------
# Permission codes. Import these constants; never type the string at a call site.
# ---------------------------------------------------------------------------
class Permissions:
    # --- tenancy ----------------------------------------------------------
    HOSPITAL_CREATE = "hospital:create"
    HOSPITAL_READ = "hospital:read"
    HOSPITAL_UPDATE = "hospital:update"
    HOSPITAL_DEACTIVATE = "hospital:deactivate"

    # --- identity ---------------------------------------------------------
    USER_CREATE = "user:create"
    USER_READ = "user:read"
    USER_UPDATE = "user:update"
    USER_DEACTIVATE = "user:deactivate"
    USER_RESET_PASSWORD = "user:reset_password"  # noqa: S105 - a permission code, not a secret

    ROLE_CREATE = "role:create"
    ROLE_READ = "role:read"
    ROLE_UPDATE = "role:update"
    ROLE_ASSIGN = "role:assign"

    AUDIT_READ = "audit:read"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(Permissions.HOSPITAL_CREATE, TENANCY_MODULE, "Register a new hospital tenant."),
    PermissionDef(Permissions.HOSPITAL_READ, TENANCY_MODULE, "View hospital details."),
    PermissionDef(Permissions.HOSPITAL_UPDATE, TENANCY_MODULE, "Edit hospital details."),
    PermissionDef(
        Permissions.HOSPITAL_DEACTIVATE, TENANCY_MODULE, "Suspend or reactivate a hospital."
    ),
    PermissionDef(Permissions.USER_CREATE, MODULE, "Create a staff account."),
    PermissionDef(Permissions.USER_READ, MODULE, "View staff accounts."),
    PermissionDef(Permissions.USER_UPDATE, MODULE, "Edit a staff account."),
    PermissionDef(Permissions.USER_DEACTIVATE, MODULE, "Deactivate a staff account."),
    PermissionDef(Permissions.USER_RESET_PASSWORD, MODULE, "Reset another user's password."),
    PermissionDef(Permissions.ROLE_CREATE, MODULE, "Define a hospital-specific role."),
    PermissionDef(Permissions.ROLE_READ, MODULE, "View roles and their permissions."),
    PermissionDef(Permissions.ROLE_UPDATE, MODULE, "Edit a hospital-specific role."),
    PermissionDef(Permissions.ROLE_ASSIGN, MODULE, "Assign or remove a user's roles."),
    PermissionDef(Permissions.AUDIT_READ, MODULE, "Read the audit log."),
)


# ---------------------------------------------------------------------------
# Seeded system roles (CLAUDE.md §8). Extensible — these are just the defaults.
#
# Two are additions to §8's original twelve: `PATHOLOGIST` and `HOUSEKEEPING`.
# §8 says the role set is "extensible — data-driven, not hardcoded in logic",
# and both were added the same way a hospital would add its own: a row in
# `roles`, rows in `role_permissions`, and the code constant below so call sites
# can name them. Neither required a change to any authorisation check.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RoleDef:
    code: str
    name: str
    description: str


class Roles:
    PLATFORM_ADMIN = "PLATFORM_ADMIN"
    HOSPITAL_ADMIN = "HOSPITAL_ADMIN"
    DOCTOR = "DOCTOR"
    NURSE = "NURSE"
    RECEPTIONIST = "RECEPTIONIST"
    CASHIER = "CASHIER"
    LAB_TECH = "LAB_TECH"
    # Signs off laboratory work — the lab's counterpart to RADIOLOGIST. Not in
    # CLAUDE.md §8's original list; added because the diagnostics workflow
    # separates entering a result from verifying one, and without this role the
    # lab signature had nowhere clinically correct to sit.
    PATHOLOGIST = "PATHOLOGIST"
    RADIOLOGIST = "RADIOLOGIST"
    PHARMACIST = "PHARMACIST"
    BILLING_STAFF = "BILLING_STAFF"
    RECORDS_OFFICER = "RECORDS_OFFICER"
    # Turns a discharged bed around. Also not in §8's original list; added with
    # `ipd`, because the bed cleaning lifecycle has a rung that is somebody's
    # actual job and giving it to nurses by default overstated their duties.
    HOUSEKEEPING = "HOUSEKEEPING"
    # Works the admission counter: patients the OPD has sent for admission,
    # the bed, the paperwork. Added for the reception → admission hand-off; a
    # hospital where one person does both simply gives them both roles.
    ADMISSION_DESK = "ADMISSION_DESK"
    AUDITOR = "AUDITOR"


SYSTEM_ROLES: Final[tuple[RoleDef, ...]] = (
    RoleDef(Roles.PLATFORM_ADMIN, "Platform Administrator", "Manages hospitals across tenants."),
    RoleDef(Roles.HOSPITAL_ADMIN, "Hospital Administrator", "Full access within one hospital."),
    RoleDef(Roles.DOCTOR, "Doctor", "Clinical actions on own or assigned patients."),
    RoleDef(Roles.NURSE, "Nurse", "Vitals, nursing notes, queue and medication records."),
    RoleDef(Roles.RECEPTIONIST, "Receptionist", "Registration, scheduling and check-in."),
    RoleDef(Roles.CASHIER, "Cashier", "Payments and invoices."),
    RoleDef(Roles.LAB_TECH, "Lab Technician", "Laboratory result entry."),
    RoleDef(Roles.PATHOLOGIST, "Pathologist", "Verifies and signs laboratory reports."),
    RoleDef(Roles.RADIOLOGIST, "Radiologist", "Radiology reporting."),
    RoleDef(Roles.PHARMACIST, "Pharmacist", "Dispensing and pharmacy stock."),
    RoleDef(Roles.BILLING_STAFF, "Billing Staff", "Invoices and insurance or scheme claims."),
    RoleDef(
        Roles.RECORDS_OFFICER,
        "Records Officer",
        "Records death, referral and LAMA; manages medical records.",
    ),
    RoleDef(
        Roles.HOUSEKEEPING,
        "Housekeeping",
        "Turns discharged beds around; no access to patient data.",
    ),
    RoleDef(
        Roles.ADMISSION_DESK,
        "Admission Desk",
        "Admits patients sent from OPD: bed, admission paperwork.",
    ),
    RoleDef(Roles.AUDITOR, "Auditor", "Read-only access to audit logs and reports."),
)


# Only the permissions that exist today. Clinical, billing and diagnostics
# roles look sparse here on purpose — they gain permissions when the modules
# that enforce them are built.
_ALL_HOSPITAL_ADMIN = (
    Permissions.HOSPITAL_READ,
    Permissions.HOSPITAL_UPDATE,
    Permissions.USER_CREATE,
    Permissions.USER_READ,
    Permissions.USER_UPDATE,
    Permissions.USER_DEACTIVATE,
    Permissions.USER_RESET_PASSWORD,
    Permissions.ROLE_CREATE,
    Permissions.ROLE_READ,
    Permissions.ROLE_UPDATE,
    Permissions.ROLE_ASSIGN,
    Permissions.AUDIT_READ,
)

_STAFF_BASELINE = (Permissions.HOSPITAL_READ,)

SYSTEM_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: tuple(perm.code for perm in PERMISSION_CATALOGUE),
    Roles.HOSPITAL_ADMIN: _ALL_HOSPITAL_ADMIN,
    Roles.AUDITOR: (
        Permissions.HOSPITAL_READ,
        Permissions.USER_READ,
        Permissions.ROLE_READ,
        Permissions.AUDIT_READ,
    ),
    Roles.DOCTOR: _STAFF_BASELINE,
    Roles.NURSE: _STAFF_BASELINE,
    Roles.RECEPTIONIST: _STAFF_BASELINE,
    Roles.CASHIER: _STAFF_BASELINE,
    Roles.LAB_TECH: _STAFF_BASELINE,
    Roles.RADIOLOGIST: _STAFF_BASELINE,
    Roles.PHARMACIST: _STAFF_BASELINE,
    Roles.BILLING_STAFF: _STAFF_BASELINE,
    Roles.RECORDS_OFFICER: _STAFF_BASELINE,
    Roles.ADMISSION_DESK: _STAFF_BASELINE,
}
