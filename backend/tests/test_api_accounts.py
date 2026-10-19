"""The accountant's routes over HTTP, and who may change an IPD bill.

An IPD stay's bill belongs to the accountant and the hospital admin. The old
manual routes — add, discount, waive, issue, cancel, write off — still work on
OPD visits for the people who held them, and refuse on a stay unless the caller
also holds the IPD power.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.pagination import PageParams
from app.modules.billing import service as billing_service
from app.modules.billing.models import ChargeStatus
from app.modules.identity.models import AuditLog, User
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
        added = await api.post(
            CHARGES,
            json={"encounter_id": str(stay.encounter.id), "service_item_code": "DRESSING"},
            headers=auth(admin_token),
        )
        assert added.status_code == 201, added.text
        charge_id = added.json()["id"]

        response = await api.post(
            f"{CHARGES}/{charge_id}/waive",
            json={"reason": "Charity case"},
            headers=auth(admin_token),
        )

        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The editor's routes
# ---------------------------------------------------------------------------
class TestIpdBillEditor:
    async def test_every_change_is_audited_with_before_and_after(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
    ) -> None:
        token = await _token(api, session, tenant, Roles.ACCOUNTANT)
        url = f"{ACCOUNTS}/{stay.encounter.id}/ipd-bill"

        loaded = await api.get(url, headers=auth(token))
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["stay"]["admission_number"] == stay.admission.admission_number

        added = await api.post(
            f"{url}/changes",
            json={
                "expected_version": loaded.json()["version"],
                "ops": [{"op": "add", "service_item_code": "DRESSING", "reason": "Ward use"}],
            },
            headers=auth(token),
        )
        assert added.status_code == 200, added.text
        [dressing] = added.json()["charges"]

        edited = await api.post(
            f"{url}/changes",
            json={
                "expected_version": added.json()["version"],
                "ops": [
                    {
                        "op": "edit",
                        "charge_id": dressing["id"],
                        "quantity": "2",
                        "reason": "Two dressings",
                    }
                ],
            },
            headers=auth(token),
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["total"] == "537.60"

        async with db.system_context(session):
            rows = (
                (
                    await session.execute(
                        sa.select(AuditLog)
                        .where(AuditLog.action == "billing.ipd_bill.edit")
                        .order_by(AuditLog.created_at)
                    )
                )
                .scalars()
                .all()
            )
        assert [row.changes["op"] for row in rows] == ["add", "edit"]
        edit = rows[-1].changes
        assert edit["reason"] == "Two dressings"
        assert edit["before"]["quantity"] == "1.00"
        assert edit["after"]["quantity"] == "2.00"
        assert edit["after"]["total_amount"] == "537.60"

    async def test_cashier_cannot_save_changes(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
    ) -> None:
        token = await _token(api, session, tenant, Roles.CASHIER)

        response = await api.post(
            f"{ACCOUNTS}/{stay.encounter.id}/ipd-bill/changes",
            json={
                "expected_version": "x",
                "ops": [{"op": "add", "service_item_code": "DRESSING", "reason": "Ward use"}],
            },
            headers=auth(token),
        )

        assert response.status_code == 403, response.text

    async def test_a_stale_save_is_a_conflict(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
    ) -> None:
        token = await _token(api, session, tenant, Roles.ACCOUNTANT)

        response = await api.post(
            f"{ACCOUNTS}/{stay.encounter.id}/ipd-bill/changes",
            json={
                "expected_version": "stale",
                "ops": [{"op": "add", "service_item_code": "DRESSING", "reason": "Ward use"}],
            },
            headers=auth(token),
        )

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "bill_changed"
