"""Billing over HTTP: a whole counter shift, and RBAC.

The first class walks one OPD visit from registration to a paid, closed
encounter exactly as the staff would, because that is the chain CLAUDE.md §13
step 7 asks for and it only works if every link agrees.

The RBAC class is the one to read closely. This is the module where an
access-control mistake becomes theft rather than inconvenience, so the tests
assert the separations by name: a cashier may take money and may not reduce what
is owed.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
QUEUE = "/api/v1/queue"
ENCOUNTERS = "/api/v1/encounters"
RATE_CARDS = "/api/v1/billing/rate-cards"
SERVICES = "/api/v1/billing/services"
CHARGES = "/api/v1/billing/charges"
INVOICES = "/api/v1/billing/invoices"
PAYMENTS = "/api/v1/billing/payments"
ACCOUNTS = "/api/v1/billing/accounts"
CLAIMS = "/api/v1/billing/claims"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def doctor_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)


@pytest.fixture
async def cashier_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.CASHIER, hospital_id=hospital.id)


@pytest.fixture
async def billing_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.BILLING_STAFF, hospital_id=hospital.id)


@pytest.fixture
async def admin_token(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def reception_token(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def doctor_token(api: AsyncClient, doctor_user: User) -> str:
    return await login(api, doctor_user)


@pytest.fixture
async def cashier_token(api: AsyncClient, cashier_user: User) -> str:
    return await login(api, cashier_user)


@pytest.fixture
async def billing_token(api: AsyncClient, billing_user: User) -> str:
    return await login(api, billing_user)


@pytest.fixture
async def price_list(api: AsyncClient, admin_token: str) -> dict[str, str]:
    """A cash rate card and three priced services: two exempt, one taxable."""
    card = await api.post(
        RATE_CARDS,
        json={
            "code": "CASH",
            "name": "Cash counter",
            "payer_type": "CASH",
            "is_default": True,
        },
        headers=auth(admin_token),
    )
    assert card.status_code == 201, card.text
    card_id = card.json()["id"]

    ids: dict[str, str] = {"card": card_id}
    for code, name, category, price, exempt, rate in (
        ("OPD-CONSULT", "OPD consultation", "CONSULTATION", "500.00", True, "0"),
        ("CBC", "Complete Blood Count", "LAB", "350.00", True, "0"),
        ("DRESSING", "Wound dressing", "PHARMACY", "240.00", False, "12.00"),
    ):
        created = await api.post(
            SERVICES,
            json={
                "code": code,
                "name": name,
                "category": category,
                "is_gst_exempt": exempt,
                "gst_rate": rate,
            },
            headers=auth(admin_token),
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]
        priced = await api.put(
            f"{SERVICES}/{item_id}/prices",
            json={"rate_card_id": card_id, "price": price},
            headers=auth(admin_token),
        )
        assert priced.status_code == 200, priced.text
        ids[code] = item_id
    return ids


@pytest.fixture
async def visit(api: AsyncClient, reception_token: str, price_list: dict[str, str]) -> dict:
    """A registered patient with an open encounter, consultation fee captured."""
    patient = await api.post(
        PATIENTS,
        json={
            "full_name": "Sunita Devi",
            "phone": "9876543210",
            "gender": "FEMALE",
            "age_years": 34,
        },
        headers=auth(reception_token),
    )
    assert patient.status_code in (200, 201), patient.text
    patient_id = patient.json()["id"]

    encounter = await api.post(
        ENCOUNTERS,
        json={"patient_id": patient_id},
        headers=auth(reception_token),
    )
    assert encounter.status_code == 201, encounter.text
    return {"patient_id": patient_id, "encounter": encounter.json()}


# ---------------------------------------------------------------------------
# A counter shift, end to end
# ---------------------------------------------------------------------------
class TestTheCounterShift:
    async def test_registration_puts_the_consultation_fee_on_the_bill(
        self, api: AsyncClient, cashier_token: str, visit: dict
    ) -> None:
        """Nobody typed this. It was captured by a transactional subscriber in
        the same transaction that opened the visit."""
        response = await api.get(
            f"{ACCOUNTS}/{visit['encounter']['id']}", headers=auth(cashier_token)
        )
        assert response.status_code == 200, response.text
        account = response.json()

        assert account["pending_total"] == "500.00"
        assert account["balance_due"] == "500.00"
        assert account["has_unpriced_items"] is False
        assert len(account["pending_charges"]) == 1
        assert account["pending_charges"][0]["category"] == "CONSULTATION"

    async def test_the_whole_chain_from_order_to_a_closed_paid_visit(
        self,
        api: AsyncClient,
        doctor_token: str,
        cashier_token: str,
        visit: dict,
    ) -> None:
        encounter_id = visit["encounter"]["id"]

        # The doctor sees the patient and orders a blood test.
        started = await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        assert started.status_code == 200, started.text

        order = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/orders",
            json={"order_type": "LAB", "item_code": "CBC", "item_name": "CBC"},
            headers=auth(doctor_token),
        )
        assert order.status_code == 201, order.text

        # Reception adds a dressing that no event could have seen.
        dressing = await api.post(
            CHARGES,
            json={"encounter_id": encounter_id, "service_item_code": "DRESSING"},
            headers=auth(cashier_token),
        )
        assert dressing.status_code == 201, dressing.text

        # Draft, and check the per-line GST: only the dressing is taxable.
        draft = await api.post(
            INVOICES, json={"encounter_id": encounter_id}, headers=auth(cashier_token)
        )
        assert draft.status_code == 201, draft.text
        invoice = draft.json()

        assert invoice["status"] == "DRAFT"
        assert invoice["subtotal"] == "1090.00"
        assert invoice["cgst_total"] == "14.40"
        assert invoice["sgst_total"] == "14.40"
        assert invoice["grand_total"] == "1119.00"
        assert invoice["round_off"] == "0.20"
        assert len(invoice["charges"]) == 3

        # Money cannot be taken against a draft.
        early = await api.post(
            f"{INVOICES}/{invoice['id']}/payments",
            json={"amount": "1119.00", "method": "UPI", "reference": "UPI-1"},
            headers=auth(cashier_token),
        )
        assert early.status_code == 409
        assert early.json()["error"]["code"] == "invoice_not_issued"

        issued = await api.post(
            f"{INVOICES}/{invoice['id']}/issue", json={}, headers=auth(cashier_token)
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["status"] == "ISSUED"
        assert issued.json()["invoice_number"].startswith("INV-")

        # UPI first (CLAUDE.md §9), and a digital payment needs a reference.
        no_reference = await api.post(
            f"{INVOICES}/{invoice['id']}/payments",
            json={"amount": "1119.00", "method": "UPI"},
            headers=auth(cashier_token),
        )
        assert no_reference.status_code == 422

        paid = await api.post(
            f"{INVOICES}/{invoice['id']}/payments",
            json={"amount": "1119.00", "method": "UPI", "reference": "UPI-2026-000441"},
            headers=auth(cashier_token),
        )
        assert paid.status_code == 201, paid.text
        assert paid.json()["receipt_number"].startswith("RCP-")

        final = await api.get(f"{INVOICES}/{invoice['id']}", headers=auth(cashier_token))
        assert final.json()["status"] == "PAID"
        assert final.json()["balance_due"] == "0.00"

    async def test_an_unpaid_bill_holds_the_visit_and_paying_releases_it(
        self,
        api: AsyncClient,
        doctor_token: str,
        cashier_token: str,
        visit: dict,
    ) -> None:
        """CLAUDE.md §6 from the money side: the cashier is a clearance station,
        and clearing the last item closes the visit with nobody pressing 'close'."""
        encounter_id = visit["encounter"]["id"]
        await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))

        completed = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/complete", json={}, headers=auth(doctor_token)
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "PENDING_CLEARANCE"

        draft = await api.post(
            INVOICES, json={"encounter_id": encounter_id}, headers=auth(cashier_token)
        )
        invoice_id = draft.json()["id"]
        await api.post(f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(cashier_token))
        await api.post(
            f"{INVOICES}/{invoice_id}/payments",
            json={"amount": "500.00", "method": "CASH"},
            headers=auth(cashier_token),
        )

        visit_now = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(doctor_token))
        assert visit_now.json()["status"] == "COMPLETED"

    async def test_an_unpriced_item_lands_on_the_exception_worklist(
        self, api: AsyncClient, doctor_token: str, cashier_token: str, visit: dict
    ) -> None:
        """Care delivered at an unknown price is a hole in the month's revenue.
        The charge is captured at zero so somebody can find it."""
        encounter_id = visit["encounter"]["id"]
        await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        placed = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/orders",
            json={
                "order_type": "PROCEDURE",
                "item_code": "NCS",
                "item_name": "Nerve conduction study",
            },
            headers=auth(doctor_token),
        )
        assert placed.status_code == 201, placed.text

        worklist = await api.get(
            CHARGES, params={"needs_pricing": True}, headers=auth(cashier_token)
        )
        assert worklist.status_code == 200
        items = worklist.json()["items"]
        assert len(items) == 1
        assert items[0]["description"] == "Nerve conduction study"
        assert items[0]["total_amount"] == "0.00"

        account = await api.get(f"{ACCOUNTS}/{encounter_id}", headers=auth(cashier_token))
        assert account.json()["has_unpriced_items"] is True

    async def test_a_reversal_reopens_the_bill_and_leaves_both_receipts(
        self, api: AsyncClient, cashier_token: str, billing_token: str, visit: dict
    ) -> None:
        encounter_id = visit["encounter"]["id"]
        draft = await api.post(
            INVOICES, json={"encounter_id": encounter_id}, headers=auth(cashier_token)
        )
        invoice_id = draft.json()["id"]
        await api.post(f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(cashier_token))
        payment = await api.post(
            f"{INVOICES}/{invoice_id}/payments",
            json={"amount": "500.00", "method": "CASH"},
            headers=auth(cashier_token),
        )
        payment_id = payment.json()["id"]

        reversed_payment = await api.post(
            f"{PAYMENTS}/{payment_id}/reverse",
            json={"reason": "Taken against the wrong patient."},
            headers=auth(billing_token),
        )
        assert reversed_payment.status_code == 200, reversed_payment.text
        assert reversed_payment.json()["status"] == "REVERSED"

        after = await api.get(f"{INVOICES}/{invoice_id}", headers=auth(cashier_token))
        assert after.json()["status"] == "ISSUED"
        assert after.json()["balance_due"] == "500.00"
        # The original receipt survives exactly as printed, plus its mirror.
        assert len(after.json()["payments"]) == 2


# ---------------------------------------------------------------------------
# Settlement (CLAUDE.md §6)
# ---------------------------------------------------------------------------
class TestSettlement:
    async def test_a_death_leaves_the_bill_on_a_worklist_not_with_the_patient(
        self,
        api: AsyncClient,
        doctor_token: str,
        cashier_token: str,
        doctor_user: User,
        visit: dict,
    ) -> None:
        encounter_id = visit["encounter"]["id"]

        recorded = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": "2026-08-08T14:20:00Z",
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest.",
                "death_place": "Casualty",
            },
            headers=auth(doctor_token),
        )
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()["status"] == "DECEASED"

        worklist = await api.get(
            INVOICES, params={"awaiting_settlement": True}, headers=auth(cashier_token)
        )
        assert worklist.status_code == 200
        items = worklist.json()["items"]
        assert len(items) == 1
        assert items[0]["settlement_context"] == "DECEASED"
        assert items[0]["balance_due"] == "500.00"

        detail = await api.get(f"{INVOICES}/{items[0]['id']}", headers=auth(cashier_token))
        assert "No automated reminders" in detail.json()["settlement_note"]

    async def test_a_lama_bill_is_settlable_and_writable_off(
        self, api: AsyncClient, doctor_token: str, billing_token: str, visit: dict
    ) -> None:
        """A patient who walked out still consumed care. Somebody has to be able
        to decide to stop chasing it — with their name on the decision."""
        encounter_id = visit["encounter"]["id"]
        left = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/lama",
            json={"lama_reason": "Absconded from the waiting area.", "lama_form_signed": False},
            headers=auth(doctor_token),
        )
        assert left.status_code == 200, left.text

        worklist = await api.get(
            INVOICES, params={"awaiting_settlement": True}, headers=auth(billing_token)
        )
        invoice_id = worklist.json()["items"][0]["id"]

        await api.post(f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(billing_token))
        written_off = await api.post(
            f"{INVOICES}/{invoice_id}/write-off",
            json={"reason": "Absconded; recovery abandoned."},
            headers=auth(billing_token),
        )
        assert written_off.status_code == 200, written_off.text
        assert written_off.json()["status"] == "WRITTEN_OFF"
        assert written_off.json()["balance_due"] == "0.00"


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
class TestClaims:
    async def test_settling_a_claim_pays_the_invoice(
        self, api: AsyncClient, cashier_token: str, billing_token: str, visit: dict
    ) -> None:
        """From the hospital's side an insurer's payment is a payment. Leaving it
        off would show a settled bill as outstanding forever."""
        encounter_id = visit["encounter"]["id"]
        draft = await api.post(
            INVOICES,
            json={"encounter_id": encounter_id, "payer_liability": "500.00"},
            headers=auth(cashier_token),
        )
        invoice_id = draft.json()["id"]
        await api.post(f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(cashier_token))

        claim = await api.post(
            CLAIMS,
            json={
                "invoice_id": invoice_id,
                "payer_name": "Star Health",
                "policy_number": "SH-99201",
                "tpa_name": "Medi Assist",
            },
            headers=auth(billing_token),
        )
        assert claim.status_code == 201, claim.text
        claim_id = claim.json()["id"]
        assert claim.json()["claimed_amount"] == "500.00"

        for payload in (
            {"status": "SUBMITTED"},
            {"status": "APPROVED", "approved_amount": "450.00"},
            {"status": "SETTLED", "settled_amount": "450.00"},
        ):
            step = await api.patch(
                f"{CLAIMS}/{claim_id}", json=payload, headers=auth(billing_token)
            )
            assert step.status_code == 200, step.text

        invoice = await api.get(f"{INVOICES}/{invoice_id}", headers=auth(cashier_token))
        assert invoice.json()["amount_paid"] == "450.00"
        assert invoice.json()["balance_due"] == "50.00"
        assert invoice.json()["status"] == "PARTIALLY_PAID"

    async def test_a_rejected_claim_must_carry_the_payers_reason(
        self, api: AsyncClient, cashier_token: str, billing_token: str, visit: dict
    ) -> None:
        """Somebody has to appeal this, and they need to know what to answer."""
        draft = await api.post(
            INVOICES,
            json={"encounter_id": visit["encounter"]["id"]},
            headers=auth(cashier_token),
        )
        invoice_id = draft.json()["id"]
        await api.post(f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(cashier_token))
        claim = await api.post(
            CLAIMS,
            json={"invoice_id": invoice_id, "payer_name": "Star Health"},
            headers=auth(billing_token),
        )
        rejected = await api.patch(
            f"{CLAIMS}/{claim.json()['id']}",
            json={"status": "REJECTED"},
            headers=auth(billing_token),
        )
        assert rejected.status_code == 422


# ---------------------------------------------------------------------------
# RBAC — the separations that matter
# ---------------------------------------------------------------------------
class TestPermissions:
    async def test_a_cashier_may_take_money_but_not_waive_a_charge(
        self, api: AsyncClient, cashier_token: str, visit: dict
    ) -> None:
        """The whole point of the split. A cashier who could waive a charge and
        pocket the cash is a cashier the hospital cannot audit."""
        account = await api.get(
            f"{ACCOUNTS}/{visit['encounter']['id']}", headers=auth(cashier_token)
        )
        charge_id = account.json()["pending_charges"][0]["id"]

        refused = await api.post(
            f"{CHARGES}/{charge_id}/waive",
            json={"reason": "Concession."},
            headers=auth(cashier_token),
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "permission_denied"

    async def test_a_cashier_cannot_reverse_a_payment(
        self, api: AsyncClient, cashier_token: str, visit: dict
    ) -> None:
        """Giving money back is never bundled with taking it."""
        draft = await api.post(
            INVOICES,
            json={"encounter_id": visit["encounter"]["id"]},
            headers=auth(cashier_token),
        )
        invoice_id = draft.json()["id"]
        await api.post(f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(cashier_token))
        payment = await api.post(
            f"{INVOICES}/{invoice_id}/payments",
            json={"amount": "100.00", "method": "CASH"},
            headers=auth(cashier_token),
        )
        refused = await api.post(
            f"{PAYMENTS}/{payment.json()['id']}/reverse",
            json={"reason": "Mis-keyed."},
            headers=auth(cashier_token),
        )
        assert refused.status_code == 403

    async def test_a_cashier_cannot_change_the_price_list(
        self, api: AsyncClient, cashier_token: str, price_list: dict[str, str]
    ) -> None:
        """Setting prices changes what the hospital charges everybody. That
        belongs with administration, not with the counter."""
        refused = await api.post(
            RATE_CARDS,
            json={"code": "MINE", "name": "My rates", "payer_type": "CASH"},
            headers=auth(cashier_token),
        )
        assert refused.status_code == 403

    async def test_billing_staff_may_waive_and_reverse(
        self, api: AsyncClient, billing_token: str, cashier_token: str, visit: dict
    ) -> None:
        """The back office runs concessions and corrections — with every one of
        them written to the audit log."""
        account = await api.get(
            f"{ACCOUNTS}/{visit['encounter']['id']}", headers=auth(cashier_token)
        )
        charge_id = account.json()["pending_charges"][0]["id"]

        waived = await api.post(
            f"{CHARGES}/{charge_id}/waive",
            json={"reason": "Hospital staff concession."},
            headers=auth(billing_token),
        )
        assert waived.status_code == 200, waived.text
        assert waived.json()["status"] == "WAIVED"

    async def test_a_doctor_sees_the_balance_but_cannot_touch_it(
        self, api: AsyncClient, doctor_token: str, visit: dict
    ) -> None:
        """Knowing the patient in front of you owes money changes the
        conversation. Being able to change it is somebody else's job."""
        readable = await api.get(
            CHARGES,
            params={"encounter_id": visit["encounter"]["id"]},
            headers=auth(doctor_token),
        )
        assert readable.status_code == 200
        assert readable.json()["total"] == 1

        refused = await api.post(
            INVOICES,
            json={"encounter_id": visit["encounter"]["id"]},
            headers=auth(doctor_token),
        )
        assert refused.status_code == 403

    async def test_the_audit_log_records_who_waived_what(
        self,
        api: AsyncClient,
        admin_token: str,
        billing_token: str,
        cashier_token: str,
        visit: dict,
    ) -> None:
        """An auditor asks for the amount and the reason, not that a request
        happened. Both are on the row."""
        account = await api.get(
            f"{ACCOUNTS}/{visit['encounter']['id']}", headers=auth(cashier_token)
        )
        charge_id = account.json()["pending_charges"][0]["id"]
        await api.post(
            f"{CHARGES}/{charge_id}/waive",
            json={"reason": "Hospital staff concession."},
            headers=auth(billing_token),
        )

        log = await api.get(
            "/api/v1/audit-logs",
            params={"action": "billing.charge.waive"},
            headers=auth(admin_token),
        )
        assert log.status_code == 200, log.text
        entries = log.json()["items"]
        assert len(entries) == 1
        assert entries[0]["changes"]["amount"] == "500.00"
        assert entries[0]["changes"]["reason"] == "Hospital staff concession."

    async def test_an_invoice_that_is_not_this_tenants_reads_as_not_found(
        self, api: AsyncClient, cashier_token: str, visit: dict
    ) -> None:
        """Reported as 404, never 403: confirming a record exists in another
        hospital is itself a cross-tenant leak. (Row-level security is tested
        properly against the database in `test_billing_service.py`.)"""
        import uuid

        response = await api.get(f"{INVOICES}/{uuid.uuid4()}", headers=auth(cashier_token))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invoice_not_found"
