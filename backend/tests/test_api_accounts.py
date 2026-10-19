"""The accountant's routes over HTTP, and who may change an IPD bill.

An IPD stay's bill belongs to the accountant and the hospital admin. The old
manual routes — add, discount, waive, issue, cancel, write off — still work on
OPD visits for the people who held them, and refuse on a stay unless the caller
also holds the IPD power.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.pagination import PageParams
from app.modules.billing import service as billing_service
from app.modules.billing.models import ChargeStatus
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests import accounts_support as support
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

CHARGES = "/api/v1/billing/charges"
INVOICES = "/api/v1/billing/invoices"
ACCOUNTS = "/api/v1/billing/accounts"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    await support.price_list(session, hospital.id)
    return hospital


async def _user(session: AsyncSession, tenant: Hospital, role: str) -> User:
    user = await _make_user(session, role_code=role, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


async def _token(api: AsyncClient, session: AsyncSession, tenant: Hospital, role: str) -> str:
    return await login(api, await _user(session, tenant, role))


@pytest.fixture
async def admin_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.HOSPITAL_ADMIN)


@pytest.fixture
async def admin_token(api: AsyncClient, admin_user: User) -> str:
    return await login(api, admin_user)


@pytest.fixture
async def stay(session: AsyncSession, tenant: Hospital, admin_user: User) -> support.Stay:
    return await support.admitted_stay(session, tenant.id, admin_user)


async def _pending_charge(session: AsyncSession, tenant: Hospital, encounter_id: uuid.UUID) -> str:
    charges, _ = await billing_service.list_charges(
        session,
        PageParams(),
        hospital_id=tenant.id,
        encounter_id=encounter_id,
        status=ChargeStatus.PENDING,
    )
    assert charges, "expected a pending charge"
    return str(charges[-1].id)


# ---------------------------------------------------------------------------
# The old manual routes, on a stay
# ---------------------------------------------------------------------------
class TestOnlyAccountsChangeAnIpdBill:
    async def test_billing_staff_cannot_discount_an_ipd_charge(
        self,
        api: AsyncClient,
        session: AsyncSession,
        tenant: Hospital,
        stay: support.Stay,
        admin_token: str,
    ) -> None:
        added = await api.post(
            CHARGES,
            json={"encounter_id": str(stay.encounter.id), "service_item_code": "DRESSING"},
            headers=auth(admin_token),
        )
        assert added.status_code == 201, added.text
        token = await _token(api, session, tenant, Roles.BILLING_STAFF)

        response = await api.post(
            f"{CHARGES}/{added.json()['id']}/discount",
            json={"amount": "10.00", "reason": "Goodwill"},
            headers=auth(token),
        )

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "ipd_bill_accounts_only"

    async def test_billing_staff_can_still_discount_an_opd_charge(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, admin_user: User
    ) -> None:
        patient = await support.register(session, tenant.id, name="Kavita Sharma")
        visit = await support.open_visit(session, tenant.id, patient, admin_user)
        charge_id = await _pending_charge(session, tenant, visit.id)
        token = await _token(api, session, tenant, Roles.BILLING_STAFF)

        response = await api.post(
            f"{CHARGES}/{charge_id}/discount",
            json={"amount": "50.00", "reason": "Staff family"},
            headers=auth(token),
        )

        assert response.status_code == 200, response.text

    async def test_cashier_cannot_issue_an_ipd_invoice(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
    ) -> None:
        token = await _token(api, session, tenant, Roles.CASHIER)
        draft = await api.post(
            INVOICES, json={"encounter_id": str(stay.encounter.id)}, headers=auth(token)
        )
        assert draft.status_code == 201, draft.text

        response = await api.post(
            f"{INVOICES}/{draft.json()['id']}/issue", json={}, headers=auth(token)
        )

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "ipd_bill_accounts_only"

    async def test_the_admin_may_still_change_an_ipd_charge(
        self,
        api: AsyncClient,
        session: AsyncSession,
        stay: support.Stay,
        admin_token: str,
        tenant: Hospital,
    ) -> None:
        charge_id = await _pending_charge(session, tenant, stay.encounter.id)

        response = await api.post(
            f"{CHARGES}/{charge_id}/waive",
            json={"reason": "Charity case"},
            headers=auth(admin_token),
        )

        assert response.status_code == 200, response.text
