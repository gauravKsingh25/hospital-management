"""Billing service: from a doctor's order to a paid, closed visit.

Three tests carry most of the weight here, and each one guards a way hospitals
actually lose money:

* `test_capturing_the_same_act_twice_produces_one_charge` — double billing.
* `test_an_unpriced_act_is_still_captured_and_flagged` — silent revenue loss.
* `test_paying_the_last_balance_closes_the_visit` — the §6 closure rule, from
  the money side rather than the lab side.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError, IllegalStateTransitionError, ValidationError
from app.core.pagination import PageParams
from app.modules.billing import service
from app.modules.billing.models import (
    ChargeCategory,
    ChargeStatus,
    InvoiceStatus,
    PayerType,
    PaymentMethod,
)
from app.modules.billing.schemas import (
    ChargeAdd,
    InvoiceDraftRequest,
    RateCardCreate,
    ServiceItemCreate,
    ServicePriceUpsert,
)
from app.modules.billing.service import financial_year
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter, EncounterStatus, OrderType
from app.modules.clinical.schemas import DeathRecord, OrderCreate
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration

D = Decimal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


async def _user(session: AsyncSession, tenant: Hospital, role: str) -> User:
    from tests.conftest import _make_user

    user = await _make_user(session, role_code=role, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.DOCTOR)


@pytest.fixture
async def cashier_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.CASHIER)


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Sunita Devi", phone="9876543210", gender=Gender.FEMALE, age_years=34
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def priced_hospital(session: AsyncSession, tenant: Hospital) -> Hospital:
    """A hospital with a cash rate card and three priced services.

    Deliberately mixes exempt and taxable, because that is what a real bill
    looks like and it is where per-line GST earns its keep.
    """
    card = await service.create_rate_card(
        session,
        RateCardCreate(
            code="CASH", name="Cash counter", payer_type=PayerType.CASH, is_default=True
        ),
        hospital_id=tenant.id,
    )
    for code, name, category, price, exempt, rate in (
        ("OPD-CONSULT", "OPD consultation", ChargeCategory.CONSULTATION, "500.00", True, "0"),
        ("CBC", "Complete Blood Count", ChargeCategory.LAB, "350.00", True, "0"),
        ("DRESSING", "Wound dressing", ChargeCategory.PHARMACY, "240.00", False, "12.00"),
    ):
        item = await service.create_service_item(
            session,
            ServiceItemCreate(
                code=code,
                name=name,
                category=category,
                is_gst_exempt=exempt,
                gst_rate=D(rate),
            ),
            hospital_id=tenant.id,
        )
        await service.set_price(
            session, item, ServicePriceUpsert(rate_card_id=card.id, price=D(price))
        )
    await session.commit()
    return tenant


@pytest.fixture
async def encounter(
    session: AsyncSession, priced_hospital: Hospital, patient: Patient, doctor_user: User
) -> Encounter:
    record = await clinical_service.open_encounter(
        session, hospital_id=priced_hospital.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    return record


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------
def test_invoice_numbering_follows_the_financial_year_not_the_calendar() -> None:
    """Rule 46 of the CGST Rules wants a series unique for a financial year, and
    India's runs April to March. A March invoice belongs to the year before."""
    from datetime import date

    assert financial_year(date(2026, 4, 1)) == 2026
    assert financial_year(date(2026, 12, 31)) == 2026
    assert financial_year(date(2027, 3, 31)) == 2026
    # One day later is a new series.
    assert financial_year(date(2027, 4, 1)) == 2027


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
async def test_opening_a_visit_captures_the_consultation_fee(
    session: AsyncSession, encounter: Encounter, priced_hospital: Hospital
) -> None:
    """The subscriber fires inside the transaction that opened the visit, so the
    fee is on the bill before anyone has had a chance to forget it."""
    charges, total = await service.list_charges(
        session, PageParams(), hospital_id=priced_hospital.id, encounter_id=encounter.id
    )

    assert total == 1
    assert charges[0].category is ChargeCategory.CONSULTATION
    assert charges[0].unit_price == D("500.00")
    assert charges[0].total_amount == D("500.00")
    assert charges[0].needs_pricing is False


async def test_ordering_a_test_captures_its_charge(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.LAB, item_code="CBC", item_name="CBC"),
        actor=doctor_user,
    )
    await session.commit()

    charges, _ = await service.list_charges(
        session, PageParams(), hospital_id=priced_hospital.id, encounter_id=encounter.id
    )
    lab = [charge for charge in charges if charge.category is ChargeCategory.LAB]

    assert len(lab) == 1
    assert lab[0].total_amount == D("350.00")
    assert lab[0].source_type == "order"


async def test_capturing_the_same_act_twice_produces_one_charge(
    session: AsyncSession, encounter: Encounter, priced_hospital: Hospital
) -> None:
    """Idempotency, which is the property that stops a retried request or a
    re-delivered event billing a patient twice for one blood test."""
    first = await service.capture_charge(
        session,
        hospital_id=priced_hospital.id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        source_module="clinical",
        source_type="order",
        source_id=encounter.id,
        description="Complete Blood Count",
        category=ChargeCategory.LAB,
        item_code="CBC",
    )
    second = await service.capture_charge(
        session,
        hospital_id=priced_hospital.id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        source_module="clinical",
        source_type="order",
        source_id=encounter.id,
        description="Complete Blood Count",
        category=ChargeCategory.LAB,
        item_code="CBC",
    )

    assert first.id == second.id


async def test_an_unpriced_act_is_still_captured_and_flagged(
    session: AsyncSession, encounter: Encounter, priced_hospital: Hospital
) -> None:
    """The property that lets billing subscribe transactionally without ever
    being the reason a clinical order fails. A visible zero is recoverable;
    a missing row is invisible revenue loss."""
    charge = await service.capture_charge(
        session,
        hospital_id=priced_hospital.id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        source_module="clinical",
        source_type="order",
        source_id=encounter.id,
        description="Nerve conduction study",
        category=ChargeCategory.PROCEDURE,
        item_code="NCS-NOT-CONFIGURED",
    )

    assert charge.needs_pricing is True
    assert charge.total_amount == D("0.00")
    assert charge.description == "Nerve conduction study"

    # And it appears on the exception worklist, which is the whole point.
    unpriced, count = await service.list_charges(
        session, PageParams(), hospital_id=priced_hospital.id, needs_pricing=True
    )
    assert count == 1
    assert unpriced[0].id == charge.id


async def test_capture_survives_a_hospital_with_no_rate_card_at_all(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> None:
    """A clinic mid-setup must still be able to open a visit. Note this fixture
    deliberately does NOT use `priced_hospital`."""
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()

    charges, total = await service.list_charges(
        session, PageParams(), hospital_id=tenant.id, encounter_id=encounter.id
    )
    assert total == 1
    assert charges[0].needs_pricing is True


async def test_cancelling_an_order_voids_its_pending_charge(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    order = await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.LAB, item_code="CBC", item_name="CBC"),
        actor=doctor_user,
    )
    await clinical_service.cancel_order(
        session,
        order,
        reason="Ordered in error.",
        actor=doctor_user,
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    charge = await service.find_charge_for_source(
        session,
        hospital_id=priced_hospital.id,
        source_module="clinical",
        source_type="order",
        source_id=order.id,
    )
    # The live-charge lookup excludes cancelled rows, so nothing comes back.
    assert charge is None


async def test_a_pharmacy_order_is_not_charged_at_ordering_time(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    """A prescription's price depends on what is dispensed and how much, which
    nobody knows yet. Charging here would bill for drugs never collected."""
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.PHARMACY, item_name="Amoxicillin 500mg"),
        actor=doctor_user,
    )
    await session.commit()

    charges, _ = await service.list_charges(
        session, PageParams(), hospital_id=priced_hospital.id, encounter_id=encounter.id
    )
    assert all(charge.category is not ChargeCategory.PHARMACY for charge in charges)


# ---------------------------------------------------------------------------
# Invoicing
# ---------------------------------------------------------------------------
async def test_a_draft_gathers_every_pending_charge_and_taxes_per_line(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
    cashier_user: User,
) -> None:
    """The realistic bill: exempt consultation, exempt lab, taxable dressing."""
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.LAB, item_code="CBC", item_name="CBC"),
        actor=doctor_user,
    )
    await service.add_charge(
        session,
        ChargeAdd(encounter_id=encounter.id, service_item_code="DRESSING"),
        hospital_id=priced_hospital.id,
        actor=cashier_user,
    )

    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    assert invoice.status is InvoiceStatus.DRAFT
    assert invoice.subtotal == D("1090.00")
    # Only the dressing is taxable: 240 at 12% = 28.80, split 14.40 / 14.40.
    assert invoice.cgst_total == D("14.40")
    assert invoice.sgst_total == D("14.40")
    assert invoice.tax_total == D("28.80")
    assert invoice.grand_total == D("1119.00")
    assert invoice.round_off == D("0.20")
    assert invoice.invoice_number.startswith("INV-")


async def test_reassembling_pulls_a_later_charge_onto_the_same_draft(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """Reception reviews the invoice, never rebuilds it (CLAUDE.md §7b)."""
    first = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.add_charge(
        session,
        ChargeAdd(encounter_id=encounter.id, service_item_code="DRESSING"),
        hospital_id=priced_hospital.id,
        actor=cashier_user,
    )
    second = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    assert first.id == second.id
    assert second.grand_total == D("769.00")


async def test_an_empty_invoice_cannot_be_issued(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    doctor_user: User,
    cashier_user: User,
) -> None:
    """Handing somebody a bill for nothing burns a serial number out of a GST
    series that has to be consecutive."""
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    invoice = await service.assemble_draft(
        session, InvoiceDraftRequest(encounter_id=encounter.id), hospital_id=tenant.id
    )
    # Remove the one (unpriced) charge so the invoice is genuinely empty.
    for charge in await service._charges_on(session, invoice.id):
        charge.status = ChargeStatus.CANCELLED
        session.add(charge)
    await session.flush()

    with pytest.raises(ValidationError) as exc:
        await service.issue_invoice(session, invoice, actor=cashier_user, hospital_id=tenant.id)
    assert exc.value.code == "invoice_empty"


async def test_an_issued_invoice_freezes_its_charges(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """The patient is holding a printout. A document that changes afterwards is
    not evidence of anything."""
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    await session.commit()

    charge = (await service._charges_on(session, invoice.id))[0]
    with pytest.raises(ConflictError) as exc:
        await service.discount_charge(
            session, charge, amount=D("100.00"), reason="Concession.", actor=cashier_user
        )
    assert exc.value.code == "charge_invoiced"


async def test_an_issued_invoice_cannot_go_back_to_draft(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    from app.modules.billing.transitions import assert_invoice_transition

    with pytest.raises(IllegalStateTransitionError):
        assert_invoice_transition(InvoiceStatus.ISSUED, InvoiceStatus.DRAFT)


async def test_cancelling_an_invoice_returns_its_charges_to_the_running_bill(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """The care still happened and still belongs on whatever bill replaces this."""
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    await service.cancel_invoice(
        session,
        invoice,
        reason="Wrong payer selected.",
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    pending, count = await service.list_charges(
        session,
        PageParams(),
        hospital_id=priced_hospital.id,
        encounter_id=encounter.id,
        status=ChargeStatus.PENDING,
    )
    assert count == 1
    assert pending[0].invoice_id is None


# ---------------------------------------------------------------------------
# Payments and the closure gate
# ---------------------------------------------------------------------------
async def test_payment_cannot_be_taken_against_a_draft(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """A draft total can still change; money must never be received against one."""
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    with pytest.raises(ConflictError) as exc:
        await service.record_payment(
            session,
            invoice,
            amount=D("500.00"),
            method=PaymentMethod.UPI,
            actor=cashier_user,
            hospital_id=priced_hospital.id,
        )
    assert exc.value.code == "invoice_not_issued"


async def test_overpayment_is_refused(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """More than the bill is an advance — a different record with a different
    meaning at year end — not a payment that happens to be too big."""
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    with pytest.raises(ValidationError) as exc:
        await service.record_payment(
            session,
            invoice,
            amount=D("9999.00"),
            method=PaymentMethod.UPI,
            actor=cashier_user,
            hospital_id=priced_hospital.id,
        )
    assert exc.value.code == "payment_exceeds_balance"


async def test_a_part_payment_leaves_the_invoice_partially_paid(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    _, invoice = await service.record_payment(
        session,
        invoice,
        amount=D("200.00"),
        method=PaymentMethod.CASH,
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    assert invoice.status is InvoiceStatus.PARTIALLY_PAID
    assert invoice.amount_paid == D("200.00")
    assert invoice.balance_due == D("300.00")


async def test_an_unpaid_bill_holds_the_visit_in_pending_clearance(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    """The cashier is one of the clearance stations CLAUDE.md §6 names by hand.
    The doctor finishing does not close a visit that still owes money."""
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    updated = await clinical_service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    assert updated.status is EncounterStatus.PENDING_CLEARANCE

    pending = await clinical_service.pending_items(session, updated)
    assert pending.by_type["BILLING"] == 1
    assert "500.00 outstanding" in pending.descriptions[0]


async def test_paying_the_last_balance_closes_the_visit(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
    cashier_user: User,
) -> None:
    """CLAUDE.md §6 from the money side: the final pending item is cleared by the
    relevant staff, and nobody has to remember to go back and close the visit."""
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.complete_consultation(session, encounter, actor=doctor_user)

    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    _, invoice = await service.record_payment(
        session,
        invoice,
        amount=invoice.balance_due,
        method=PaymentMethod.UPI,
        actor=cashier_user,
        hospital_id=priced_hospital.id,
        reference="UPI-2026-0001",
    )
    await session.commit()

    assert invoice.status is InvoiceStatus.PAID
    closed = await clinical_service.get_encounter(
        session, encounter.id, hospital_id=priced_hospital.id
    )
    assert closed.status is EncounterStatus.COMPLETED


async def test_writing_off_a_bill_also_releases_the_visit(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
    cashier_user: User,
) -> None:
    """A patient who absconded owing money leaves an encounter that would
    otherwise sit open forever. Somebody has to be able to make that decision."""
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.complete_consultation(session, encounter, actor=doctor_user)

    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    await service.write_off_invoice(
        session,
        invoice,
        reason="Patient absconded; recovery abandoned.",
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    assert invoice.status is InvoiceStatus.WRITTEN_OFF
    assert invoice.balance_due == D("0.00")
    closed = await clinical_service.get_encounter(
        session, encounter.id, hospital_id=priced_hospital.id
    )
    assert closed.status is EncounterStatus.COMPLETED


async def test_waiving_the_only_charge_also_releases_the_visit(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
    cashier_user: User,
) -> None:
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    await clinical_service.complete_consultation(session, encounter, actor=doctor_user)

    charges, _ = await service.list_charges(
        session, PageParams(), hospital_id=priced_hospital.id, encounter_id=encounter.id
    )
    await service.waive_charge(
        session, charges[0], reason="Hospital staff concession.", actor=cashier_user
    )
    reloaded = await clinical_service.get_encounter(
        session, encounter.id, hospital_id=priced_hospital.id
    )
    await clinical_service.reevaluate_closure(session, reloaded, actor=cashier_user)
    await session.commit()

    assert reloaded.status is EncounterStatus.COMPLETED


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------
async def test_reversing_a_payment_writes_a_second_row_and_reopens_the_bill(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """The original receipt stays exactly as it was printed — the cash drawer
    reconciles against what happened, not against what anyone wishes had."""
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    payment, invoice = await service.record_payment(
        session,
        invoice,
        amount=D("500.00"),
        method=PaymentMethod.CASH,
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    assert invoice.status is InvoiceStatus.PAID

    _, invoice = await service.reverse_payment(
        session,
        payment,
        reason="Taken against the wrong patient.",
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    assert invoice.status is InvoiceStatus.ISSUED
    assert invoice.amount_paid == D("0.00")
    assert invoice.balance_due == D("500.00")

    rows, count = await service.list_payments(
        session, PageParams(), hospital_id=priced_hospital.id, invoice_id=invoice.id
    )
    # Two rows: the original, now marked reversed, and its mirror.
    assert count == 2
    assert {row.receipt_number for row in rows} != {payment.receipt_number}


async def test_a_reversed_payment_cannot_be_reversed_again(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    payment, _ = await service.record_payment(
        session,
        invoice,
        amount=D("100.00"),
        method=PaymentMethod.CASH,
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    await service.reverse_payment(
        session,
        payment,
        reason="Mis-keyed.",
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    with pytest.raises(IllegalStateTransitionError):
        await service.reverse_payment(
            session,
            payment,
            reason="Again.",
            actor=cashier_user,
            hospital_id=priced_hospital.id,
        )


async def test_an_invoice_with_payments_cannot_simply_be_cancelled(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """Otherwise the payment would point at nothing. Reverse first, then cancel."""
    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await service.issue_invoice(
        session, invoice, actor=cashier_user, hospital_id=priced_hospital.id
    )
    await service.record_payment(
        session,
        invoice,
        amount=D("100.00"),
        method=PaymentMethod.CASH,
        actor=cashier_user,
        hospital_id=priced_hospital.id,
    )
    with pytest.raises(ConflictError) as exc:
        await service.cancel_invoice(
            session,
            invoice,
            reason="Changed our mind.",
            actor=cashier_user,
            hospital_id=priced_hospital.id,
        )
    assert exc.value.code == "invoice_has_payments"


# ---------------------------------------------------------------------------
# Settlement (CLAUDE.md §6)
# ---------------------------------------------------------------------------
async def test_a_death_still_runs_the_settlement_flow(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    """CLAUDE.md §6: a deceased patient usually still has an outstanding bill,
    and it must be settled rather than leaving with the patient."""
    from app.core.models import utc_now

    await clinical_service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=utc_now(),
            death_certified_by_id=doctor_user.id,
            cause_of_death="Cardiac arrest.",
        ),
        actor=doctor_user,
    )
    await session.commit()

    invoices, count = await service.list_invoices(
        session,
        PageParams(),
        hospital_id=priced_hospital.id,
        awaiting_settlement=True,
    )
    assert count == 1
    assert invoices[0].settlement_context == "DECEASED"
    assert invoices[0].balance_due == D("500.00")
    assert "No automated reminders" in (invoices[0].settlement_note or "")


async def test_the_settlement_event_suppresses_patient_notification_on_a_death(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    """The §14 invariant, carried on the event so a dispatcher cannot forget to
    check: the bill is real, the automated message is not."""
    from app.core.events import event_bus
    from app.core.models import utc_now
    from app.modules.billing.events import SettlementRequired

    seen: list[SettlementRequired] = []

    async def capture(event: SettlementRequired) -> None:
        seen.append(event)

    event_bus.subscribe(SettlementRequired, capture)
    try:
        await clinical_service.record_death(
            session,
            encounter,
            DeathRecord(
                deceased_at=utc_now(),
                death_certified_by_id=doctor_user.id,
                cause_of_death="Cardiac arrest.",
            ),
            actor=doctor_user,
        )
        await session.commit()
    finally:
        event_bus._handlers[SettlementRequired].remove(capture)

    assert len(seen) == 1
    assert seen[0].context == "DECEASED"
    assert seen[0].notify_patient is False


async def test_a_referral_out_settles_with_a_notifiable_context(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    from app.modules.clinical.schemas import ReferralRecord

    await clinical_service.record_referral(
        session,
        encounter,
        ReferralRecord(
            referred_to_facility="District Hospital",
            referral_reason="Needs a CT that we do not have.",
        ),
        actor=doctor_user,
    )
    await session.commit()

    invoices, count = await service.list_invoices(
        session, PageParams(), hospital_id=priced_hospital.id, awaiting_settlement=True
    )
    assert count == 1
    assert invoices[0].settlement_context == "REFERRED_OUT"


async def test_a_visit_with_nothing_owing_needs_no_settlement(
    session: AsyncSession,
    tenant: Hospital,
    patient: Patient,
    doctor_user: User,
) -> None:
    """No rate card at all, so the consultation captures at zero — and a zero
    balance is not something to put on anybody's worklist."""
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    updated = await clinical_service.complete_consultation(session, encounter, actor=doctor_user)
    await session.commit()

    assert updated.status is EncounterStatus.COMPLETED
    _, count = await service.list_invoices(
        session, PageParams(), hospital_id=tenant.id, awaiting_settlement=True
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Rate cards
# ---------------------------------------------------------------------------
async def test_only_one_rate_card_can_be_the_default(
    session: AsyncSession, priced_hospital: Hospital
) -> None:
    scheme = await service.create_rate_card(
        session,
        RateCardCreate(
            code="PMJAY",
            name="PMJAY package rates",
            payer_type=PayerType.SCHEME,
            scheme_code="PMJAY",
            is_default=True,
        ),
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    cards, _ = await service.list_rate_cards(session, PageParams(), hospital_id=priced_hospital.id)
    defaults = [card for card in cards if card.is_default]
    assert len(defaults) == 1
    assert defaults[0].id == scheme.id


async def test_the_same_service_prices_differently_per_payer(
    session: AsyncSession, priced_hospital: Hospital
) -> None:
    """CLAUDE.md §9: model rate cards, not single prices."""
    scheme = await service.create_rate_card(
        session,
        RateCardCreate(
            code="PMJAY", name="PMJAY", payer_type=PayerType.SCHEME, scheme_code="PMJAY"
        ),
        hospital_id=priced_hospital.id,
    )
    item = await service.get_service_item_by_code(session, "CBC", hospital_id=priced_hospital.id)
    assert item is not None
    await service.set_price(
        session,
        item,
        ServicePriceUpsert(rate_card_id=scheme.id, price=D("180.00"), is_package=True),
    )
    await session.commit()

    cash_card = await service.get_default_rate_card(session, hospital_id=priced_hospital.id)
    assert cash_card is not None
    cash = await service.lookup_price(session, service_item_id=item.id, rate_card_id=cash_card.id)
    packaged = await service.lookup_price(session, service_item_id=item.id, rate_card_id=scheme.id)
    assert cash is not None
    assert packaged is not None
    assert cash.price == D("350.00")
    assert packaged.price == D("180.00")
    assert packaged.is_package is True


async def test_cancelling_an_order_also_takes_its_report_off_the_lab_worklist(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    doctor_user: User,
) -> None:
    """The Phase 6 gap, closed now that the bus carries a session.

    Cancelling from the clinical side used to leave the diagnostics report on
    the bench. Nothing unsafe followed — signing it was refused — but somebody
    had to spot the stale row by hand.
    """
    from app.modules.diagnostics import service as diagnostics_service
    from app.modules.diagnostics.models import DiagnosticDiscipline, ReportStatus, SpecimenType
    from app.modules.diagnostics.schemas import AccessionRequest, CatalogueItemCreate

    test = await diagnostics_service.create_catalogue_item(
        session,
        CatalogueItemCreate(
            code="CBC",
            name="Complete Blood Count",
            discipline=DiagnosticDiscipline.LAB,
            specimen_type=SpecimenType.BLOOD,
        ),
        hospital_id=priced_hospital.id,
    )
    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
    order = await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.LAB, item_code="CBC", item_name="CBC"),
        actor=doctor_user,
    )
    report, _ = await diagnostics_service.accession_order(
        session,
        order,
        AccessionRequest(order_id=order.id, catalogue_item_id=test.id),
        actor=doctor_user,
        hospital_id=priced_hospital.id,
    )

    await clinical_service.cancel_order(
        session,
        order,
        reason="Patient declined the test.",
        actor=doctor_user,
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    withdrawn = await diagnostics_service.get_report(
        session, report.id, hospital_id=priced_hospital.id
    )
    assert withdrawn.status is ReportStatus.CANCELLED
    assert withdrawn.cancellation_reason == "Patient declined the test."

    # And it is off the open worklist entirely.
    _, open_count = await diagnostics_service.list_reports(
        session, PageParams(), hospital_id=priced_hospital.id, open_only=True
    )
    assert open_count == 0


async def test_another_hospitals_invoice_is_invisible(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """Row-level security, not a WHERE clause someone can forget."""
    import uuid

    from app.core.exceptions import NotFoundError

    invoice = await service.assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=priced_hospital.id,
    )
    await session.commit()

    other = uuid.uuid4()
    await db.set_tenant_context(session, other)
    with pytest.raises(NotFoundError):
        await service.get_invoice(session, invoice.id, hospital_id=other)

    await db.set_tenant_context(session, priced_hospital.id)
    found = await service.get_invoice(session, invoice.id, hospital_id=priced_hospital.id)
    assert found.id == invoice.id


async def test_a_discount_larger_than_the_charge_is_refused(
    session: AsyncSession,
    encounter: Encounter,
    priced_hospital: Hospital,
    cashier_user: User,
) -> None:
    """Somebody meant to type something else, and silently clamping hides it."""
    charges, _ = await service.list_charges(
        session, PageParams(), hospital_id=priced_hospital.id, encounter_id=encounter.id
    )
    with pytest.raises(ValidationError) as exc:
        await service.discount_charge(
            session,
            charges[0],
            amount=D("5000.00"),
            reason="Fat finger.",
            actor=cashier_user,
        )
    assert exc.value.code == "discount_exceeds_charge"
