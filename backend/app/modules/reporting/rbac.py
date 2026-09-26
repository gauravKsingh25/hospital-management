"""Permissions enforced by `reporting`.

Three permissions, not one, and the split is the whole design.

A single `report:read` would have been simpler and wrong. The three kinds of
number this module produces have genuinely different audiences:

* **operational** — footfall, bed occupancy, queue length. Nearly everyone
  needs these; a ward sister deciding whether to open a side room and a
  receptionist telling a patient how long the wait is are both asking
  operational questions.
* **clinical** — follow-up compliance, discharge outcomes, length of stay by
  diagnosis. Clinicians and records officers.
* **revenue** — collections, outstanding balances, what was waived. A much
  smaller circle.

Rolling them together would mean that giving a nurse the queue screen also
handed her the hospital's monthly collections. It is the same reasoning
`billing` uses to separate taking money from reducing it: the finer split costs
one extra row in a seed table and removes a whole class of "why can they see
that?" conversation.

Note there is no `report:write` of any kind. This module is read-only by
construction — it owns no tables, and every endpoint is a `GET`.
"""

from __future__ import annotations

from typing import Final

from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "reporting"


class ReportingPermissions:
    # Volumes, occupancy, queue — the numbers that describe how the day is going.
    OPERATIONAL = "report:operational"
    # Outcomes and follow-up — the numbers that describe how care went.
    CLINICAL = "report:clinical"
    # Money.
    REVENUE = "report:revenue"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(
        ReportingPermissions.OPERATIONAL,
        MODULE,
        "Patient volumes, occupancy and queue analytics.",
    ),
    PermissionDef(ReportingPermissions.CLINICAL, MODULE, "Clinical outcome and follow-up metrics."),
    PermissionDef(
        ReportingPermissions.REVENUE, MODULE, "Revenue, collections and outstanding balances."
    ),
)

_ALL = tuple(perm.code for perm in PERMISSION_CATALOGUE)


MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: _ALL,
    Roles.HOSPITAL_ADMIN: _ALL,
    # CLAUDE.md §8: read-only access to audit logs *and reports*. All three.
    Roles.AUDITOR: _ALL,
    # A consultant asks how many patients they saw and how many came back.
    Roles.DOCTOR: (ReportingPermissions.OPERATIONAL, ReportingPermissions.CLINICAL),
    # The ward: how full are we, how long is the wait.
    Roles.NURSE: (ReportingPermissions.OPERATIONAL,),
    Roles.RECEPTIONIST: (ReportingPermissions.OPERATIONAL,),
    # The counter reconciles its own day.
    Roles.CASHIER: (ReportingPermissions.OPERATIONAL, ReportingPermissions.REVENUE),
    Roles.BILLING_STAFF: (ReportingPermissions.OPERATIONAL, ReportingPermissions.REVENUE),
    # Records officers own follow-up compliance and outcome statistics.
    Roles.RECORDS_OFFICER: (ReportingPermissions.OPERATIONAL, ReportingPermissions.CLINICAL),
}
