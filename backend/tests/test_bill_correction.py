"""Correcting an issued bill: Edit on a frozen bill.

An issued bill is never changed in place — it is in the GST series and the
patient holds a copy. A correction issues a new bill with the corrected lines,
moves the receipts onto it, and cancels the old one with the reason and the
new number. All of it, or none of it. Only the accounts office may do it.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError
from app.core.pagination import PageParams
from app.modules.billing import service
from app.modules.billing.models import InvoiceScope, InvoiceStatus, PaymentMethod
from app.modules.billing.schemas import BillAdd, BillDiscount, BillRemove
from app.modules.identity.models import AuditLog, User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.ipd.models import DischargeType
from app.modules.ipd.schemas import DischargeRequest
from app.modules.tenancy.models import Hospital
from tests import accounts_support as support
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

D = support.D
INVOICES = "/api/v1/billing/invoices"


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


async def _paid_opd_bill(session: AsyncSession, tenant: Hospital, admin: User):
    """An OPD visit whose ₹500 consultation was paid in cash at the counter."""
    visit = await support.open_visit(
        session, tenant.id, await support.register(session, tenant.id), admin
    )
    await service.collect_balance(
        session, visit, method=PaymentMethod.CASH, actor=admin, hospital_id=tenant.id
    )
    await session.commit()
    rows, _ = await service.list_invoices(
        session, PageParams(), hospital_id=tenant.id, encounter_id=visit.id
    )
    [invoice] = rows
    return invoice


def _dressing() -> BillAdd:
    return BillAdd(op="add", service_item_code="DRESSING", reason="Dressing was missed")


async def test_correcting_a_paid_bill_issues_a_new_one_and_moves_the_receipt(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    old = await _paid_opd_bill(session, tenant, admin)

    new, changes = await service.correct_invoice(
        session,
        old,
        [_dressing()],
        reason="Dressing was missed",
        actor=admin,
        hospital_id=tenant.id,
    )
    await session.commit()
    await session.refresh(old)

    assert new.id != old.id and new.invoice_number != old.invoice_number
    assert new.scope is InvoiceScope.OPD
    # ₹500 consultation + ₹268.80 dressing, rounded to the rupee.
    assert new.grand_total == D("769.00")
    assert new.amount_paid == D("500.00")
    assert new.status is InvoiceStatus.PARTIALLY_PAID
    assert new.letterhead is not None
    assert old.status is InvoiceStatus.CANCELLED
    assert old.amount_paid == D("0.00")
    assert new.invoice_number in (old.cancellation_reason or "")
    assert "Dressing was missed" in (old.cancellation_reason or "")
    payments, _ = await service.list_payments(
        session, PageParams(), hospital_id=tenant.id, invoice_id=new.id
    )
    assert [payment.amount for payment in payments] == [D("500.00")]
    lines, _ = await service.list_charges(
        session, PageParams(), hospital_id=tenant.id, invoice_id=new.id
    )
    assert sorted(line.description for line in lines) == ["OPD consultation", "Wound dressing"]
    assert [change.op for change in changes] == ["add"]


async def test_a_correction_below_what_was_paid_is_refused(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    old = await _paid_opd_bill(session, tenant, admin)
    lines, _ = await service.list_charges(
        session, PageParams(), hospital_id=tenant.id, invoice_id=old.id
    )

    with pytest.raises(ConflictError) as refused:
        await service.correct_invoice(
            session,
            old,
            [BillDiscount(op="discount", charge_id=lines[0].id, amount=D("100"), reason="Staff")],
            reason="Staff discount",
            actor=admin,
            hospital_id=tenant.id,
        )

    assert refused.value.code == "correction_below_paid"


async def test_a_draft_or_cancelled_bill_is_not_corrected(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    visit = await support.open_visit(
        session, tenant.id, await support.register(session, tenant.id), admin
    )
    from app.modules.billing.schemas import InvoiceDraftRequest

    draft = await service.assemble_draft(
        session, InvoiceDraftRequest(encounter_id=visit.id), hospital_id=tenant.id
    )

    with pytest.raises(ConflictError) as refused:
        await service.correct_invoice(
            session, draft, [_dressing()], reason="Missed", actor=admin, hospital_id=tenant.id
        )

    assert refused.value.code == "invoice_not_correctable"


async def test_correcting_a_final_ipd_bill_keeps_it_the_final_bill(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    stay = await support.admitted_stay(session, tenant.id, admin)
    await ipd_service.discharge_patient(
        session,
        stay.admission,
        DischargeRequest(discharge_type=DischargeType.RECOVERED),
        actor=admin,
    )
    await session.commit()
    await session.refresh(stay.encounter)
    final = await service.finalise_ipd_bill(
        session, stay.encounter, actor=admin, hospital_id=tenant.id
    )
    await session.commit()
    [room] = (
        await service.list_charges(
            session, PageParams(), hospital_id=tenant.id, invoice_id=final.id
        )
    )[0]

    new, _ = await service.correct_invoice(
        session,
        final,
        [BillRemove(op="remove", charge_id=room.id, reason="Same-day discharge")],
        reason="Same-day discharge",
        actor=admin,
        hospital_id=tenant.id,
    )
    await session.commit()

    assert new.scope is InvoiceScope.IPD
    assert new.grand_total == D("0.00")
    bill = await service.ipd_bill(session, stay.encounter, hospital_id=tenant.id)
    assert bill.final_invoice is not None and bill.final_invoice.id == new.id


async def test_only_the_accounts_office_corrects_and_it_is_audited(
    api: AsyncClient, session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    old = await _paid_opd_bill(session, tenant, admin)
    body = {
        "reason": "Dressing was missed",
        "ops": [{"op": "add", "service_item_code": "DRESSING", "reason": "Dressing was missed"}],
    }
    cashier = await _make_user(session, role_code=Roles.BILLING_STAFF, hospital_id=tenant.id)
    accountant = await _make_user(session, role_code=Roles.ACCOUNTANT, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)

    refused = await api.post(
        f"{INVOICES}/{old.id}/correct", json=body, headers=auth(await login(api, cashier))
    )
    corrected = await api.post(
        f"{INVOICES}/{old.id}/correct", json=body, headers=auth(await login(api, accountant))
    )

    assert refused.status_code == 403, refused.text
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["grand_total"] == "769.00"
    audit = (
        await session.execute(
            sa.select(AuditLog).where(
                AuditLog.action == "billing.invoice.correct",
                AuditLog.resource_id == old.id,
            )
        )
    ).scalar_one()
    assert audit.changes["new_invoice_number"] == corrected.json()["invoice_number"]
    assert audit.changes["reason"] == "Dressing was missed"
    assert uuid.UUID(corrected.json()["id"]) != old.id
