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
from app.modules.billing.schemas import ChargeAdd, InvoiceDraftRequest
from app.modules.identity.models import AuditLog, User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.ipd.models import DischargeType
from app.modules.ipd.schemas import DischargeRequest
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
        self,
        api: AsyncClient,
        session: AsyncSession,
        tenant: Hospital,
        stay: support.Stay,
        admin_token: str,
    ) -> None:
        token = await _token(api, session, tenant, Roles.CASHIER)
        await api.post(
            CHARGES,
            json={"encounter_id": str(stay.encounter.id), "service_item_code": "DRESSING"},
            headers=auth(admin_token),
        )
        draft = await api.post(
            INVOICES, json={"encounter_id": str(stay.encounter.id)}, headers=auth(admin_token)
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


# ---------------------------------------------------------------------------
# The generic invoice routes on a stay (final review #1, #3)
# ---------------------------------------------------------------------------
class TestGenericRoutesOnAStay:
    async def test_admin_cannot_issue_the_ipd_bill_before_discharge(
        self, api: AsyncClient, stay: support.Stay, admin_token: str
    ) -> None:
        """Issuing a stay's IPD draft is generating its final bill, with the
        final bill's rules: after discharge, priced, once."""
        await api.post(
            CHARGES,
            json={"encounter_id": str(stay.encounter.id), "service_item_code": "DRESSING"},
            headers=auth(admin_token),
        )
        draft = await api.post(
            INVOICES, json={"encounter_id": str(stay.encounter.id)}, headers=auth(admin_token)
        )
        assert draft.status_code == 201, draft.text
        assert draft.json()["scope"] == "IPD"

        response = await api.post(
            f"{INVOICES}/{draft.json()['id']}/issue", json={}, headers=auth(admin_token)
        )

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "stay_not_discharged"

    async def test_billing_staff_cannot_assemble_a_stay_bill(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
    ) -> None:
        token = await _token(api, session, tenant, Roles.BILLING_STAFF)

        response = await api.post(
            INVOICES, json={"encounter_id": str(stay.encounter.id)}, headers=auth(token)
        )

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "ipd_bill_accounts_only"

    async def test_billing_staff_correct_and_reissue_the_pre_admission_bill(
        self, api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
    ) -> None:
        """The consultation before the admission is an OPD bill: the counter's,
        cancelled, discounted and issued again like any other."""
        token = await _token(api, session, tenant, Roles.BILLING_STAFF)
        [opd] = (
            await api.get(
                INVOICES, params={"encounter_id": str(stay.encounter.id)}, headers=auth(token)
            )
        ).json()["items"]

        cancelled = await api.post(
            f"{INVOICES}/{opd['id']}/cancel", json={"reason": "Wrong fee"}, headers=auth(token)
        )
        assert cancelled.status_code == 200, cancelled.text
        account = (await api.get(f"{ACCOUNTS}/{stay.encounter.id}", headers=auth(token))).json()
        assert (account["is_stay"], account["pre_admission_pending"]) == (True, True)
        charge_id = await _pending_charge(session, tenant, stay.encounter.id)
        discounted = await api.post(
            f"{CHARGES}/{charge_id}/discount",
            json={"amount": "100.00", "reason": "Senior citizen"},
            headers=auth(token),
        )
        assert discounted.status_code == 200, discounted.text

        reissued = await api.post(
            f"{ACCOUNTS}/{stay.encounter.id}/pre-admission-bill", headers=auth(token)
        )

        assert reissued.status_code == 201, reissued.text
        body = reissued.json()
        assert (body["scope"], body["status"], body["grand_total"]) == ("OPD", "ISSUED", "400.00")


async def test_the_printed_bill_carries_every_line(
    api: AsyncClient, session: AsyncSession, tenant: Hospital, admin_user: User, admin_token: str
) -> None:
    """Final review #5: a long stay has hundreds of lines; none may drop off paper."""
    patient = await support.register(session, tenant.id, name="Long Stay")
    visit = await support.open_visit(session, tenant.id, patient, admin_user)
    for _ in range(210):
        await billing_service.add_charge(
            session,
            ChargeAdd(encounter_id=visit.id, service_item_code="DRESSING"),
            hospital_id=tenant.id,
            actor=admin_user,
        )
    invoice = await billing_service.assemble_draft(
        session, InvoiceDraftRequest(encounter_id=visit.id), hospital_id=tenant.id
    )
    await session.commit()

    printed = await api.get(f"{INVOICES}/{invoice.id}/printable", headers=auth(admin_token))

    assert printed.status_code == 200, printed.text
    assert len(printed.json()["lines"]) == 211


async def test_the_accountant_cancels_a_final_bill_to_correct_it(
    api: AsyncClient,
    session: AsyncSession,
    tenant: Hospital,
    stay: support.Stay,
    admin_user: User,
) -> None:
    """Final review #6: correction is cancel (with a reason), edit, generate
    again — and the accountant, who holds no `invoice:cancel`, can do it."""
    token = await _token(api, session, tenant, Roles.ACCOUNTANT)
    await ipd_service.discharge_patient(
        session,
        stay.admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=admin_user,
    )
    await session.commit()
    final = await api.post(f"{ACCOUNTS}/{stay.encounter.id}/final-bill", headers=auth(token))
    assert final.status_code == 201, final.text

    cancelled = await api.post(
        f"{ACCOUNTS}/{stay.encounter.id}/final-bill/cancel",
        json={"reason": "Room charged twice"},
        headers=auth(token),
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    bill = (await api.get(f"{ACCOUNTS}/{stay.encounter.id}/ipd-bill", headers=auth(token))).json()
    assert bill["final_invoice"] is None
    assert bill["can_finalise"] is True
    audit = (
        await session.execute(
            sa.select(AuditLog).where(
                AuditLog.action == "billing.ipd_bill.cancel",
                AuditLog.resource_id == uuid.UUID(final.json()["id"]),
            )
        )
    ).scalar_one()
    assert audit.changes["reason"] == "Room charged twice"


async def test_cancelling_with_no_final_bill_is_refused(
    api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
) -> None:
    token = await _token(api, session, tenant, Roles.ACCOUNTANT)

    response = await api.post(
        f"{ACCOUNTS}/{stay.encounter.id}/final-bill/cancel",
        json={"reason": "Nothing to cancel"},
        headers=auth(token),
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "no_final_bill"
