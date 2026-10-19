"""The accountant's side of billing: IPD bills, the final bill, the accounts view.

OPD bills are frozen the moment they are paid and belong to the counter. An IPD
stay runs for days, and its bill is the one that goes to the tax filing — so it
gets one owner, the accountant (and the hospital admin, who can do anything),
and nobody else may change it by hand.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError
from app.core.rbac_registry import role_grants
from app.modules.billing import service
from app.modules.billing.models import PaymentMethod
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests import accounts_support as support
from tests.conftest import _make_user

D = support.D

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    await support.price_list(session, hospital.id)
    return hospital


@pytest.fixture
async def admin(session: AsyncSession, tenant: Hospital) -> User:
    user = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def stay(session: AsyncSession, tenant: Hospital, admin: User) -> support.Stay:
    return await support.admitted_stay(session, tenant.id, admin)


# ---------------------------------------------------------------------------
# Which visits are IPD stays — billing asks ipd, through the stay provider
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_stay_for_an_opd_visit_is_none(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    patient = await support.register(session, tenant.id)
    visit = await support.open_visit(session, tenant.id, patient, admin)

    assert await service.stay_for(session, visit.id, hospital_id=tenant.id) is None


@pytest.mark.integration
async def test_stay_for_an_admitted_visit_names_the_admission(
    session: AsyncSession, tenant: Hospital, stay: support.Stay
) -> None:
    info = await service.stay_for(session, stay.encounter.id, hospital_id=tenant.id)

    assert info is not None
    assert info.admission_id == stay.admission.id
    assert info.admission_number == stay.admission.admission_number
    assert info.status == "ADMITTED"
    assert info.discharged_at is None
    assert info.ward_bed is not None and info.ward_bed.startswith("General Ward, GW")


@pytest.mark.integration
async def test_collect_refuses_an_inpatient_stay(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """Reception's one-tap Paid must never issue an IPD bill behind the accountant."""
    with pytest.raises(ConflictError) as refused:
        await service.collect_balance(
            session, stay.encounter, method=PaymentMethod.CASH, actor=admin, hospital_id=tenant.id
        )
    assert refused.value.code == "ipd_bill_by_accounts"


@pytest.mark.integration
async def test_board_shows_no_balance_for_an_admitted_patient(
    session: AsyncSession, tenant: Hospital, admin: User, stay: support.Stay
) -> None:
    """The board's money chip is `balances_for`; a stay is absent, so the
    reception strip hides itself — there is nothing for the counter to take."""
    other = await support.open_visit(
        session, tenant.id, await support.register(session, tenant.id, name="Kavita"), admin
    )

    balances = await service.balances_for(
        session, [stay.encounter.id, other.id], hospital_id=tenant.id
    )

    assert stay.encounter.id not in balances
    assert balances[other.id].balance_due == D("500.00")
