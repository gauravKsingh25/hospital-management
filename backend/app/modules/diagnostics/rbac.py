"""Permissions enforced by `diagnostics`.

The split that matters is **entering a result** versus **verifying one**. They
are separate permissions, and the service additionally refuses to let one person
do both on the same report. A lab where the same technician measures and signs
has no second pair of eyes, and the second pair of eyes is the entire reason
verification exists.

Lab verification sits with `PATHOLOGIST` — the lab's counterpart to
`RADIOLOGIST`, and a role added to the seed set precisely because this split
demanded it. `DOCTOR` keeps it too, deliberately: a mid-size hospital without a
resident pathologist still has to release results, and a permission model that
makes that impossible is one the hospital works around by handing somebody the
administrator account.
"""

from __future__ import annotations

from typing import Final

from app.modules.diagnostics.models import DiagnosticDiscipline
from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "diagnostics"


class DiagnosticsPermissions:
    CATALOGUE_READ = "catalogue:read"
    CATALOGUE_MANAGE = "catalogue:manage"

    SPECIMEN_READ = "specimen:read"
    # Draw the blood, label the tube.
    SPECIMEN_COLLECT = "specimen:collect"
    # Book it in at the bench, or refuse it.
    SPECIMEN_RECEIVE = "specimen:receive"

    REPORT_READ = "report:read"
    RESULT_ENTER = "result:enter"
    # The second pair of eyes. Deliberately not held by whoever entered it.
    RESULT_VERIFY = "result:verify"
    RESULT_AMEND = "result:amend"
    REPORT_CANCEL = "report:cancel"
    # Recording the phone call a panic value demands (NABH).
    CRITICAL_ACKNOWLEDGE = "result:acknowledge_critical"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(DiagnosticsPermissions.CATALOGUE_READ, MODULE, "View the test catalogue."),
    PermissionDef(
        DiagnosticsPermissions.CATALOGUE_MANAGE, MODULE, "Edit tests, analytes and ranges."
    ),
    PermissionDef(DiagnosticsPermissions.SPECIMEN_READ, MODULE, "View specimens."),
    PermissionDef(DiagnosticsPermissions.SPECIMEN_COLLECT, MODULE, "Collect and label a sample."),
    PermissionDef(
        DiagnosticsPermissions.SPECIMEN_RECEIVE, MODULE, "Receive or reject a sample at the lab."
    ),
    PermissionDef(DiagnosticsPermissions.REPORT_READ, MODULE, "Read diagnostic reports."),
    PermissionDef(DiagnosticsPermissions.RESULT_ENTER, MODULE, "Enter results and findings."),
    PermissionDef(DiagnosticsPermissions.RESULT_VERIFY, MODULE, "Verify and release a report."),
    PermissionDef(DiagnosticsPermissions.RESULT_AMEND, MODULE, "Amend a released report."),
    PermissionDef(DiagnosticsPermissions.REPORT_CANCEL, MODULE, "Cancel a report."),
    PermissionDef(
        DiagnosticsPermissions.CRITICAL_ACKNOWLEDGE, MODULE, "Record a critical-value callback."
    ),
)


# Which roles may verify which discipline. Checked in addition to
# `result:verify`, never instead of it — a radiologist signing off a blood
# culture is the same class of mistake as a cashier resulting one.
VERIFICATION_ROLES: Final[dict[DiagnosticDiscipline, frozenset[str]]] = {
    DiagnosticDiscipline.LAB: frozenset(
        {Roles.PATHOLOGIST, Roles.DOCTOR, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}
    ),
    DiagnosticDiscipline.RADIOLOGY: frozenset(
        {Roles.RADIOLOGIST, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}
    ),
}


_ALL = tuple(perm.code for perm in PERMISSION_CATALOGUE)

_READ = (
    DiagnosticsPermissions.CATALOGUE_READ,
    DiagnosticsPermissions.SPECIMEN_READ,
    DiagnosticsPermissions.REPORT_READ,
)

# The lab bench: everything up to but not including the signature.
_LAB_TECH = (
    *_READ,
    DiagnosticsPermissions.SPECIMEN_COLLECT,
    DiagnosticsPermissions.SPECIMEN_RECEIVE,
    DiagnosticsPermissions.RESULT_ENTER,
    DiagnosticsPermissions.CRITICAL_ACKNOWLEDGE,
)

# A pathologist reports and signs their own work, exactly as a radiologist does.
# They do not collect or receive specimens: that is the bench's job, and keeping
# the signatory off the pre-analytical steps is the same second-pair-of-eyes
# argument that separates entering a result from verifying one.
_PATHOLOGIST = (
    *_READ,
    DiagnosticsPermissions.RESULT_ENTER,
    DiagnosticsPermissions.RESULT_VERIFY,
    DiagnosticsPermissions.RESULT_AMEND,
    DiagnosticsPermissions.REPORT_CANCEL,
    DiagnosticsPermissions.CRITICAL_ACKNOWLEDGE,
)

# A radiologist performs and signs their own reporting — that IS the job.
_RADIOLOGIST = (
    *_READ,
    DiagnosticsPermissions.RESULT_ENTER,
    DiagnosticsPermissions.RESULT_VERIFY,
    DiagnosticsPermissions.RESULT_AMEND,
    DiagnosticsPermissions.REPORT_CANCEL,
    DiagnosticsPermissions.CRITICAL_ACKNOWLEDGE,
)

# Doctors read reports and countersign lab work; they do not run the bench.
# `result:amend` and `report:cancel` come with the signature rather than with
# the bench: whoever put their name to a report has to be able to correct it,
# and a correction that needs an administrator is a correction that waits.
_DOCTOR = (
    *_READ,
    DiagnosticsPermissions.RESULT_VERIFY,
    DiagnosticsPermissions.RESULT_AMEND,
    DiagnosticsPermissions.REPORT_CANCEL,
    DiagnosticsPermissions.CRITICAL_ACKNOWLEDGE,
)

# Nurses draw blood on the ward and take the callback when the lab phones.
_NURSE = (
    *_READ,
    DiagnosticsPermissions.SPECIMEN_COLLECT,
    DiagnosticsPermissions.CRITICAL_ACKNOWLEDGE,
)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: _ALL,
    Roles.HOSPITAL_ADMIN: _ALL,
    Roles.LAB_TECH: _LAB_TECH,
    Roles.PATHOLOGIST: _PATHOLOGIST,
    Roles.RADIOLOGIST: _RADIOLOGIST,
    Roles.DOCTOR: _DOCTOR,
    Roles.NURSE: _NURSE,
    # Reception tells a waiting patient whether their report is ready; they do
    # not get to read what is in it.
    Roles.RECEPTIONIST: (
        DiagnosticsPermissions.CATALOGUE_READ,
        DiagnosticsPermissions.SPECIMEN_READ,
    ),
    Roles.CASHIER: (DiagnosticsPermissions.CATALOGUE_READ,),
    # Billing needs the price list, not the results.
    Roles.BILLING_STAFF: (DiagnosticsPermissions.CATALOGUE_READ,),
    Roles.PHARMACIST: (DiagnosticsPermissions.CATALOGUE_READ,),
    Roles.RECORDS_OFFICER: _READ,
    Roles.AUDITOR: _READ,
}
