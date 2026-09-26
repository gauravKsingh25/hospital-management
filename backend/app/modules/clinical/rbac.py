"""Permissions enforced by `clinical`.

Two things are deliberately separated here.

**Reading a visit is not reading a chart.** A cashier needs the encounter to
raise an invoice and has no business in the examination note, so
`encounter:read` and `note:read` are different permissions rather than one
convenient bundle.

**Fulfilling an order is gated twice.** The permission says a user may clear
orders at all; `FULFILMENT_ROLES` below says *which kind*. Without the second
check, `order:fulfil` would let a cashier mark a blood test resulted, which is
the sort of thing that is obvious in hindsight and invisible in a permission
matrix.
"""

from __future__ import annotations

from typing import Final

from app.modules.clinical.models import OrderType
from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "clinical"


class ClinicalPermissions:
    ENCOUNTER_READ = "encounter:read"
    ENCOUNTER_CREATE = "encounter:create"
    ENCOUNTER_UPDATE = "encounter:update"
    ENCOUNTER_ADMIT = "encounter:admit"
    ENCOUNTER_CANCEL = "encounter:cancel"
    # The doctor's "I am finished" action, and the staff clearance that follows.
    ENCOUNTER_COMPLETE = "encounter:complete"

    # CLAUDE.md §8 gives RECORDS_OFFICER these explicitly. Split into three
    # because a hospital may well want the death register in fewer hands than
    # the LAMA register.
    ENCOUNTER_RECORD_DEATH = "encounter:record_death"
    ENCOUNTER_RECORD_REFERRAL = "encounter:record_referral"
    ENCOUNTER_RECORD_LAMA = "encounter:record_lama"

    VITALS_READ = "vitals:read"
    VITALS_RECORD = "vitals:record"

    NOTE_READ = "note:read"
    NOTE_WRITE = "note:write"
    NOTE_SIGN = "note:sign"

    DIAGNOSIS_READ = "diagnosis:read"
    DIAGNOSIS_RECORD = "diagnosis:record"

    ORDER_READ = "order:read"
    ORDER_PLACE = "order:place"
    ORDER_FULFIL = "order:fulfil"
    ORDER_CANCEL = "order:cancel"

    TEMPLATE_READ = "template:read"
    TEMPLATE_MANAGE = "template:manage"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(ClinicalPermissions.ENCOUNTER_READ, MODULE, "View a visit and its status."),
    PermissionDef(ClinicalPermissions.ENCOUNTER_CREATE, MODULE, "Open a visit."),
    PermissionDef(ClinicalPermissions.ENCOUNTER_UPDATE, MODULE, "Edit visit details."),
    PermissionDef(ClinicalPermissions.ENCOUNTER_ADMIT, MODULE, "Convert a visit to admission."),
    PermissionDef(ClinicalPermissions.ENCOUNTER_CANCEL, MODULE, "Cancel or mark a visit no-show."),
    PermissionDef(
        ClinicalPermissions.ENCOUNTER_COMPLETE, MODULE, "Complete a consultation or close a visit."
    ),
    PermissionDef(ClinicalPermissions.ENCOUNTER_RECORD_DEATH, MODULE, "Record a patient death."),
    PermissionDef(
        ClinicalPermissions.ENCOUNTER_RECORD_REFERRAL,
        MODULE,
        "Record a referral to another facility.",
    ),
    PermissionDef(
        ClinicalPermissions.ENCOUNTER_RECORD_LAMA, MODULE, "Record leaving against medical advice."
    ),
    PermissionDef(ClinicalPermissions.VITALS_READ, MODULE, "View recorded vitals."),
    PermissionDef(ClinicalPermissions.VITALS_RECORD, MODULE, "Record vitals."),
    PermissionDef(ClinicalPermissions.NOTE_READ, MODULE, "Read clinical notes."),
    PermissionDef(ClinicalPermissions.NOTE_WRITE, MODULE, "Write clinical notes."),
    PermissionDef(ClinicalPermissions.NOTE_SIGN, MODULE, "e-Sign a clinical note."),
    PermissionDef(ClinicalPermissions.DIAGNOSIS_READ, MODULE, "View diagnoses."),
    PermissionDef(ClinicalPermissions.DIAGNOSIS_RECORD, MODULE, "Record or code a diagnosis."),
    PermissionDef(ClinicalPermissions.ORDER_READ, MODULE, "View orders."),
    PermissionDef(ClinicalPermissions.ORDER_PLACE, MODULE, "Place a clinical order."),
    PermissionDef(ClinicalPermissions.ORDER_FULFIL, MODULE, "Start or complete an order."),
    PermissionDef(ClinicalPermissions.ORDER_CANCEL, MODULE, "Cancel an order."),
    PermissionDef(ClinicalPermissions.TEMPLATE_READ, MODULE, "Use note templates."),
    PermissionDef(ClinicalPermissions.TEMPLATE_MANAGE, MODULE, "Create or edit note templates."),
)


# Which roles may clear which kind of order. Checked in addition to
# `order:fulfil`, never instead of it.
FULFILMENT_ROLES: Final[dict[OrderType, frozenset[str]]] = {
    OrderType.LAB: frozenset({Roles.LAB_TECH, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}),
    OrderType.RADIOLOGY: frozenset(
        {Roles.RADIOLOGIST, Roles.LAB_TECH, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}
    ),
    OrderType.PHARMACY: frozenset({Roles.PHARMACIST, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}),
    # Performed in the room, by the people who were in it.
    OrderType.PROCEDURE: frozenset(
        {Roles.DOCTOR, Roles.NURSE, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}
    ),
    OrderType.REFERRAL: frozenset(
        {Roles.DOCTOR, Roles.RECORDS_OFFICER, Roles.HOSPITAL_ADMIN, Roles.PLATFORM_ADMIN}
    ),
}


_ALL = tuple(perm.code for perm in PERMISSION_CATALOGUE)

_CHART_READ = (
    ClinicalPermissions.ENCOUNTER_READ,
    ClinicalPermissions.VITALS_READ,
    ClinicalPermissions.NOTE_READ,
    ClinicalPermissions.DIAGNOSIS_READ,
    ClinicalPermissions.ORDER_READ,
)

# CLAUDE.md §7: the doctor's surface is the smallest it can be — clinical acts
# only. No registration, no counter work, no cancelling other people's visits.
_DOCTOR = (
    *_CHART_READ,
    ClinicalPermissions.ENCOUNTER_UPDATE,
    ClinicalPermissions.ENCOUNTER_ADMIT,
    ClinicalPermissions.ENCOUNTER_COMPLETE,
    ClinicalPermissions.ENCOUNTER_RECORD_DEATH,
    ClinicalPermissions.ENCOUNTER_RECORD_REFERRAL,
    ClinicalPermissions.ENCOUNTER_RECORD_LAMA,
    ClinicalPermissions.VITALS_RECORD,
    ClinicalPermissions.NOTE_WRITE,
    ClinicalPermissions.NOTE_SIGN,
    ClinicalPermissions.DIAGNOSIS_RECORD,
    ClinicalPermissions.ORDER_PLACE,
    # A procedure done in the room is cleared by whoever was in it, and a
    # referral by whoever wrote it. Without this the doctor can order the thing
    # and then cannot mark it done, so the visit sits in PENDING_CLEARANCE until
    # an administrator notices — which is the exact "why won't this close?"
    # failure the state machine exists to prevent. `FULFILMENT_ROLES` still
    # decides *which kind* of order; this only makes that mapping reachable.
    ClinicalPermissions.ORDER_FULFIL,
    ClinicalPermissions.ORDER_CANCEL,
    ClinicalPermissions.TEMPLATE_READ,
    ClinicalPermissions.TEMPLATE_MANAGE,
)

# Nurses open casualty visits, take vitals and write nursing notes. They sign
# their own notes — the service refuses anyone signing someone else's.
_NURSE = (
    *_CHART_READ,
    ClinicalPermissions.ENCOUNTER_CREATE,
    ClinicalPermissions.ENCOUNTER_UPDATE,
    ClinicalPermissions.VITALS_RECORD,
    ClinicalPermissions.NOTE_WRITE,
    ClinicalPermissions.NOTE_SIGN,
    # Nurses perform half the procedures on a ward. Same reasoning as the doctor.
    ClinicalPermissions.ORDER_FULFIL,
    ClinicalPermissions.TEMPLATE_READ,
)

# Reception runs the front of house and never sees the chart.
_RECEPTION = (
    ClinicalPermissions.ENCOUNTER_READ,
    ClinicalPermissions.ENCOUNTER_CREATE,
    ClinicalPermissions.ENCOUNTER_UPDATE,
    ClinicalPermissions.ENCOUNTER_CANCEL,
    ClinicalPermissions.ENCOUNTER_COMPLETE,
    ClinicalPermissions.ORDER_READ,
)

# Records staff own the death, referral and LAMA registers, and assign the ICD
# codes the doctor did not stop to look up.
_RECORDS = (
    *_CHART_READ,
    ClinicalPermissions.ENCOUNTER_RECORD_DEATH,
    ClinicalPermissions.ENCOUNTER_RECORD_REFERRAL,
    ClinicalPermissions.ENCOUNTER_RECORD_LAMA,
    ClinicalPermissions.DIAGNOSIS_RECORD,
    # Records staff close out referrals — `FULFILMENT_ROLES` names them for
    # REFERRAL orders, and this is what makes that name mean something.
    ClinicalPermissions.ORDER_FULFIL,
)

# Money-side roles see that a visit exists and what was ordered on it, never the
# clinical narrative. Their own clearance step arrives with `billing` (Phase 7).
_FINANCE = (ClinicalPermissions.ENCOUNTER_READ, ClinicalPermissions.ORDER_READ)

_TECH = (
    ClinicalPermissions.ENCOUNTER_READ,
    ClinicalPermissions.ORDER_READ,
    ClinicalPermissions.ORDER_FULFIL,
)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: _ALL,
    Roles.HOSPITAL_ADMIN: _ALL,
    Roles.DOCTOR: _DOCTOR,
    Roles.NURSE: _NURSE,
    Roles.RECEPTIONIST: _RECEPTION,
    Roles.RECORDS_OFFICER: _RECORDS,
    Roles.CASHIER: _FINANCE,
    Roles.BILLING_STAFF: _FINANCE,
    Roles.LAB_TECH: _TECH,
    Roles.PATHOLOGIST: (
        *_TECH,
        ClinicalPermissions.NOTE_READ,
        ClinicalPermissions.DIAGNOSIS_READ,
    ),
    Roles.RADIOLOGIST: (*_TECH, ClinicalPermissions.NOTE_READ, ClinicalPermissions.DIAGNOSIS_READ),
    # Dispensing safely means seeing why the drug was prescribed — and it is
    # what the Schedule H/H1/X validation in Phase 9 will read.
    Roles.PHARMACIST: (*_TECH, ClinicalPermissions.DIAGNOSIS_READ),
    # The visit being admitted and what it was for — not the notes.
    Roles.ADMISSION_DESK: (
        ClinicalPermissions.ENCOUNTER_READ,
        ClinicalPermissions.DIAGNOSIS_READ,
    ),
    Roles.AUDITOR: _CHART_READ,
}
