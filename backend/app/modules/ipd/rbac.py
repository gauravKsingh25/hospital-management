"""Permissions enforced by `ipd`.

Three splits carry the weight, and each one matches how a ward actually runs.

**Giving a drug is not prescribing one.** `medication:administer` belongs to
nurses and `medication:prescribe` to doctors, and the service additionally
refuses to let one person do both on the same dose where the roles differ. A
ward where whoever writes the order also signs for it has no second pair of eyes,
and the second pair of eyes is most of what a medication chart is for.

**Moving a patient is not managing capacity.** `bed:assign` puts a patient in a
bed and is held widely — admissions, nursing, the ward clerk. `ward:manage`
creates and retires beds, and is not: a hospital whose bed count changes because
a nurse mistyped something has lost its census.

**Discharging is not writing the summary.** `admission:discharge` is the
administrative act of ending the stay and freeing the bed; `summary:sign` is a
clinical signature with a registration number attached. The first can be done by
the ward clerk once the doctor has done the second.

**Cleaning a bed is somebody's actual job.** `bed:clean` belongs to
`HOUSEKEEPING`, a role added to the seed set for exactly this rung of the bed
lifecycle. Nursing keeps it as well, because a small hospital at 3am has no
housekeeping shift on and a bed that cannot be released until morning is worse
than a nurse marking it clean. What `HOUSEKEEPING` gets is *only* that: it can
see wards and beds and turn them around, and it holds no permission that
touches a patient record. A porter who can read a diagnosis is a data-protection
finding waiting to happen (CLAUDE.md §12).
"""

from __future__ import annotations

from typing import Final

from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "ipd"


class IpdPermissions:
    WARD_READ = "ward:read"
    WARD_MANAGE = "ward:manage"

    BED_READ = "bed:read"
    # Put a patient in a bed, or move them.
    BED_ASSIGN = "bed:assign"
    # Housekeeping: mark a released bed turned around and ready.
    BED_CLEAN = "bed:clean"
    # Take a bed out of circulation, or bring it back.
    BED_BLOCK = "bed:block"

    ADMISSION_READ = "admission:read"
    ADMISSION_CREATE = "admission:create"
    ADMISSION_UPDATE = "admission:update"
    ADMISSION_DISCHARGE = "admission:discharge"
    ADMISSION_CANCEL = "admission:cancel"
    # Send a seen OPD patient to the admission desk.
    ADMISSION_REQUEST = "admission:request"
    # Work the admission desk: the patients waiting to be admitted.
    ADMISSION_DESK = "admission:desk"

    MEDICATION_READ = "medication:read"
    MEDICATION_PRESCRIBE = "medication:prescribe"
    # The nurse at the bedside, signing for a dose.
    MEDICATION_ADMINISTER = "medication:administer"

    SUMMARY_READ = "summary:read"
    SUMMARY_WRITE = "summary:write"
    # The clinical signature. Deliberately not held by whoever can edit the text.
    SUMMARY_SIGN = "summary:sign"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(IpdPermissions.WARD_READ, MODULE, "View wards and the bed board."),
    PermissionDef(IpdPermissions.WARD_MANAGE, MODULE, "Create or edit wards and beds."),
    PermissionDef(IpdPermissions.BED_READ, MODULE, "View bed occupancy."),
    PermissionDef(IpdPermissions.BED_ASSIGN, MODULE, "Assign or transfer a patient's bed."),
    PermissionDef(IpdPermissions.BED_CLEAN, MODULE, "Mark a bed cleaned and ready."),
    PermissionDef(IpdPermissions.BED_BLOCK, MODULE, "Take a bed out of service, or restore it."),
    PermissionDef(IpdPermissions.ADMISSION_READ, MODULE, "View admissions and the ward census."),
    PermissionDef(IpdPermissions.ADMISSION_CREATE, MODULE, "Admit a patient."),
    PermissionDef(IpdPermissions.ADMISSION_UPDATE, MODULE, "Edit admission details."),
    PermissionDef(IpdPermissions.ADMISSION_DISCHARGE, MODULE, "Discharge an inpatient."),
    PermissionDef(IpdPermissions.ADMISSION_CANCEL, MODULE, "Cancel an admission made in error."),
    PermissionDef(
        IpdPermissions.ADMISSION_REQUEST, MODULE, "Send a seen patient to the admission desk."
    ),
    PermissionDef(
        IpdPermissions.ADMISSION_DESK,
        MODULE,
        "Work the admission desk: admit patients sent from OPD, or turn a request away.",
    ),
    PermissionDef(IpdPermissions.MEDICATION_READ, MODULE, "View the medication chart."),
    PermissionDef(IpdPermissions.MEDICATION_PRESCRIBE, MODULE, "Prescribe or stop a drug."),
    PermissionDef(IpdPermissions.MEDICATION_ADMINISTER, MODULE, "Record a dose given or withheld."),
    PermissionDef(IpdPermissions.SUMMARY_READ, MODULE, "Read discharge summaries."),
    PermissionDef(IpdPermissions.SUMMARY_WRITE, MODULE, "Compile or edit a discharge summary."),
    PermissionDef(IpdPermissions.SUMMARY_SIGN, MODULE, "Sign and release a discharge summary."),
)

_ALL = tuple(perm.code for perm in PERMISSION_CATALOGUE)

_READ = (
    IpdPermissions.WARD_READ,
    IpdPermissions.BED_READ,
    IpdPermissions.ADMISSION_READ,
    IpdPermissions.MEDICATION_READ,
    IpdPermissions.SUMMARY_READ,
)

# The ward: everything at the bedside, and nothing that changes what the hospital
# owns or what a document says clinically.
_NURSE = (
    *_READ,
    IpdPermissions.BED_ASSIGN,
    IpdPermissions.BED_CLEAN,
    IpdPermissions.ADMISSION_CREATE,
    IpdPermissions.ADMISSION_UPDATE,
    IpdPermissions.MEDICATION_ADMINISTER,
    IpdPermissions.SUMMARY_WRITE,
)

# The consultant: prescribes, writes up, signs. Does not run the bed board —
# though they can see it, because "is there an ICU bed" changes a decision.
_DOCTOR = (
    *_READ,
    IpdPermissions.ADMISSION_CREATE,
    IpdPermissions.ADMISSION_UPDATE,
    IpdPermissions.ADMISSION_DISCHARGE,
    IpdPermissions.MEDICATION_PRESCRIBE,
    IpdPermissions.SUMMARY_WRITE,
    IpdPermissions.SUMMARY_SIGN,
    # The doctor who advises admission can send the patient to the desk.
    IpdPermissions.ADMISSION_REQUEST,
)

# The front desk admits, and completes the paperwork once a doctor has signed.
_RECEPTION = (
    *_READ,
    IpdPermissions.BED_ASSIGN,
    IpdPermissions.ADMISSION_CREATE,
    IpdPermissions.ADMISSION_UPDATE,
    IpdPermissions.ADMISSION_DISCHARGE,
    # "Send to admission" on the OPD board. Working the desk itself is
    # `admission:desk`, which reception does not hold: the hand-off is between
    # two counters, and a hospital where one person runs both gives them both
    # roles rather than blurring one.
    IpdPermissions.ADMISSION_REQUEST,
)

# The admission counter: the patients sent from OPD, a bed, the paperwork.
# Not discharge, not the medication chart — the stay belongs to the ward once
# the patient is in a bed.
_ADMISSION_DESK = (
    IpdPermissions.WARD_READ,
    IpdPermissions.BED_READ,
    IpdPermissions.BED_ASSIGN,
    IpdPermissions.ADMISSION_READ,
    IpdPermissions.ADMISSION_CREATE,
    IpdPermissions.ADMISSION_UPDATE,
    IpdPermissions.ADMISSION_REQUEST,
    IpdPermissions.ADMISSION_DESK,
)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: _ALL,
    Roles.HOSPITAL_ADMIN: _ALL,
    Roles.NURSE: _NURSE,
    Roles.DOCTOR: _DOCTOR,
    Roles.RECEPTIONIST: _RECEPTION,
    # The counter needs to know the bed class to price the stay, and whether the
    # patient is still in.
    Roles.CASHIER: (
        IpdPermissions.WARD_READ,
        IpdPermissions.BED_READ,
        IpdPermissions.ADMISSION_READ,
    ),
    Roles.BILLING_STAFF: (
        IpdPermissions.WARD_READ,
        IpdPermissions.BED_READ,
        IpdPermissions.ADMISSION_READ,
    ),
    # The pharmacy reads the chart to dispense against it, and does not sign for
    # a dose given at the bedside.
    Roles.PHARMACIST: (
        IpdPermissions.ADMISSION_READ,
        IpdPermissions.MEDICATION_READ,
    ),
    # The lab and radiology need to know which ward to send a report to.
    Roles.LAB_TECH: (IpdPermissions.WARD_READ, IpdPermissions.ADMISSION_READ),
    Roles.PATHOLOGIST: (IpdPermissions.WARD_READ, IpdPermissions.ADMISSION_READ),
    Roles.RADIOLOGIST: (IpdPermissions.WARD_READ, IpdPermissions.ADMISSION_READ),
    # Records officers hold the medical record, including summaries; they record
    # death and LAMA, which end a stay.
    Roles.RECORDS_OFFICER: (
        *_READ,
        IpdPermissions.ADMISSION_DISCHARGE,
        IpdPermissions.SUMMARY_WRITE,
    ),
    # Housekeeping sees beds, not patients. Deliberately not `_READ`: that
    # bundle includes `admission:read` and `summary:read`, and neither has
    # anything to do with turning a bed around.
    Roles.HOUSEKEEPING: (
        IpdPermissions.WARD_READ,
        IpdPermissions.BED_READ,
        IpdPermissions.BED_CLEAN,
    ),
    Roles.ADMISSION_DESK: _ADMISSION_DESK,
    Roles.AUDITOR: _READ,
}


# Which roles may sign a discharge summary. Checked in addition to
# `summary:sign`, never instead of it — the signature carries a registration
# number, and an administrator holding the permission is not a clinician.
SIGNING_ROLES: Final[frozenset[str]] = frozenset(
    {Roles.DOCTOR, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}
)
