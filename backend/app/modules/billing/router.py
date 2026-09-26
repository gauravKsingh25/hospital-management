"""Billing HTTP routes. Thin — logic lives in `service.py`.

Four surfaces, because four different people use them. The price list is set up
once by an administrator. The visit account is the cash counter's screen and is
worked all day. Invoices and payments are the document trail. Claims are a back
office job with a different rhythm entirely.

Money-moving routes audit-log explicitly. The generic audit log already records
that a request happened; what an auditor asks for is the amount, the method and
the reference — which is what makes a reversal or a write-off explicable six
months later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import SessionDep, TenantId, get_client_ip, require
from app.core.pagination import MAX_PAGE_SIZE, Page, PageParams, page_params
from app.modules.billing import service
from app.modules.billing.models import (
    ChargeCategory,
    ChargeStatus,
    ClaimStatus,
    Invoice,
    InvoiceStatus,
    PayerType,
    PaymentMethod,
)
from app.modules.billing.rbac import BillingPermissions
from app.modules.billing.schemas import (
    AccountBoardEntry,
    ChargeAdd,
    ChargeRead,
    ClaimCreate,
    ClaimRead,
    ClaimUpdate,
    DiscountRequest,
    InvoiceDraftRequest,
    InvoiceIssueRequest,
    InvoiceRead,
    InvoiceSummary,
    PaymentRead,
    PaymentRequest,
    RateCardCreate,
    RateCardRead,
    RateCardUpdate,
    ReasonRequest,
    ReversalRequest,
    ServiceItemCreate,
    ServiceItemRead,
    ServiceItemUpdate,
    ServicePriceRead,
    ServicePriceUpsert,
    VisitAccount,
)
from app.modules.clinical import service as clinical_service
from app.modules.identity import service as identity_service
from app.modules.identity.service import AuthContext
from app.modules.patients import service as patients_service
from app.modules.patients.schemas import PatientRead

rate_cards_router = APIRouter(prefix="/billing/rate-cards", tags=["billing"])
services_router = APIRouter(prefix="/billing/services", tags=["billing"])
charges_router = APIRouter(prefix="/billing/charges", tags=["billing"])
invoices_router = APIRouter(prefix="/billing/invoices", tags=["billing"])
payments_router = APIRouter(prefix="/billing/payments", tags=["billing"])
claims_router = APIRouter(prefix="/billing/claims", tags=["billing"])
accounts_router = APIRouter(prefix="/billing/accounts", tags=["billing"])

CanReadRateCard = Annotated[AuthContext, Depends(require(BillingPermissions.RATECARD_READ))]
CanManageRateCard = Annotated[AuthContext, Depends(require(BillingPermissions.RATECARD_MANAGE))]
CanReadService = Annotated[AuthContext, Depends(require(BillingPermissions.SERVICE_READ))]
CanManageService = Annotated[AuthContext, Depends(require(BillingPermissions.SERVICE_MANAGE))]
CanReadCharge = Annotated[AuthContext, Depends(require(BillingPermissions.CHARGE_READ))]
CanAddCharge = Annotated[AuthContext, Depends(require(BillingPermissions.CHARGE_ADD))]
CanDiscount = Annotated[AuthContext, Depends(require(BillingPermissions.CHARGE_DISCOUNT))]
CanWaive = Annotated[AuthContext, Depends(require(BillingPermissions.CHARGE_WAIVE))]
CanReadInvoice = Annotated[AuthContext, Depends(require(BillingPermissions.INVOICE_READ))]
CanCreateInvoice = Annotated[AuthContext, Depends(require(BillingPermissions.INVOICE_CREATE))]
CanIssueInvoice = Annotated[AuthContext, Depends(require(BillingPermissions.INVOICE_ISSUE))]
CanCancelInvoice = Annotated[AuthContext, Depends(require(BillingPermissions.INVOICE_CANCEL))]
CanWriteOff = Annotated[AuthContext, Depends(require(BillingPermissions.INVOICE_WRITE_OFF))]
CanReadPayment = Annotated[AuthContext, Depends(require(BillingPermissions.PAYMENT_READ))]
CanRecordPayment = Annotated[AuthContext, Depends(require(BillingPermissions.PAYMENT_RECORD))]
CanReversePayment = Annotated[AuthContext, Depends(require(BillingPermissions.PAYMENT_REVERSE))]
CanReadClaim = Annotated[AuthContext, Depends(require(BillingPermissions.CLAIM_READ))]
CanManageClaim = Annotated[AuthContext, Depends(require(BillingPermissions.CLAIM_MANAGE))]


async def _render(session: SessionDep, invoice: Invoice, *, hospital_id: uuid.UUID) -> InvoiceRead:
    """Assemble the whole document: header, lines and receipts.

    One response rather than three. An invoice whose lines arrive after its
    total is an invoice somebody reads the total off before the lines load.
    """
    rendered = InvoiceRead.model_validate(invoice)
    charges, _ = await service.list_charges(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=hospital_id,
        invoice_id=invoice.id,
    )
    payments, _ = await service.list_payments(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=hospital_id,
        invoice_id=invoice.id,
    )
    rendered.charges = [ChargeRead.model_validate(charge) for charge in charges]
    rendered.payments = [PaymentRead.model_validate(payment) for payment in payments]
    return rendered


# ---------------------------------------------------------------------------
# Rate cards
# ---------------------------------------------------------------------------
@rate_cards_router.post(
    "",
    response_model=RateCardRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a rate card",
)
async def create_rate_card(
    payload: RateCardCreate,
    session: SessionDep,
    context: CanManageRateCard,
    tenant_id: TenantId,
) -> RateCardRead:
    """One card per payer: cash, an insurer's negotiated list, a scheme's
    package rates (CLAUDE.md §9)."""
    card = await service.create_rate_card(session, payload, hospital_id=tenant_id)
    await session.commit()
    return RateCardRead.model_validate(card)


@rate_cards_router.get("", response_model=Page[RateCardRead], summary="List rate cards")
async def list_rate_cards(
    session: SessionDep,
    context: CanReadRateCard,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    payer_type: PayerType | None = None,
    include_inactive: bool = False,
) -> Page[RateCardRead]:
    cards, total = await service.list_rate_cards(
        session,
        params,
        hospital_id=tenant_id,
        payer_type=payer_type,
        include_inactive=include_inactive,
    )
    return Page.build(
        [RateCardRead.model_validate(card) for card in cards], total=total, params=params
    )


@rate_cards_router.get("/{card_id}", response_model=RateCardRead, summary="Get a rate card")
async def get_rate_card(
    card_id: uuid.UUID, session: SessionDep, context: CanReadRateCard, tenant_id: TenantId
) -> RateCardRead:
    return RateCardRead.model_validate(
        await service.get_rate_card(session, card_id, hospital_id=tenant_id)
    )


@rate_cards_router.patch("/{card_id}", response_model=RateCardRead, summary="Edit a rate card")
async def update_rate_card(
    card_id: uuid.UUID,
    payload: RateCardUpdate,
    session: SessionDep,
    context: CanManageRateCard,
    tenant_id: TenantId,
) -> RateCardRead:
    card = await service.get_rate_card(session, card_id, hospital_id=tenant_id)
    updated = await service.update_rate_card(session, card, payload)
    await session.commit()
    return RateCardRead.model_validate(updated)


@rate_cards_router.post(
    "/{card_id}/default", response_model=RateCardRead, summary="Make this the default card"
)
async def set_default_rate_card(
    card_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    context: CanManageRateCard,
    tenant_id: TenantId,
) -> RateCardRead:
    """The card a walk-in cash patient gets, and the fallback for everyone whose
    scheme has no card configured. Exactly one per hospital."""
    card = await service.get_rate_card(session, card_id, hospital_id=tenant_id)
    updated = await service.set_default_rate_card(session, card)
    await identity_service.record_audit(
        session,
        action="billing.rate_card.default",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="rate_card",
        resource_id=card.id,
        changes={"code": card.code},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return RateCardRead.model_validate(updated)


# ---------------------------------------------------------------------------
# Services and prices
# ---------------------------------------------------------------------------
@services_router.post(
    "",
    response_model=ServiceItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a billable service",
)
async def create_service_item(
    payload: ServiceItemCreate,
    session: SessionDep,
    context: CanManageService,
    tenant_id: TenantId,
) -> ServiceItemRead:
    """`code` is what links this to the rest of the system: a diagnostics
    catalogue entry or a clinical order with the same code prices against it."""
    item = await service.create_service_item(session, payload, hospital_id=tenant_id)
    await session.commit()
    return ServiceItemRead.model_validate(item)


@services_router.get("", response_model=Page[ServiceItemRead], summary="The service list")
async def list_service_items(
    session: SessionDep,
    context: CanReadService,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    category: ChargeCategory | None = None,
    search: str | None = None,
    include_inactive: bool = False,
) -> Page[ServiceItemRead]:
    items, total = await service.list_service_items(
        session,
        params,
        hospital_id=tenant_id,
        category=category,
        search=search,
        include_inactive=include_inactive,
    )
    return Page.build(
        [ServiceItemRead.model_validate(item) for item in items], total=total, params=params
    )


@services_router.get("/{item_id}", response_model=ServiceItemRead, summary="Get a service")
async def get_service_item(
    item_id: uuid.UUID, session: SessionDep, context: CanReadService, tenant_id: TenantId
) -> ServiceItemRead:
    return ServiceItemRead.model_validate(
        await service.get_service_item(session, item_id, hospital_id=tenant_id)
    )


@services_router.patch("/{item_id}", response_model=ServiceItemRead, summary="Edit a service")
async def update_service_item(
    item_id: uuid.UUID,
    payload: ServiceItemUpdate,
    session: SessionDep,
    context: CanManageService,
    tenant_id: TenantId,
) -> ServiceItemRead:
    item = await service.get_service_item(session, item_id, hospital_id=tenant_id)
    updated = await service.update_service_item(session, item, payload)
    await session.commit()
    return ServiceItemRead.model_validate(updated)


@services_router.put(
    "/{item_id}/prices", response_model=ServicePriceRead, summary="Set a price on a rate card"
)
async def set_price(
    item_id: uuid.UUID,
    payload: ServicePriceUpsert,
    session: SessionDep,
    context: CanManageRateCard,
    tenant_id: TenantId,
) -> ServicePriceRead:
    """The same service costs different amounts to a cash patient, an insurer
    and a scheme. This is where those numbers live."""
    item = await service.get_service_item(session, item_id, hospital_id=tenant_id)
    price = await service.set_price(session, item, payload)
    await session.commit()
    return ServicePriceRead.model_validate(price)


@services_router.get(
    "/{item_id}/prices", response_model=list[ServicePriceRead], summary="Prices for a service"
)
async def list_prices(
    item_id: uuid.UUID, session: SessionDep, context: CanReadRateCard, tenant_id: TenantId
) -> list[ServicePriceRead]:
    await service.get_service_item(session, item_id, hospital_id=tenant_id)
    rows = await service.list_prices(session, item_id)
    return [ServicePriceRead.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------
@charges_router.get("", response_model=Page[ChargeRead], summary="Charges, and unpriced items")
async def list_charges(
    session: SessionDep,
    context: CanReadCharge,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    encounter_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    charge_status: ChargeStatus | None = None,
    needs_pricing: bool | None = None,
) -> Page[ChargeRead]:
    """`needs_pricing=true` is the exception worklist: care that was delivered
    and that nobody has been able to put a price on."""
    charges, total = await service.list_charges(
        session,
        params,
        hospital_id=tenant_id,
        encounter_id=encounter_id,
        patient_id=patient_id,
        invoice_id=invoice_id,
        status=charge_status,
        needs_pricing=needs_pricing,
    )
    return Page.build(
        [ChargeRead.model_validate(charge) for charge in charges], total=total, params=params
    )


@charges_router.post(
    "", response_model=ChargeRead, status_code=status.HTTP_201_CREATED, summary="Add a charge"
)
async def add_charge(
    payload: ChargeAdd,
    session: SessionDep,
    context: CanAddCharge,
    tenant_id: TenantId,
) -> ChargeRead:
    """For what the event bus cannot see — a dressing, a consumable, an
    ambulance. Everything else is captured automatically."""
    charge = await service.add_charge(session, payload, hospital_id=tenant_id, actor=context.user)
    await session.commit()
    return ChargeRead.model_validate(charge)


@charges_router.post(
    "/{charge_id}/discount", response_model=ChargeRead, summary="Discount a charge"
)
async def discount_charge(
    charge_id: uuid.UUID,
    payload: DiscountRequest,
    session: SessionDep,
    request: Request,
    context: CanDiscount,
    tenant_id: TenantId,
) -> ChargeRead:
    charge = await service.get_charge(session, charge_id, hospital_id=tenant_id)
    updated = await service.discount_charge(
        session, charge, amount=payload.amount, reason=payload.reason, actor=context.user
    )
    await identity_service.record_audit(
        session,
        action="billing.charge.discount",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="charge",
        resource_id=charge.id,
        changes={"amount": str(payload.amount), "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return ChargeRead.model_validate(updated)


@charges_router.post("/{charge_id}/waive", response_model=ChargeRead, summary="Waive a charge")
async def waive_charge(
    charge_id: uuid.UUID,
    payload: ReasonRequest,
    session: SessionDep,
    request: Request,
    context: CanWaive,
    tenant_id: TenantId,
) -> ChargeRead:
    """The care stays on the clinical record; the money does not. Audited with
    the amount, because this is the lever somebody would pull to make money
    disappear."""
    charge = await service.get_charge(session, charge_id, hospital_id=tenant_id)
    updated = await service.waive_charge(session, charge, reason=payload.reason, actor=context.user)
    await identity_service.record_audit(
        session,
        action="billing.charge.waive",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="charge",
        resource_id=charge.id,
        changes={
            "amount": str(charge.total_amount),
            "description": charge.description,
            "reason": payload.reason,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return ChargeRead.model_validate(updated)


# ---------------------------------------------------------------------------
# The counter's screen
# ---------------------------------------------------------------------------
@accounts_router.get(
    "", response_model=Page[AccountBoardEntry], summary="Visits with money outstanding"
)
async def accounts_board(
    session: SessionDep,
    context: CanReadCharge,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[AccountBoardEntry]:
    """The cash counter's board: every visit that still owes something, oldest
    debt first.

    Two bulk lookups decorate the page — the visits through `clinical.service`,
    the patients through `patients.service` — so the cost is flat in the number
    of rows rather than three queries per row.
    """
    accounts, total = await service.list_open_accounts(session, params, hospital_id=tenant_id)
    encounters = await clinical_service.get_encounters_by_ids(
        session, [account.encounter_id for account in accounts], hospital_id=tenant_id
    )
    patients = await patients_service.get_patients_by_ids(
        session,
        [
            encounter.patient_id
            for encounter in (encounters.get(a.encounter_id) for a in accounts)
            if encounter is not None
        ],
        hospital_id=tenant_id,
    )

    board: list[AccountBoardEntry] = []
    for account in accounts:
        entry = AccountBoardEntry(
            encounter_id=account.encounter_id,
            pending_total=account.pending_total,
            invoiced_balance=account.invoiced_balance,
            balance_due=account.balance_due,
            has_unpriced_items=account.has_unpriced_items,
            outstanding_since=account.outstanding_since,
        )
        encounter = encounters.get(account.encounter_id)
        if encounter is not None:
            entry.encounter_number = encounter.encounter_number
            entry.encounter_status = encounter.status.value
            entry.patient_id = encounter.patient_id

            patient = patients.get(encounter.patient_id)
            if patient is not None:
                view = PatientRead.model_validate(patient)
                entry.patient_name = view.full_name
                entry.patient_uhid = view.uhid
                entry.patient_age_years = view.age_years
                entry.patient_gender = str(view.gender)
                entry.patient_is_deceased = view.is_deceased
        board.append(entry)

    return Page.build(board, total=total, params=params)


@accounts_router.get("/{encounter_id}", response_model=VisitAccount, summary="A visit's account")
async def visit_account(
    encounter_id: uuid.UUID, session: SessionDep, context: CanReadCharge, tenant_id: TenantId
) -> VisitAccount:
    """Everything the cash counter needs about one visit, in one round trip."""
    encounter = await clinical_service.get_encounter(session, encounter_id, hospital_id=tenant_id)
    account = await service.visit_account(session, encounter, hospital_id=tenant_id)
    return VisitAccount(
        encounter_id=account.encounter_id,
        patient_id=account.patient_id,
        uhid=account.uhid,
        patient_name=account.patient_name,
        encounter_number=account.encounter_number,
        encounter_status=account.encounter_status,
        pending_charges=[ChargeRead.model_validate(row) for row in account.pending_charges],
        invoices=[InvoiceSummary.model_validate(row) for row in account.invoices],
        pending_total=account.pending_total,
        invoiced_total=account.invoiced_total,
        paid_total=account.paid_total,
        balance_due=account.balance_due,
        has_unpriced_items=account.has_unpriced_items,
    )


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
@invoices_router.post(
    "",
    response_model=InvoiceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Assemble a draft invoice",
)
async def assemble_draft(
    payload: InvoiceDraftRequest,
    session: SessionDep,
    context: CanCreateInvoice,
    tenant_id: TenantId,
) -> InvoiceRead:
    """Gathers every pending charge on the visit. Re-runnable: called again
    after a later charge lands, it pulls the new line onto the same draft
    rather than opening a second one."""
    invoice = await service.assemble_draft(
        session, payload, hospital_id=tenant_id, actor=context.user
    )
    await session.commit()
    return await _render(session, invoice, hospital_id=tenant_id)


@invoices_router.get("", response_model=Page[InvoiceSummary], summary="The invoice worklist")
async def list_invoices(
    session: SessionDep,
    context: CanReadInvoice,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    encounter_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    invoice_status: InvoiceStatus | None = None,
    unpaid_only: bool = False,
    awaiting_settlement: bool = False,
) -> Page[InvoiceSummary]:
    """`awaiting_settlement=true` is the bills left behind by a death, a referral
    or a patient who walked out (CLAUDE.md §6)."""
    invoices, total = await service.list_invoices(
        session,
        params,
        hospital_id=tenant_id,
        encounter_id=encounter_id,
        patient_id=patient_id,
        status=invoice_status,
        unpaid_only=unpaid_only,
        awaiting_settlement=awaiting_settlement,
    )
    return Page.build(
        [InvoiceSummary.model_validate(invoice) for invoice in invoices],
        total=total,
        params=params,
    )


@invoices_router.get("/{invoice_id}", response_model=InvoiceRead, summary="An invoice in full")
async def get_invoice(
    invoice_id: uuid.UUID, session: SessionDep, context: CanReadInvoice, tenant_id: TenantId
) -> InvoiceRead:
    invoice = await service.get_invoice(session, invoice_id, hospital_id=tenant_id)
    return await _render(session, invoice, hospital_id=tenant_id)


@invoices_router.post(
    "/{invoice_id}/issue", response_model=InvoiceRead, summary="Issue the invoice"
)
async def issue_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceIssueRequest,
    session: SessionDep,
    request: Request,
    context: CanIssueInvoice,
    tenant_id: TenantId,
) -> InvoiceRead:
    """One-way door: after this the lines are frozen and the patient has a
    printout. A correction is a credit note, not an edit."""
    invoice = await service.get_invoice(session, invoice_id, hospital_id=tenant_id)
    issued = await service.issue_invoice(
        session,
        invoice,
        actor=context.user,
        hospital_id=tenant_id,
        notes=payload.notes,
    )
    await identity_service.record_audit(
        session,
        action="billing.invoice.issue",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="invoice",
        resource_id=invoice.id,
        changes={
            "invoice_number": issued.invoice_number,
            "grand_total": str(issued.grand_total),
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, issued, hospital_id=tenant_id)


@invoices_router.post(
    "/{invoice_id}/cancel", response_model=InvoiceRead, summary="Cancel an invoice"
)
async def cancel_invoice(
    invoice_id: uuid.UUID,
    payload: ReasonRequest,
    session: SessionDep,
    request: Request,
    context: CanCancelInvoice,
    tenant_id: TenantId,
) -> InvoiceRead:
    """Releases the charges back onto the running bill — the care still happened
    and still belongs on whatever invoice replaces this one."""
    invoice = await service.get_invoice(session, invoice_id, hospital_id=tenant_id)
    cancelled = await service.cancel_invoice(
        session, invoice, reason=payload.reason, actor=context.user, hospital_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="billing.invoice.cancel",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="invoice",
        resource_id=invoice.id,
        changes={"invoice_number": invoice.invoice_number, "reason": payload.reason},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, cancelled, hospital_id=tenant_id)


@invoices_router.post(
    "/{invoice_id}/write-off", response_model=InvoiceRead, summary="Write off an unpaid invoice"
)
async def write_off_invoice(
    invoice_id: uuid.UUID,
    payload: ReasonRequest,
    session: SessionDep,
    request: Request,
    context: CanWriteOff,
    tenant_id: TenantId,
) -> InvoiceRead:
    """Bad debt. Terminal, and it releases the visit's closure gate — which is
    how an absconded patient's encounter stops sitting open forever."""
    invoice = await service.get_invoice(session, invoice_id, hospital_id=tenant_id)
    written_off = await service.write_off_invoice(
        session, invoice, reason=payload.reason, actor=context.user, hospital_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="billing.invoice.write_off",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="invoice",
        resource_id=invoice.id,
        changes={
            "invoice_number": invoice.invoice_number,
            "amount": str(invoice.grand_total),
            "reason": payload.reason,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return await _render(session, written_off, hospital_id=tenant_id)


@invoices_router.post(
    "/{invoice_id}/payments",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment",
)
async def record_payment(
    invoice_id: uuid.UUID,
    payload: PaymentRequest,
    session: SessionDep,
    request: Request,
    context: CanRecordPayment,
    tenant_id: TenantId,
) -> PaymentRead:
    """UPI first (CLAUDE.md §9). Clearing the last balance closes the visit by
    itself, exactly as verifying the last lab report does."""
    invoice = await service.get_invoice(session, invoice_id, hospital_id=tenant_id)
    payment, _ = await service.record_payment(
        session,
        invoice,
        amount=payload.amount,
        method=payload.method,
        actor=context.user,
        hospital_id=tenant_id,
        reference=payload.reference,
        payer_name=payload.payer_name,
        received_at=payload.received_at,
        notes=payload.notes,
    )
    await identity_service.record_audit(
        session,
        action="billing.payment.record",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="payment",
        resource_id=payment.id,
        changes={
            "receipt_number": payment.receipt_number,
            "amount": str(payment.amount),
            "method": payment.method.value,
            "reference": payment.reference,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return PaymentRead.model_validate(payment)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
@payments_router.get("", response_model=Page[PaymentRead], summary="Payments and collections")
async def list_payments(
    session: SessionDep,
    context: CanReadPayment,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    invoice_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    method: PaymentMethod | None = None,
    since: datetime | None = None,
) -> Page[PaymentRead]:
    """With `method` and `since`, this is the shift's collection report."""
    payments, total = await service.list_payments(
        session,
        params,
        hospital_id=tenant_id,
        invoice_id=invoice_id,
        patient_id=patient_id,
        method=method,
        since=since,
    )
    return Page.build(
        [PaymentRead.model_validate(payment) for payment in payments], total=total, params=params
    )


@payments_router.get("/{payment_id}", response_model=PaymentRead, summary="A receipt")
async def get_payment(
    payment_id: uuid.UUID, session: SessionDep, context: CanReadPayment, tenant_id: TenantId
) -> PaymentRead:
    return PaymentRead.model_validate(
        await service.get_payment(session, payment_id, hospital_id=tenant_id)
    )


@payments_router.post(
    "/{payment_id}/reverse", response_model=PaymentRead, summary="Reverse a payment"
)
async def reverse_payment(
    payment_id: uuid.UUID,
    payload: ReversalRequest,
    session: SessionDep,
    request: Request,
    context: CanReversePayment,
    tenant_id: TenantId,
) -> PaymentRead:
    """Writes a second, reversed row. The original receipt stays exactly as it
    was printed, because the cash drawer reconciles against what happened."""
    payment = await service.get_payment(session, payment_id, hospital_id=tenant_id)
    reversed_payment, _ = await service.reverse_payment(
        session, payment, reason=payload.reason, actor=context.user, hospital_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="billing.payment.reverse",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="payment",
        resource_id=payment.id,
        changes={
            "receipt_number": payment.receipt_number,
            "amount": str(payment.amount),
            "reason": payload.reason,
        },
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return PaymentRead.model_validate(reversed_payment)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
@claims_router.post(
    "", response_model=ClaimRead, status_code=status.HTTP_201_CREATED, summary="Raise a claim"
)
async def raise_claim(
    payload: ClaimCreate,
    session: SessionDep,
    context: CanManageClaim,
    tenant_id: TenantId,
) -> ClaimRead:
    claim = await service.raise_claim(session, payload, hospital_id=tenant_id, actor=context.user)
    await session.commit()
    return ClaimRead.model_validate(claim)


@claims_router.get("", response_model=Page[ClaimRead], summary="The claims worklist")
async def list_claims(
    session: SessionDep,
    context: CanReadClaim,
    tenant_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
    claim_status: ClaimStatus | None = None,
    patient_id: uuid.UUID | None = None,
) -> Page[ClaimRead]:
    claims, total = await service.list_claims(
        session, params, hospital_id=tenant_id, status=claim_status, patient_id=patient_id
    )
    return Page.build(
        [ClaimRead.model_validate(claim) for claim in claims], total=total, params=params
    )


@claims_router.get("/{claim_id}", response_model=ClaimRead, summary="Get a claim")
async def get_claim(
    claim_id: uuid.UUID, session: SessionDep, context: CanReadClaim, tenant_id: TenantId
) -> ClaimRead:
    return ClaimRead.model_validate(
        await service.get_claim(session, claim_id, hospital_id=tenant_id)
    )


@claims_router.patch("/{claim_id}", response_model=ClaimRead, summary="Progress a claim")
async def progress_claim(
    claim_id: uuid.UUID,
    payload: ClaimUpdate,
    session: SessionDep,
    request: Request,
    context: CanManageClaim,
    tenant_id: TenantId,
) -> ClaimRead:
    """Settling a claim records the insurer's money against the invoice — from
    the hospital's side that is exactly what it is."""
    claim = await service.get_claim(session, claim_id, hospital_id=tenant_id)
    updated = await service.progress_claim(
        session, claim, payload, actor=context.user, hospital_id=tenant_id
    )
    await identity_service.record_audit(
        session,
        action="billing.claim.progress",
        actor=context.user,
        hospital_id=tenant_id,
        resource_type="insurance_claim",
        resource_id=claim.id,
        changes={"status": payload.status.value, "settled": str(updated.settled_amount)},
        ip_address=await get_client_ip(request),
    )
    await session.commit()
    return ClaimRead.model_validate(updated)
