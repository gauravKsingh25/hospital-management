"""Permissions enforced by `scheduling` (and the department routes in tenancy)."""

from __future__ import annotations

from typing import Final

from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "scheduling"
TENANCY_MODULE: Final[str] = "tenancy"


class SchedulingPermissions:
    DEPARTMENT_READ = "department:read"
    DEPARTMENT_MANAGE = "department:manage"

    DOCTOR_READ = "doctor:read"
    DOCTOR_MANAGE = "doctor:manage"
    # Separated from `doctor:manage`: a doctor may set their own clinic hours
    # without being able to create or retire colleagues.
    AVAILABILITY_MANAGE = "availability:manage"

    APPOINTMENT_CREATE = "appointment:create"
    APPOINTMENT_READ = "appointment:read"
    APPOINTMENT_UPDATE = "appointment:update"
    APPOINTMENT_CANCEL = "appointment:cancel"

    QUEUE_READ = "queue:read"
    # Check in, call next, skip, complete — the running of the waiting room.
    QUEUE_MANAGE = "queue:manage"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(SchedulingPermissions.DEPARTMENT_READ, TENANCY_MODULE, "View departments."),
    PermissionDef(
        SchedulingPermissions.DEPARTMENT_MANAGE, TENANCY_MODULE, "Create or edit departments."
    ),
    PermissionDef(SchedulingPermissions.DOCTOR_READ, MODULE, "View doctor profiles."),
    PermissionDef(SchedulingPermissions.DOCTOR_MANAGE, MODULE, "Create or edit doctor profiles."),
    PermissionDef(SchedulingPermissions.AVAILABILITY_MANAGE, MODULE, "Set clinic hours and leave."),
    PermissionDef(SchedulingPermissions.APPOINTMENT_CREATE, MODULE, "Book an appointment."),
    PermissionDef(SchedulingPermissions.APPOINTMENT_READ, MODULE, "View appointments."),
    PermissionDef(SchedulingPermissions.APPOINTMENT_UPDATE, MODULE, "Edit or reschedule."),
    PermissionDef(
        SchedulingPermissions.APPOINTMENT_CANCEL, MODULE, "Cancel or mark an appointment no-show."
    ),
    PermissionDef(SchedulingPermissions.QUEUE_READ, MODULE, "View the OPD queue."),
    PermissionDef(
        SchedulingPermissions.QUEUE_MANAGE, MODULE, "Check in, call, skip and complete tokens."
    ),
)

_READ_ONLY = (
    SchedulingPermissions.DEPARTMENT_READ,
    SchedulingPermissions.DOCTOR_READ,
    SchedulingPermissions.APPOINTMENT_READ,
    SchedulingPermissions.QUEUE_READ,
)

# Reception runs the front of house: books, checks in, and works the queue.
_RECEPTION = (
    *_READ_ONLY,
    SchedulingPermissions.APPOINTMENT_CREATE,
    SchedulingPermissions.APPOINTMENT_UPDATE,
    SchedulingPermissions.APPOINTMENT_CANCEL,
    SchedulingPermissions.QUEUE_MANAGE,
)

# A doctor calls their own next patient and sets their own hours — but does not
# book appointments or run the counter. Keeping their surface small is the
# entire point of CLAUDE.md §7.
_DOCTOR = (
    *_READ_ONLY,
    SchedulingPermissions.QUEUE_MANAGE,
    SchedulingPermissions.AVAILABILITY_MANAGE,
)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: tuple(perm.code for perm in PERMISSION_CATALOGUE),
    Roles.HOSPITAL_ADMIN: tuple(perm.code for perm in PERMISSION_CATALOGUE),
    Roles.RECEPTIONIST: _RECEPTION,
    Roles.DOCTOR: _DOCTOR,
    # Nurses marshal the waiting room and prepare patients.
    Roles.NURSE: (*_READ_ONLY, SchedulingPermissions.QUEUE_MANAGE),
    Roles.CASHIER: _READ_ONLY,
    Roles.BILLING_STAFF: _READ_ONLY,
    Roles.RECORDS_OFFICER: _READ_ONLY,
    # Which doctor sent them, and from which department.
    Roles.ADMISSION_DESK: (
        SchedulingPermissions.DEPARTMENT_READ,
        SchedulingPermissions.DOCTOR_READ,
    ),
    Roles.AUDITOR: _READ_ONLY,
    Roles.LAB_TECH: (SchedulingPermissions.DEPARTMENT_READ, SchedulingPermissions.DOCTOR_READ),
    Roles.PATHOLOGIST: (
        SchedulingPermissions.DEPARTMENT_READ,
        SchedulingPermissions.DOCTOR_READ,
    ),
    Roles.RADIOLOGIST: (
        SchedulingPermissions.DEPARTMENT_READ,
        SchedulingPermissions.DOCTOR_READ,
    ),
    Roles.PHARMACIST: (SchedulingPermissions.DEPARTMENT_READ, SchedulingPermissions.DOCTOR_READ),
}
