"""The accountant's side of billing: IPD bills, the final bill, the accounts view.

OPD bills are frozen the moment they are paid and belong to the counter. An IPD
stay runs for days, and its bill is the one that goes to the tax filing — so it
gets one owner, the accountant (and the hospital admin, who can do anything),
and nobody else may change it by hand.
"""

from __future__ import annotations

from app.core.rbac_registry import role_grants
from app.modules.identity.rbac import Roles

_IPD_POWERS = {"ipd_bill:edit", "ipd_bill:finalise"}


def test_accountant_holds_exactly_the_ipd_billing_powers() -> None:
    grants = role_grants()

    accountant = grants[Roles.ACCOUNTANT]
    assert {
        "accounts:read",
        "ipd_bill:edit",
        "ipd_bill:finalise",
        "charge:read",
        "invoice:read",
        "payment:read",
    } <= accountant
    # Money is taken at the counter, not by the person who corrects the bill.
    assert "payment:record" not in accountant

    assert {"accounts:read", *_IPD_POWERS} <= grants[Roles.HOSPITAL_ADMIN]
    for role in (Roles.CASHIER, Roles.RECEPTIONIST, Roles.BILLING_STAFF, Roles.PHARMACIST):
        assert not (grants.get(role, set()) & _IPD_POWERS), role
