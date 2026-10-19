"""The accounts dashboard, bill history, cost journey and print — all read only.

Newest first everywhere: the accountant's question is "what happened today",
not "what is oldest". Outstanding counts issued bills only; a patient still in
a bed has a running bill, which is shown as "running in wards" and never
inflates what the hospital is owed.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.billing import service as billing_service
from app.modules.billing.models import PaymentMethod
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.ipd.models import DischargeType
from app.modules.ipd.schemas import DischargeRequest
from app.modules.tenancy.models import Hospital
from tests import accounts_support as support
from tests.conftest import _make_user, auth
from tests.test_api_accounts import ACCOUNTS, INVOICES, _token

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/billing/patients"

_DRESSING = {"op": "add", "service_item_code": "DRESSING", "reason": "Ward use"}
_OXYGEN = {
    "op": "add",
    "description": "Oxygen cylinder",
    "category": "OTHER",
    "unit_price": "1000.00",
    "reason": "Ward use",
}


async def _collected(
    session: AsyncSession, tenant: Hospital, actor: User, name: str, method: PaymentMethod
) -> uuid.UUID:
    """An OPD visit whose ₹500 fee was taken at the counter."""
    visit = await support.open_visit(
        session, tenant.id, await support.register(session, tenant.id, name=name), actor
    )
    await billing_service.collect_balance(
        session,
        visit,
        method=method,
        reference="UTR123456" if method is PaymentMethod.UPI else None,
        actor=actor,
        hospital_id=tenant.id,
    )
    await session.commit()
    return visit.id


async def _add_on_stay(api: AsyncClient, token: str, stay: support.Stay, *ops: dict) -> dict:
    url = f"{ACCOUNTS}/{stay.encounter.id}/ipd-bill"
    loaded = await api.get(url, headers=auth(token))
    saved = await api.post(
        f"{url}/changes",
        json={"expected_version": loaded.json()["version"], "ops": list(ops)},
        headers=auth(token),
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    await support.price_list(session, hospital.id)
    return hospital


@pytest.fixture
async def admin_user(session: AsyncSession, tenant: Hospital) -> User:
    user = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def stay(session: AsyncSession, tenant: Hospital, admin_user: User) -> support.Stay:
    return await support.admitted_stay(session, tenant.id, admin_user)


@pytest.fixture
async def accountant(api: AsyncClient, session: AsyncSession, tenant: Hospital) -> str:
    return await _token(api, session, tenant, Roles.ACCOUNTANT)


async def test_summary_splits_collections_by_method(
    api: AsyncClient,
    session: AsyncSession,
    tenant: Hospital,
    admin_user: User,
    accountant: str,
) -> None:
    await _collected(session, tenant, admin_user, "Sunita Devi", PaymentMethod.CASH)
    await _collected(session, tenant, admin_user, "Kavita Sharma", PaymentMethod.UPI)

    summary = await api.get(f"{ACCOUNTS}/summary", headers=auth(accountant))

    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["collected"] == "1000.00"
    assert body["collected_by_method"] == {"CASH": "500.00", "UPI": "500.00"}
    assert body["bills_count"] == 2
    assert body["billed"] == "1000.00"
    assert body["outstanding"] == "0.00"
    assert body["reversals"] == "0.00"


async def test_running_ipd_charges_are_not_outstanding(
    api: AsyncClient, stay: support.Stay, accountant: str
) -> None:
    """The stay's consultation is an issued, unpaid OPD bill: outstanding.
    The ward's dressing is not billed yet: running in wards, not outstanding."""
    await _add_on_stay(api, accountant, stay, _DRESSING)

    body = (await api.get(f"{ACCOUNTS}/summary", headers=auth(accountant))).json()

    assert body["outstanding"] == "500.00"
    assert body["outstanding_patients"] == 1
    assert body["running_in_wards"] == "268.80"
    assert body["running_patients"] == 1


async def test_lists_are_newest_first(
    api: AsyncClient,
    session: AsyncSession,
    tenant: Hospital,
    admin_user: User,
    accountant: str,
) -> None:
    await _collected(session, tenant, admin_user, "First Patient", PaymentMethod.CASH)
    await _collected(session, tenant, admin_user, "Second Patient", PaymentMethod.CASH)

    bills = (await api.get(f"{ACCOUNTS}/bills", headers=auth(accountant))).json()
    payments = (await api.get(f"{ACCOUNTS}/payments", headers=auth(accountant))).json()
    patients = (await api.get(f"{ACCOUNTS}/patients", headers=auth(accountant))).json()

    assert [row["patient_name"] for row in bills["items"]] == ["Second Patient", "First Patient"]
    assert payments["items"][0]["patient_name"] == "Second Patient"
    assert payments["items"][0]["received_by_name"] == admin_user.full_name
    assert payments["items"][0]["invoice_number"] == bills["items"][0]["invoice_number"]
    assert [row["name"] for row in patients["items"]] == ["Second Patient", "First Patient"]
    assert patients["items"][0]["paid"] == "500.00"
    assert patients["items"][0]["due"] == "0.00"


async def test_bills_are_searchable_by_name_and_number(
    api: AsyncClient,
    session: AsyncSession,
    tenant: Hospital,
    admin_user: User,
    accountant: str,
) -> None:
    await _collected(session, tenant, admin_user, "Sunita Devi", PaymentMethod.CASH)
    await _collected(session, tenant, admin_user, "Kavita Sharma", PaymentMethod.CASH)

    by_name = await api.get(f"{ACCOUNTS}/bills", params={"q": "kavita"}, headers=auth(accountant))
    [row] = by_name.json()["items"]
    by_number = await api.get(
        f"{ACCOUNTS}/bills", params={"q": row["invoice_number"]}, headers=auth(accountant)
    )

    assert row["patient_name"] == "Kavita Sharma"
    assert [found["invoice_id"] for found in by_number.json()["items"]] == [row["invoice_id"]]


async def test_outstanding_lists_issued_bills_with_a_balance(
    api: AsyncClient, stay: support.Stay, accountant: str
) -> None:
    rows = (await api.get(f"{ACCOUNTS}/outstanding", headers=auth(accountant))).json()

    [row] = rows["items"]
    assert row["scope"] == "OPD"
    assert row["balance_due"] == "500.00"
    assert row["patient_name"] == "Ramesh Yadav"


async def test_history_shows_the_consultation_and_the_stay_separately(
    api: AsyncClient, stay: support.Stay, accountant: str
) -> None:
    await _add_on_stay(api, accountant, stay, _DRESSING)

    history = await api.get(f"{PATIENTS}/{stay.patient.id}/history", headers=auth(accountant))

    assert history.status_code == 200, history.text
    body = history.json()
    assert [visit["kind"] for visit in body["visits"]] == ["IPD", "OPD"]
    ipd, opd = body["visits"]
    assert ipd["admission_number"] == stay.admission.admission_number
    assert ipd["status_label"] == "Running"
    assert ipd["total"] == "268.80"
    assert opd["total"] == "500.00"
    assert opd["due"] == "500.00"
    assert [invoice["scope"] for invoice in opd["invoices"]] == ["OPD"]
    assert body["lifetime_billed"] == "768.80"
    assert body["lifetime_paid"] == "0.00"
    assert body["lifetime_due"] == "768.80"


async def test_journey_lists_medicines_newest_first_and_totals_by_category(
    api: AsyncClient, stay: support.Stay, accountant: str
) -> None:
    await _add_on_stay(api, accountant, stay, _DRESSING)
    await _add_on_stay(api, accountant, stay, _OXYGEN)
    url = f"{PATIENTS}/{stay.patient.id}/journey"

    everything = (await api.get(url, headers=auth(accountant))).json()
    medicines = (
        await api.get(url, params={"category": "PHARMACY"}, headers=auth(accountant))
    ).json()

    lines = everything["lines"]["items"]
    assert [line["description"] for line in lines] == [
        "Oxygen cylinder",
        "Wound dressing",
        "OPD consultation",
    ]
    assert [line["kind"] for line in lines] == ["IPD", "IPD", "OPD"]
    assert everything["totals_by_category"] == {
        "OTHER": "1000.00",
        "PHARMACY": "268.80",
        "CONSULTATION": "500.00",
    }
    assert [line["description"] for line in medicines["lines"]["items"]] == ["Wound dressing"]
    assert medicines["totals_by_category"] == {"PHARMACY": "268.80"}


async def test_title_is_tax_invoice_only_when_a_line_carries_gst(
    api: AsyncClient,
    session: AsyncSession,
    stay: support.Stay,
    accountant: str,
    admin_user: User,
) -> None:
    await _add_on_stay(api, accountant, stay, _DRESSING)
    await ipd_service.discharge_patient(
        session,
        stay.admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=admin_user,
    )
    await session.commit()
    final = await api.post(f"{ACCOUNTS}/{stay.encounter.id}/final-bill", headers=auth(accountant))
    assert final.status_code == 201, final.text
    bills = (await api.get(f"{ACCOUNTS}/bills", headers=auth(accountant))).json()["items"]
    [opd] = [row for row in bills if row["scope"] == "OPD"]

    ipd_print = await api.get(
        f"{INVOICES}/{final.json()['id']}/printable", headers=auth(accountant)
    )
    opd_print = await api.get(f"{INVOICES}/{opd['invoice_id']}/printable", headers=auth(accountant))

    assert ipd_print.status_code == 200, ipd_print.text
    printed = ipd_print.json()
    assert printed["title"] == "TAX INVOICE"
    assert printed["number"] == final.json()["invoice_number"]
    assert printed["stay"]["admission_number"] == stay.admission.admission_number
    assert printed["letterhead"]["name"] == "Test Hospital"
    assert printed["patient"]["uhid"] == stay.patient.uhid
    assert printed["totals"]["grand_total"] == final.json()["grand_total"]
    assert opd_print.json()["title"] == "BILL OF SUPPLY"
    assert opd_print.json()["stay"] is None


async def test_running_bill_prints_as_interim(
    api: AsyncClient, stay: support.Stay, accountant: str
) -> None:
    await _add_on_stay(api, accountant, stay, _DRESSING)

    printed = await api.get(
        f"{ACCOUNTS}/{stay.encounter.id}/ipd-bill/printable", headers=auth(accountant)
    )

    assert printed.status_code == 200, printed.text
    body = printed.json()
    assert body["title"] == "INTERIM — NOT A FINAL BILL"
    assert body["number"] is None
    assert [line["description"] for line in body["lines"]] == ["Wound dressing"]
    # Rounded to the rupee, exactly as the issued bill will be.
    assert body["totals"]["grand_total"] == "269.00"


async def test_receptionist_cannot_open_the_accounts_dashboard(
    api: AsyncClient, session: AsyncSession, tenant: Hospital, stay: support.Stay
) -> None:
    token = await _token(api, session, tenant, Roles.RECEPTIONIST)

    for path in ("summary", "bills", "payments", "outstanding", "patients"):
        response = await api.get(f"{ACCOUNTS}/{path}", headers=auth(token))
        assert response.status_code == 403, (path, response.text)
    history = await api.get(f"{PATIENTS}/{stay.patient.id}/history", headers=auth(token))
    assert history.status_code == 403
