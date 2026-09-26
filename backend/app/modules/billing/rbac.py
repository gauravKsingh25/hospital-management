"""Permissions enforced by `billing`.

Three separations matter more here than anywhere else in the system, because
this is the module where a permission mistake becomes theft rather than
inconvenience.

**Taking money is not adjusting money.** `payment:record` lets a cashier accept
what is owed. `charge:waive`, `invoice:write_off` and `payment:reverse` let
somebody decide that less is owed — which is the same lever a person would pull
to pocket the difference. A cashier gets the first and not the others.

**Setting prices is not using them.** `ratecard:manage` changes what the hospital
charges everybody. That belongs with administration, not with the counter.

**Reading a bill is not reading a chart.** Finance roles get invoices and charges
and, from `clinical`, nothing but the fact that a visit exists and what was
ordered on it. The examination note is not theirs.
"""

from __future__ import annotations

from typing import Final

from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "billing"


class BillingPermissions:
    # --- the price list ---------------------------------------------------
    RATECARD_READ = "ratecard:read"
    RATECARD_MANAGE = "ratecard:manage"
    SERVICE_READ = "service:read"
    SERVICE_MANAGE = "service:manage"

    # --- the running bill -------------------------------------------------
    CHARGE_READ = "charge:read"
    # Adding a line by hand. Capture is automatic; this is for what the events
    # cannot see — a dressing, a consumable, an ambulance.
    CHARGE_ADD = "charge:add"
    # Deciding money is not owed. Separated from every other charge action.
    CHARGE_WAIVE = "charge:waive"
    CHARGE_DISCOUNT = "charge:discount"

    # --- the document -----------------------------------------------------
    INVOICE_READ = "invoice:read"
    INVOICE_CREATE = "invoice:create"
    # One-way door: after this the lines are frozen.
    INVOICE_ISSUE = "invoice:issue"
    INVOICE_CANCEL = "invoice:cancel"
    INVOICE_WRITE_OFF = "invoice:write_off"

    # --- money ------------------------------------------------------------
    PAYMENT_READ = "payment:read"
    PAYMENT_RECORD = "payment:record"
    # Giving money back. Never bundled with taking it.
    PAYMENT_REVERSE = "payment:reverse"

    # --- claims -----------------------------------------------------------
    CLAIM_READ = "claim:read"
    CLAIM_MANAGE = "claim:manage"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(BillingPermissions.RATECARD_READ, MODULE, "View rate cards and prices."),
    PermissionDef(BillingPermissions.RATECARD_MANAGE, MODULE, "Create or edit rate cards."),
    PermissionDef(BillingPermissions.SERVICE_READ, MODULE, "View the billable service list."),
    PermissionDef(BillingPermissions.SERVICE_MANAGE, MODULE, "Edit billable services."),
    PermissionDef(BillingPermissions.CHARGE_READ, MODULE, "View charges on a visit."),
    PermissionDef(BillingPermissions.CHARGE_ADD, MODULE, "Add a charge by hand."),
    PermissionDef(BillingPermissions.CHARGE_WAIVE, MODULE, "Waive a charge."),
    PermissionDef(BillingPermissions.CHARGE_DISCOUNT, MODULE, "Discount a charge."),
    PermissionDef(BillingPermissions.INVOICE_READ, MODULE, "View invoices."),
    PermissionDef(BillingPermissions.INVOICE_CREATE, MODULE, "Assemble a draft invoice."),
    PermissionDef(BillingPermissions.INVOICE_ISSUE, MODULE, "Issue an invoice to a patient."),
    PermissionDef(BillingPermissions.INVOICE_CANCEL, MODULE, "Cancel an invoice."),
    PermissionDef(BillingPermissions.INVOICE_WRITE_OFF, MODULE, "Write off an unpaid invoice."),
    PermissionDef(BillingPermissions.PAYMENT_READ, MODULE, "View payments and receipts."),
    PermissionDef(BillingPermissions.PAYMENT_RECORD, MODULE, "Record a payment."),
    PermissionDef(BillingPermissions.PAYMENT_REVERSE, MODULE, "Reverse a payment."),
    PermissionDef(BillingPermissions.CLAIM_READ, MODULE, "View insurance and scheme claims."),
    PermissionDef(BillingPermissions.CLAIM_MANAGE, MODULE, "Raise and progress a claim."),
)

_ALL = tuple(perm.code for perm in PERMISSION_CATALOGUE)

_READ = (
    BillingPermissions.RATECARD_READ,
    BillingPermissions.SERVICE_READ,
    BillingPermissions.CHARGE_READ,
    BillingPermissions.INVOICE_READ,
    BillingPermissions.PAYMENT_READ,
)

# The counter. Everything needed to turn a finished visit into a paid one, and
# nothing that reduces what is owed. A cashier who could waive a charge and take
# the cash is a cashier the hospital cannot audit.
_CASHIER = (
    *_READ,
    BillingPermissions.CHARGE_ADD,
    BillingPermissions.INVOICE_CREATE,
    BillingPermissions.INVOICE_ISSUE,
    BillingPermissions.PAYMENT_RECORD,
)

# Billing staff run the back office: claims, concessions, corrections. They
# reverse payments and write off bad debt; they do not set the price list.
_BILLING_STAFF = (
    *_CASHIER,
    BillingPermissions.CHARGE_DISCOUNT,
    BillingPermissions.CHARGE_WAIVE,
    BillingPermissions.INVOICE_CANCEL,
    BillingPermissions.INVOICE_WRITE_OFF,
    BillingPermissions.PAYMENT_REVERSE,
    BillingPermissions.CLAIM_READ,
    BillingPermissions.CLAIM_MANAGE,
)

# Reception doubles as the cash counter in most mid-size hospitals — CLAUDE.md
# §8 says as much ("can be merged with receptionist"). Same powers as a cashier,
# for the same reason.
_RECEPTION = _CASHIER

# A doctor should be able to see whether the patient in front of them has an
# unpaid bill — it changes the conversation — without being able to alter it.
_DOCTOR = (BillingPermissions.CHARGE_READ, BillingPermissions.INVOICE_READ)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: _ALL,
    Roles.HOSPITAL_ADMIN: _ALL,
    Roles.CASHIER: _CASHIER,
    Roles.BILLING_STAFF: _BILLING_STAFF,
    Roles.RECEPTIONIST: _RECEPTION,
    Roles.DOCTOR: _DOCTOR,
    Roles.NURSE: (BillingPermissions.CHARGE_READ,),
    # The pharmacy counter takes money for what it dispenses.
    Roles.PHARMACIST: (
        *_READ,
        BillingPermissions.CHARGE_ADD,
        BillingPermissions.PAYMENT_RECORD,
    ),
    # The lab quotes a price at the window; it does not collect.
    Roles.LAB_TECH: (BillingPermissions.SERVICE_READ, BillingPermissions.RATECARD_READ),
    Roles.PATHOLOGIST: (BillingPermissions.SERVICE_READ, BillingPermissions.RATECARD_READ),
    Roles.RADIOLOGIST: (BillingPermissions.SERVICE_READ, BillingPermissions.RATECARD_READ),
    Roles.RECORDS_OFFICER: (BillingPermissions.INVOICE_READ, BillingPermissions.CHARGE_READ),
    Roles.AUDITOR: (*_READ, BillingPermissions.CLAIM_READ),
}
