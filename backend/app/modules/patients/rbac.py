"""Permissions enforced by the `patients` module.

Seeded by this module's own migration, per the rule set in
`identity.rbac`: a module ships the permissions it actually enforces, never
placeholders for actions the software cannot yet perform.
"""

from __future__ import annotations

from typing import Final

from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "patients"


class PatientPermissions:
    CREATE = "patient:create"
    READ = "patient:read"
    UPDATE = "patient:update"
    # Separated from `update` on purpose: merging rewrites which record the
    # history belongs to, and that is a records-office decision.
    MERGE = "patient:merge"
    # Reading identifiers (Aadhaar, scheme numbers) is a narrower right than
    # reading demographics — DPDP data minimisation.
    READ_IDENTIFIERS = "patient:read_identifiers"
    MANAGE_IDENTIFIERS = "patient:manage_identifiers"
    MANAGE_ALERTS = "patient:manage_alerts"
    MANAGE_CONSENT = "patient:manage_consent"
    # SEC-01. Browsing the whole directory is not the same right as opening a
    # patient: a lab or a cash counter works from the visit in front of it,
    # and "page through everyone" is how a phone book walks out of the door.
    LIST = "patient:list"
    # SEC-01. Seeing a patient's full phone number. Granted to the people who
    # ring patients; everybody else sees `98•••••670`.
    CONTACT = "patient:contact"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(PatientPermissions.CREATE, MODULE, "Register a patient."),
    PermissionDef(PatientPermissions.READ, MODULE, "View and search patient records."),
    PermissionDef(PatientPermissions.UPDATE, MODULE, "Edit patient demographics."),
    PermissionDef(PatientPermissions.MERGE, MODULE, "Merge duplicate patient records."),
    PermissionDef(
        PatientPermissions.READ_IDENTIFIERS, MODULE, "View government and scheme identifiers."
    ),
    PermissionDef(
        PatientPermissions.MANAGE_IDENTIFIERS, MODULE, "Add or remove patient identifiers."
    ),
    PermissionDef(
        PatientPermissions.MANAGE_ALERTS, MODULE, "Record or retire patient safety alerts."
    ),
    PermissionDef(
        PatientPermissions.MANAGE_CONSENT, MODULE, "Capture or withdraw patient consent."
    ),
    PermissionDef(PatientPermissions.LIST, MODULE, "Browse the whole patient directory."),
    PermissionDef(PatientPermissions.CONTACT, MODULE, "See patients' full phone numbers."),
)


# Who gets what, and why:
#   * Reception registers and edits — that is the whole "staff, not doctors" point.
#   * Clinical staff read, and may raise a safety alert; a nurse who learns of
#     an allergy must be able to record it without finding an administrator.
#   * Identifiers are visible only to the roles that bill or maintain records.
_CLINICAL_READ = (
    PatientPermissions.READ,
    PatientPermissions.MANAGE_ALERTS,
    # Doctors and nurses ring patients and families about results and care.
    PatientPermissions.CONTACT,
)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: tuple(perm.code for perm in PERMISSION_CATALOGUE),
    Roles.HOSPITAL_ADMIN: tuple(perm.code for perm in PERMISSION_CATALOGUE),
    Roles.RECEPTIONIST: (
        PatientPermissions.CREATE,
        PatientPermissions.READ,
        PatientPermissions.LIST,
        PatientPermissions.CONTACT,
        PatientPermissions.UPDATE,
        PatientPermissions.MANAGE_IDENTIFIERS,
        PatientPermissions.MANAGE_CONSENT,
    ),
    Roles.DOCTOR: _CLINICAL_READ,
    Roles.NURSE: _CLINICAL_READ,
    Roles.LAB_TECH: (PatientPermissions.READ,),
    Roles.PATHOLOGIST: (PatientPermissions.READ,),
    Roles.RADIOLOGIST: (PatientPermissions.READ,),
    Roles.PHARMACIST: (PatientPermissions.READ,),
    Roles.CASHIER: (PatientPermissions.READ, PatientPermissions.READ_IDENTIFIERS),
    Roles.ACCOUNTANT: (PatientPermissions.READ, PatientPermissions.READ_IDENTIFIERS),
    Roles.BILLING_STAFF: (
        PatientPermissions.READ,
        PatientPermissions.READ_IDENTIFIERS,
        PatientPermissions.MANAGE_IDENTIFIERS,
    ),
    Roles.RECORDS_OFFICER: (
        PatientPermissions.READ,
        PatientPermissions.LIST,
        PatientPermissions.CONTACT,
        PatientPermissions.UPDATE,
        PatientPermissions.MERGE,
        PatientPermissions.READ_IDENTIFIERS,
        PatientPermissions.MANAGE_IDENTIFIERS,
        PatientPermissions.MANAGE_CONSENT,
    ),
    # The admission desk completes what the four-field OPD registration left
    # out — address, guardian — because an inpatient stay needs it.
    Roles.ADMISSION_DESK: (
        PatientPermissions.READ,
        PatientPermissions.UPDATE,
        # Rings the family about the bed, the deposit and the discharge.
        PatientPermissions.CONTACT,
    ),
    Roles.AUDITOR: (PatientPermissions.READ,),
}
