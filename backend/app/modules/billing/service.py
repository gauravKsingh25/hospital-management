"""Billing business logic — the module's public interface.

The shape of this module follows one sentence in CLAUDE.md §7b: *"charges flow
automatically from consultation, lab, radiology, pharmacy via the event bus;
reception reviews the invoice, never rebuilds it."* Everything here exists to
make the second half of that true, and `capture_charge` is where it happens.

Three properties are load-bearing, and each one is a guard against a specific
way hospitals lose money:

**Capture is idempotent.** Keyed on (source module, source type, source id) with
a partial unique index behind it. A re-delivered event, a double-click, a
retried request — all collapse onto the same row. Billing a patient twice for
one CBC is worse than not billing them at all, because the first is discovered
by the patient at the counter and the second by an accountant in a quiet month.

**Capture is total.** It never raises for missing configuration. No rate card, no
price, no mapped service — the charge lands anyway, at zero, flagged
`needs_pricing`. This is deliberate and it is the whole reason `billing` can
subscribe transactionally without ever blocking clinical work: a hospital that
has not finished setting up its price list must still be able to order a blood
test. A visible zero is recoverable; a missing row is invisible revenue loss.

**Issued invoices are frozen.** Every mutation checks. After `ISSUED` the patient
is holding a printout, and a document that changes afterwards is not evidence of
anything.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# `sa_select` is SQLAlchemy's own `select`. SQLModel's is typed for up to four
# clauses and biased towards model classes; the open-accounts aggregate selects
# five labelled columns. Everything else in this module stays on the SQLModel
# one, imported below.
from sqlalchemy import ColumnElement, Select, func, literal, text, union_all
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.events import event_bus
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.models import utc_now
from app.core.pagination import MAX_PAGE_SIZE, PageParams
from app.modules.billing.events import (
    ChargeCaptured,
    ChargeNeedsPricing,
    InvoiceIssued,
    InvoicePaid,
    PaymentReceived,
    PaymentReversed,
    SettlementRequired,
)
from app.modules.billing.models import (
    Charge,
    ChargeCategory,
    ChargeStatus,
    ClaimStatus,
    InsuranceClaim,
    Invoice,
    InvoiceSequence,
    InvoiceStatus,
    PayerType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    RateCard,
    ServiceItem,
    ServicePrice,
)
from app.modules.billing.schemas import (
    ChargeAdd,
    ClaimCreate,
    ClaimUpdate,
    InvoiceDraftRequest,
    RateCardCreate,
    RateCardUpdate,
    ServiceItemCreate,
    ServiceItemUpdate,
    ServicePriceUpsert,
)
from app.modules.billing.tax import ZERO, LineTax, compute_line, money, sum_lines
from app.modules.billing.transitions import (
    assert_claim_transition,
    assert_invoice_transition,
    assert_payment_transition,
)
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter
from app.modules.identity.models import User
from app.modules.patients import service as patients_service
from app.modules.tenancy import service as tenancy_service

logger = logging.getLogger(__name__)

__all__ = [
    "add_charge",
    "assemble_draft",
    "cancel_charges_for_source",
    "cancel_invoice",
    "capture_charge",
    "create_rate_card",
    "create_service_item",
    "discount_charge",
    "get_charge",
    "get_invoice",
    "get_payment",
    "get_rate_card_by_code",
    "get_service_item_by_code",
    "issue_invoice",
    "list_charges",
    "list_invoices",
    "list_open_accounts",
    "list_payments",
    "open_balance",
    "raise_claim",
    "record_payment",
    "resolve_rate_card",
    "reverse_payment",
    "set_price",
    "settle_on_closure",
    "visit_account",
    "waive_charge",
    "write_off_invoice",
]

# Charge statuses that still represent money the hospital expects.
_LIVE_CHARGE_STATUSES: tuple[ChargeStatus, ...] = (ChargeStatus.PENDING, ChargeStatus.INVOICED)
_OPEN_INVOICE_STATUSES: tuple[InvoiceStatus, ...] = (
    InvoiceStatus.DRAFT,
    InvoiceStatus.ISSUED,
    InvoiceStatus.PARTIALLY_PAID,
)


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------
def financial_year(moment: date) -> int:
    """The starting calendar year of India's financial year (April - March).

    A date in March 2027 belongs to FY 2026-27, so this returns 2026 for it.
    Invoice numbers restart here rather than on 1 January because rule 46 of the
    CGST Rules wants a series unique for a *financial* year.
    """
    return moment.year if moment.month >= 4 else moment.year - 1


async def _next_number(session: AsyncSession, hospital_id: uuid.UUID, kind: str) -> str:
    """`INV-2627-000001` / `RCP-2627-000001`, per hospital per financial year.

    Upsert-then-lock, the same shape as every other counter here: a plain
    check-then-insert races on the first allocation of the year, which for
    invoices is 1 April — a day the counter is certainly busy.
    """
    year = financial_year(utc_now().date())
    await session.execute(
        text(
            """
            INSERT INTO invoice_sequences
                (id, hospital_id, financial_year, kind, last_value, created_at, updated_at)
            VALUES (:id, :hospital_id, :year, :kind, 0, now(), now())
            ON CONFLICT (hospital_id, financial_year, kind) DO NOTHING
            """
        ),
        {"id": new_id(), "hospital_id": hospital_id, "year": year, "kind": kind},
    )
    row = (
        (
            await session.execute(
                select(InvoiceSequence)
                .where(
                    col(InvoiceSequence.hospital_id) == hospital_id,
                    col(InvoiceSequence.financial_year) == year,
                    col(InvoiceSequence.kind) == kind,
                )
                .with_for_update()
            )
        )
        .scalars()
        .one()
    )
    row.last_value += 1
    session.add(row)
    await session.flush()
    # FY 2026-27 renders as "2627" — short enough to read down a phone line and
    # unambiguous about which year's series an invoice belongs to.
    label = f"{year % 100:02d}{(year + 1) % 100:02d}"
    return f"{kind}-{label}-{row.last_value:06d}"


# ---------------------------------------------------------------------------
# Rate cards
# ---------------------------------------------------------------------------
async def create_rate_card(
    session: AsyncSession, payload: RateCardCreate, *, hospital_id: uuid.UUID
) -> RateCard:
    code = payload.code.strip().upper()
    if await get_rate_card_by_code(session, code, hospital_id=hospital_id) is not None:
        raise ConflictError(
            f"A rate card with code '{code}' already exists.", code="rate_card_code_taken"
        )

    card = RateCard(hospital_id=hospital_id, **{**payload.model_dump(), "code": code})
    session.add(card)
    await session.flush()

    if card.is_default:
        await _make_sole_default(session, card)
    await session.refresh(card)
    return card


async def _make_sole_default(session: AsyncSession, card: RateCard) -> None:
    """Exactly one default per hospital.

    Enforced here rather than by a unique index for the same reason as the
    primary diagnosis in `clinical`: "make *that* one the default" has to be a
    single call. A unique index would force the caller into clear-then-set,
    which is a sequence a user can leave half finished — and a hospital with no
    default rate card prices nothing.
    """
    others = (
        (
            await session.execute(
                select(RateCard).where(
                    col(RateCard.hospital_id) == card.hospital_id,
                    col(RateCard.id) != card.id,
                    col(RateCard.is_default).is_(True),
                    col(RateCard.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for other in others:
        other.is_default = False
        other.updated_at = utc_now()
        session.add(other)
    card.is_default = True
    session.add(card)
    await session.flush()


async def get_rate_card(
    session: AsyncSession, card_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> RateCard:
    card = (
        (
            await session.execute(
                select(RateCard).where(
                    col(RateCard.id) == card_id,
                    col(RateCard.hospital_id) == hospital_id,
                    col(RateCard.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if card is None:
        raise NotFoundError("Rate card not found.", code="rate_card_not_found")
    return card


async def get_rate_card_by_code(
    session: AsyncSession, code: str, *, hospital_id: uuid.UUID
) -> RateCard | None:
    return (
        (
            await session.execute(
                select(RateCard).where(
                    col(RateCard.hospital_id) == hospital_id,
                    col(RateCard.code) == code.strip().upper(),
                    col(RateCard.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def list_rate_cards(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    payer_type: PayerType | None = None,
    include_inactive: bool = False,
) -> tuple[list[RateCard], int]:
    filters: list[ColumnElement[bool]] = [
        col(RateCard.hospital_id) == hospital_id,
        col(RateCard.deleted_at).is_(None),
    ]
    if payer_type is not None:
        filters.append(col(RateCard.payer_type) == payer_type)
    if not include_inactive:
        filters.append(col(RateCard.is_active).is_(True))

    total = (
        await session.execute(select(func.count()).select_from(RateCard).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(RateCard)
                .where(*filters)
                .order_by(col(RateCard.is_default).desc(), col(RateCard.name))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def update_rate_card(
    session: AsyncSession, card: RateCard, payload: RateCardUpdate
) -> RateCard:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(card, field, value)
    card.updated_at = utc_now()
    session.add(card)
    await session.flush()
    await session.refresh(card)
    return card


async def set_default_rate_card(session: AsyncSession, card: RateCard) -> RateCard:
    await _make_sole_default(session, card)
    await session.refresh(card)
    return card


async def get_default_rate_card(
    session: AsyncSession, *, hospital_id: uuid.UUID
) -> RateCard | None:
    return (
        (
            await session.execute(
                select(RateCard).where(
                    col(RateCard.hospital_id) == hospital_id,
                    col(RateCard.is_default).is_(True),
                    col(RateCard.is_active).is_(True),
                    col(RateCard.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def resolve_rate_card(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    preferred_id: uuid.UUID | None = None,
    on_date: date | None = None,
) -> RateCard | None:
    """Which price list applies. Returns None when none is configured.

    Returning None rather than raising is the choice that keeps capture total: a
    hospital mid-setup has no rate card, and clinical work must not stop for it.
    """
    today = on_date or utc_now().date()

    if preferred_id is not None:
        card = await get_rate_card(session, preferred_id, hospital_id=hospital_id)
        if _is_current(card, today):
            return card
        # A card whose validity window has passed is a configuration mistake,
        # not a reason to refuse the patient. Fall through to the default and
        # let the price land on the exception list if it cannot be found.
        logger.warning("rate card %s is outside its validity window", card.code)

    return await get_default_rate_card(session, hospital_id=hospital_id)


def _is_current(card: RateCard, today: date) -> bool:
    if not card.is_active:
        return False
    if card.valid_from and today < card.valid_from:
        return False
    return not (card.valid_to and today > card.valid_to)


# ---------------------------------------------------------------------------
# Services and prices
# ---------------------------------------------------------------------------
async def create_service_item(
    session: AsyncSession, payload: ServiceItemCreate, *, hospital_id: uuid.UUID
) -> ServiceItem:
    code = payload.code.strip().upper()
    if await get_service_item_by_code(session, code, hospital_id=hospital_id) is not None:
        raise ConflictError(
            f"A service with code '{code}' already exists.", code="service_code_taken"
        )
    item = ServiceItem(hospital_id=hospital_id, **{**payload.model_dump(), "code": code})
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item


async def get_service_item(
    session: AsyncSession, item_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> ServiceItem:
    item = (
        (
            await session.execute(
                select(ServiceItem).where(
                    col(ServiceItem.id) == item_id,
                    col(ServiceItem.hospital_id) == hospital_id,
                    col(ServiceItem.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if item is None:
        raise NotFoundError("Service not found.", code="service_not_found")
    return item


async def get_service_item_by_code(
    session: AsyncSession, code: str, *, hospital_id: uuid.UUID
) -> ServiceItem | None:
    return (
        (
            await session.execute(
                select(ServiceItem).where(
                    col(ServiceItem.hospital_id) == hospital_id,
                    col(ServiceItem.code) == code.strip().upper(),
                    col(ServiceItem.is_active).is_(True),
                    col(ServiceItem.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def list_service_items(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    category: ChargeCategory | None = None,
    search: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[ServiceItem], int]:
    filters: list[ColumnElement[bool]] = [
        col(ServiceItem.hospital_id) == hospital_id,
        col(ServiceItem.deleted_at).is_(None),
    ]
    if category is not None:
        filters.append(col(ServiceItem.category) == category)
    if not include_inactive:
        filters.append(col(ServiceItem.is_active).is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(col(ServiceItem.name).ilike(pattern) | col(ServiceItem.code).ilike(pattern))

    total = (
        await session.execute(select(func.count()).select_from(ServiceItem).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(ServiceItem)
                .where(*filters)
                .order_by(col(ServiceItem.category), col(ServiceItem.name))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def update_service_item(
    session: AsyncSession, item: ServiceItem, payload: ServiceItemUpdate
) -> ServiceItem:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    if item.is_gst_exempt:
        item.gst_rate = ZERO
    item.updated_at = utc_now()
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return item


async def set_price(
    session: AsyncSession, item: ServiceItem, payload: ServicePriceUpsert
) -> ServicePrice:
    """Set or update what one service costs on one rate card."""
    await get_rate_card(session, payload.rate_card_id, hospital_id=item.hospital_id)

    existing = (
        (
            await session.execute(
                select(ServicePrice).where(
                    col(ServicePrice.service_item_id) == item.id,
                    col(ServicePrice.rate_card_id) == payload.rate_card_id,
                    col(ServicePrice.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        existing.price = money(payload.price)
        existing.is_package = payload.is_package
        existing.notes = payload.notes
        existing.updated_at = utc_now()
        session.add(existing)
        await session.flush()
        await session.refresh(existing)
        return existing

    price = ServicePrice(
        hospital_id=item.hospital_id,
        service_item_id=item.id,
        rate_card_id=payload.rate_card_id,
        price=money(payload.price),
        is_package=payload.is_package,
        notes=payload.notes,
    )
    session.add(price)
    await session.flush()
    await session.refresh(price)
    return price


async def list_prices(session: AsyncSession, service_item_id: uuid.UUID) -> list[ServicePrice]:
    rows = (
        (
            await session.execute(
                select(ServicePrice).where(
                    col(ServicePrice.service_item_id) == service_item_id,
                    col(ServicePrice.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def lookup_price(
    session: AsyncSession, *, service_item_id: uuid.UUID, rate_card_id: uuid.UUID
) -> ServicePrice | None:
    return (
        (
            await session.execute(
                select(ServicePrice).where(
                    col(ServicePrice.service_item_id) == service_item_id,
                    col(ServicePrice.rate_card_id) == rate_card_id,
                    col(ServicePrice.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# Charge capture — the heart of the module
# ---------------------------------------------------------------------------
async def find_charge_for_source(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    source_module: str,
    source_type: str,
    source_id: uuid.UUID,
) -> Charge | None:
    """The live charge for one act, if it has already been captured."""
    return (
        (
            await session.execute(
                select(Charge).where(
                    col(Charge.hospital_id) == hospital_id,
                    col(Charge.source_module) == source_module,
                    col(Charge.source_type) == source_type,
                    col(Charge.source_id) == source_id,
                    col(Charge.status) != ChargeStatus.CANCELLED,
                    col(Charge.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def capture_charge(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    encounter_id: uuid.UUID,
    patient_id: uuid.UUID,
    source_module: str,
    source_type: str,
    source_id: uuid.UUID,
    description: str,
    category: ChargeCategory,
    item_code: str | None = None,
    doctor_id: uuid.UUID | None = None,
    quantity: Decimal = Decimal("1"),
    unit_price: Decimal | None = None,
    discount_amount: Decimal = ZERO,
    discount_reason: str | None = None,
    actor_id: uuid.UUID | None = None,
    rate_card: RateCard | None = None,
) -> Charge:
    """Put a billable act on a visit's running bill.

    **Idempotent**: called twice for the same source act, it returns the charge
    that already exists rather than creating a second one. This is not a nicety.
    Transactional subscribers can be re-entered by a retry, and the failure mode
    it prevents — a patient billed twice for one blood test — is one they find
    out about at the counter.

    **Total**: it never raises because billing is unconfigured. No rate card, no
    mapped service, no price on the card — the charge is recorded at zero with
    `needs_pricing` set, and it lands on an exception worklist where somebody
    can price it. That is what allows this to run inside a clinical transaction
    without ever being the reason an order fails.
    """
    existing = await find_charge_for_source(
        session,
        hospital_id=hospital_id,
        source_module=source_module,
        source_type=source_type,
        source_id=source_id,
    )
    if existing is not None:
        logger.debug("charge already captured for %s/%s/%s", source_module, source_type, source_id)
        return existing

    item = (
        await get_service_item_by_code(session, item_code, hospital_id=hospital_id)
        if item_code
        else None
    )
    if item is None and doctor_id is not None and category is ChargeCategory.CONSULTATION:
        item = await _consultation_item_for(session, hospital_id=hospital_id, doctor_id=doctor_id)
    if item is None:
        item = await _default_item_for(session, hospital_id=hospital_id, category=category)

    card = rate_card or await resolve_rate_card(session, hospital_id=hospital_id)

    resolved_price: Decimal | None = money(unit_price) if unit_price is not None else None
    if resolved_price is None and item is not None and card is not None:
        price_row = await lookup_price(session, service_item_id=item.id, rate_card_id=card.id)
        if price_row is not None:
            resolved_price = price_row.price

    needs_pricing = resolved_price is None
    effective_price = resolved_price if resolved_price is not None else ZERO

    line = compute_line(
        unit_price=effective_price,
        quantity=quantity,
        discount_amount=discount_amount,
        gst_rate=item.gst_rate if item else ZERO,
        is_exempt=item.is_gst_exempt if item else True,
    )

    charge = Charge(
        hospital_id=hospital_id,
        encounter_id=encounter_id,
        patient_id=patient_id,
        source_module=source_module,
        source_type=source_type,
        source_id=source_id,
        service_item_id=item.id if item else None,
        rate_card_id=card.id if card else None,
        category=item.category if item else category,
        description=(item.name if item else description)[:250],
        quantity=Decimal(quantity),
        unit_price=effective_price,
        discount_amount=money(min(discount_amount, money(effective_price) * Decimal(quantity))),
        discount_reason=discount_reason,
        taxable_amount=line.taxable_amount,
        gst_rate=line.gst_rate,
        cgst_amount=line.cgst_amount,
        sgst_amount=line.sgst_amount,
        igst_amount=line.igst_amount,
        total_amount=line.total_amount,
        hsn_sac_code=item.hsn_sac_code if item else None,
        status=ChargeStatus.PENDING,
        needs_pricing=needs_pricing,
        captured_at=utc_now(),
        captured_by_id=actor_id,
    )
    session.add(charge)
    await session.flush()
    await session.refresh(charge)

    await event_bus.publish(
        ChargeCaptured(
            hospital_id=hospital_id,
            actor_id=actor_id,
            charge_id=charge.id,
            encounter_id=encounter_id,
            patient_id=patient_id,
            category=charge.category.value,
            description=charge.description,
            total_amount=charge.total_amount,
        ),
        session=session,
    )
    if needs_pricing:
        # Loud on purpose. Care delivered at an unknown price is a hole in the
        # month's revenue, and the only thing worse than finding it late is not
        # finding it.
        logger.warning(
            "charge captured without a price: hospital=%s item_code=%s description=%s",
            hospital_id,
            item_code,
            charge.description,
        )
        await event_bus.publish(
            ChargeNeedsPricing(
                hospital_id=hospital_id,
                actor_id=actor_id,
                charge_id=charge.id,
                encounter_id=encounter_id,
                description=charge.description,
                source_module=source_module,
                item_code=item_code,
            ),
            session=session,
        )
    return charge


async def _consultation_item_for(
    session: AsyncSession, *, hospital_id: uuid.UUID, doctor_id: uuid.UUID
) -> ServiceItem | None:
    """This consultant's own fee, if one is configured."""
    return (
        (
            await session.execute(
                select(ServiceItem).where(
                    col(ServiceItem.hospital_id) == hospital_id,
                    col(ServiceItem.doctor_id) == doctor_id,
                    col(ServiceItem.category) == ChargeCategory.CONSULTATION,
                    col(ServiceItem.is_active).is_(True),
                    col(ServiceItem.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def _default_item_for(
    session: AsyncSession, *, hospital_id: uuid.UUID, category: ChargeCategory
) -> ServiceItem | None:
    """The hospital-wide fallback for a category — the unnamed consultant's fee.

    Only ever a service with no doctor attached, so a specific consultant's rate
    can never be silently applied to somebody else's patient.
    """
    return (
        (
            await session.execute(
                select(ServiceItem)
                .where(
                    col(ServiceItem.hospital_id) == hospital_id,
                    col(ServiceItem.category) == category,
                    col(ServiceItem.doctor_id).is_(None),
                    col(ServiceItem.is_active).is_(True),
                    col(ServiceItem.deleted_at).is_(None),
                )
                .order_by(col(ServiceItem.created_at))
            )
        )
        .scalars()
        .first()
    )


async def cancel_charges_for_source(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    source_module: str,
    source_type: str,
    source_id: uuid.UUID,
    reason: str,
) -> Charge | None:
    """Void the charge for an act that was called off.

    Refuses to touch an invoiced charge. Once a line is on a document the remedy
    is a credit note, not a quiet edit — otherwise an invoice and its own charges
    would stop agreeing, and the patient's printout would be the only honest copy.
    """
    charge = await find_charge_for_source(
        session,
        hospital_id=hospital_id,
        source_module=source_module,
        source_type=source_type,
        source_id=source_id,
    )
    if charge is None:
        return None
    if charge.status is not ChargeStatus.PENDING:
        logger.info("not cancelling charge %s: already %s", charge.id, charge.status.value.lower())
        return charge

    charge.status = ChargeStatus.CANCELLED
    charge.cancellation_reason = reason
    charge.updated_at = utc_now()
    session.add(charge)
    await session.flush()
    await session.refresh(charge)
    return charge


async def add_charge(
    session: AsyncSession, payload: ChargeAdd, *, hospital_id: uuid.UUID, actor: User
) -> Charge:
    """Add a line by hand — a dressing, a consumable, an ambulance.

    Given its own source id so it can never collide with an automatically
    captured act, and so two identical manual lines (two dressings, genuinely)
    both stand.
    """
    encounter = await clinical_service.get_encounter(
        session, payload.encounter_id, hospital_id=hospital_id
    )
    if encounter.is_closed and encounter.status.value == "CANCELLED":
        raise ConflictError(
            "This visit was cancelled; charges cannot be added to it.",
            code="encounter_cancelled",
        )

    item = (
        await get_service_item_by_code(session, payload.service_item_code, hospital_id=hospital_id)
        if payload.service_item_code
        else None
    )
    if payload.service_item_code and item is None:
        raise NotFoundError(
            f"No service with code '{payload.service_item_code}'.", code="service_not_found"
        )

    return await capture_charge(
        session,
        hospital_id=hospital_id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        source_module="billing",
        source_type="manual",
        source_id=new_id(),
        description=payload.description or (item.name if item else "Manual charge"),
        category=item.category if item else payload.category,
        item_code=item.code if item else None,
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        discount_amount=payload.discount_amount,
        discount_reason=payload.discount_reason,
        actor_id=actor.id,
    )


async def get_charge(
    session: AsyncSession, charge_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Charge:
    charge = (
        (
            await session.execute(
                select(Charge).where(
                    col(Charge.id) == charge_id,
                    col(Charge.hospital_id) == hospital_id,
                    col(Charge.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if charge is None:
        raise NotFoundError("Charge not found.", code="charge_not_found")
    return charge


async def list_charges(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    encounter_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    status: ChargeStatus | None = None,
    needs_pricing: bool | None = None,
) -> tuple[list[Charge], int]:
    """The running bill, and the unpriced-items exception worklist."""
    filters: list[ColumnElement[bool]] = [
        col(Charge.hospital_id) == hospital_id,
        col(Charge.deleted_at).is_(None),
    ]
    if encounter_id is not None:
        filters.append(col(Charge.encounter_id) == encounter_id)
    if patient_id is not None:
        filters.append(col(Charge.patient_id) == patient_id)
    if invoice_id is not None:
        filters.append(col(Charge.invoice_id) == invoice_id)
    if status is not None:
        filters.append(col(Charge.status) == status)
    if needs_pricing is not None:
        filters.append(col(Charge.needs_pricing).is_(needs_pricing))
        if needs_pricing:
            # An unpriced line that has been cancelled or waived needs nobody.
            filters.append(col(Charge.status) == ChargeStatus.PENDING)

    total = (
        await session.execute(select(func.count()).select_from(Charge).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Charge)
                .where(*filters)
                .order_by(col(Charge.captured_at))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


def _assert_charge_editable(charge: Charge) -> None:
    if charge.status is ChargeStatus.INVOICED:
        raise ConflictError(
            "This charge is on an invoice and can no longer be changed. "
            "Cancel the invoice, or raise a credit note.",
            code="charge_invoiced",
            details={"invoice_id": str(charge.invoice_id)},
        )
    if charge.status in (ChargeStatus.CANCELLED, ChargeStatus.WAIVED):
        raise ConflictError(
            f"This charge is already {charge.status.value.lower()}.", code="charge_closed"
        )


async def _reprice(session: AsyncSession, charge: Charge) -> Charge:
    """Recompute one line's tax after its price or discount changed."""
    item = (
        await get_service_item(session, charge.service_item_id, hospital_id=charge.hospital_id)
        if charge.service_item_id
        else None
    )
    line = compute_line(
        unit_price=charge.unit_price,
        quantity=charge.quantity,
        discount_amount=charge.discount_amount,
        gst_rate=item.gst_rate if item else charge.gst_rate,
        is_exempt=item.is_gst_exempt if item else charge.gst_rate <= 0,
    )
    charge.taxable_amount = line.taxable_amount
    charge.gst_rate = line.gst_rate
    charge.cgst_amount = line.cgst_amount
    charge.sgst_amount = line.sgst_amount
    charge.igst_amount = line.igst_amount
    charge.total_amount = line.total_amount
    charge.updated_at = utc_now()
    session.add(charge)
    await session.flush()
    await session.refresh(charge)
    return charge


async def price_charge(session: AsyncSession, charge: Charge, *, unit_price: Decimal) -> Charge:
    """Put a price on a line that was captured without one."""
    _assert_charge_editable(charge)
    charge.unit_price = money(unit_price)
    charge.needs_pricing = False
    return await _reprice(session, charge)


async def discount_charge(
    session: AsyncSession, charge: Charge, *, amount: Decimal, reason: str, actor: User
) -> Charge:
    """Reduce a line, with a reason attached.

    A discount larger than the line is refused rather than clamped: somebody
    meant to type something else, and silently turning ₹5,000 off a ₹500 line
    into ₹500 off hides the typo.
    """
    _assert_charge_editable(charge)
    gross = money(charge.unit_price * charge.quantity)
    if money(amount) > gross:
        raise ValidationError(
            f"A discount of {amount} is more than the charge of {gross}.",
            code="discount_exceeds_charge",
        )
    charge.discount_amount = money(amount)
    charge.discount_reason = reason
    return await _reprice(session, charge)


async def waive_charge(
    session: AsyncSession, charge: Charge, *, reason: str, actor: User
) -> Charge:
    """Decide this line is not owed.

    Distinct from cancelling: the act happened and stays on the clinical record;
    what changed is that nobody is paying for it. Held behind its own permission
    because it is the lever somebody would pull to make money disappear.
    """
    _assert_charge_editable(charge)
    charge.status = ChargeStatus.WAIVED
    charge.waiver_reason = reason
    charge.waived_by_id = actor.id
    charge.updated_at = utc_now()
    session.add(charge)
    await session.flush()
    await session.refresh(charge)
    return charge


# ---------------------------------------------------------------------------
# The outstanding balance — and the closure gate (CLAUDE.md §6)
# ---------------------------------------------------------------------------
async def open_balance(session: AsyncSession, encounter_id: uuid.UUID) -> Decimal:
    """What is still owed on a visit: uninvoiced charges plus unpaid invoices.

    Both halves are needed. A visit with charges nobody has invoiced owes money
    just as surely as one with an issued invoice nobody has paid — and the first
    is the more common way a patient walks out without paying.
    """
    pending = (
        await session.execute(
            select(func.coalesce(func.sum(col(Charge.total_amount)), 0)).where(
                col(Charge.encounter_id) == encounter_id,
                col(Charge.status) == ChargeStatus.PENDING,
                col(Charge.deleted_at).is_(None),
            )
        )
    ).scalar_one()

    invoiced = (
        await session.execute(
            select(func.coalesce(func.sum(col(Invoice.balance_due)), 0)).where(
                col(Invoice.encounter_id) == encounter_id,
                col(Invoice.status).in_(_OPEN_INVOICE_STATUSES),
                col(Invoice.deleted_at).is_(None),
            )
        )
    ).scalar_one()

    return money(Decimal(pending) + Decimal(invoiced))


@dataclass(frozen=True, slots=True)
class OpenAccount:
    """One visit that still owes the hospital money."""

    encounter_id: uuid.UUID
    pending_total: Decimal
    invoiced_balance: Decimal
    outstanding_since: datetime
    has_unpriced_items: bool

    @property
    def balance_due(self) -> Decimal:
        return money(self.pending_total + self.invoiced_balance)


async def list_open_accounts(
    session: AsyncSession, params: PageParams, *, hospital_id: uuid.UUID
) -> tuple[list[OpenAccount], int]:
    """Every visit with an open balance, oldest debt first — the counter's board.

    This is the cashier's entry point, and it is deliberately *not* the invoice
    list. A visit whose charges nobody has assembled into an invoice yet owes
    money just as surely as one with an issued invoice nobody has paid, and it
    is the more common way a patient walks out without paying — there is no
    document to notice the absence of. Listing invoices would show the counter
    only the debts somebody already did the paperwork for.

    One query, over billing's own two tables. It does not join `encounters`
    (CLAUDE.md §2): the caller decorates the page through `clinical.service`.
    Ordering by the oldest unsettled item rather than by visit time is a
    deliberate choice too — the person who has been standing at the counter
    longest should be at the top, which is not the same as the person who
    arrived first.
    """
    pending_part: Select[Any] = sa_select(
        col(Charge.encounter_id).label("encounter_id"),
        col(Charge.total_amount).label("pending"),
        literal(ZERO).label("invoiced"),
        col(Charge.captured_at).label("since"),
        col(Charge.needs_pricing).label("unpriced"),
    ).where(
        col(Charge.hospital_id) == hospital_id,
        col(Charge.status) == ChargeStatus.PENDING,
        col(Charge.deleted_at).is_(None),
    )
    invoiced_part: Select[Any] = sa_select(
        col(Invoice.encounter_id).label("encounter_id"),
        literal(ZERO).label("pending"),
        col(Invoice.balance_due).label("invoiced"),
        col(Invoice.created_at).label("since"),
        literal(False).label("unpriced"),
    ).where(
        col(Invoice.hospital_id) == hospital_id,
        col(Invoice.status).in_(_OPEN_INVOICE_STATUSES),
        col(Invoice.deleted_at).is_(None),
    )

    combined = union_all(pending_part, invoiced_part).subquery("owed")
    grouped = (
        sa_select(
            combined.c.encounter_id,
            func.sum(combined.c.pending).label("pending_total"),
            func.sum(combined.c.invoiced).label("invoiced_balance"),
            func.min(combined.c.since).label("outstanding_since"),
            func.bool_or(combined.c.unpriced).label("has_unpriced_items"),
        )
        .group_by(combined.c.encounter_id)
        # A fully paid invoice contributes a zero row, not an absent one. Without
        # this the board would show every settled visit of the day.
        .having(func.sum(combined.c.pending) + func.sum(combined.c.invoiced) > 0)
        .subquery("open_accounts")
    )

    total = (await session.execute(sa_select(func.count()).select_from(grouped))).scalar_one()
    rows = (
        await session.execute(
            sa_select(grouped)
            .order_by(grouped.c.outstanding_since)
            .limit(params.limit)
            .offset(params.offset)
        )
    ).all()

    return [
        OpenAccount(
            encounter_id=row.encounter_id,
            pending_total=money(Decimal(row.pending_total)),
            invoiced_balance=money(Decimal(row.invoiced_balance)),
            outstanding_since=row.outstanding_since,
            has_unpriced_items=bool(row.has_unpriced_items),
        )
        for row in rows
    ], total


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
async def get_invoice(
    session: AsyncSession, invoice_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Invoice:
    invoice = (
        (
            await session.execute(
                select(Invoice).where(
                    col(Invoice.id) == invoice_id,
                    col(Invoice.hospital_id) == hospital_id,
                    col(Invoice.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if invoice is None:
        raise NotFoundError("Invoice not found.", code="invoice_not_found")
    return invoice


async def get_draft_for_encounter(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Invoice | None:
    return (
        (
            await session.execute(
                select(Invoice).where(
                    col(Invoice.encounter_id) == encounter_id,
                    col(Invoice.hospital_id) == hospital_id,
                    col(Invoice.status) == InvoiceStatus.DRAFT,
                    col(Invoice.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )


async def list_invoices(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    encounter_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    status: InvoiceStatus | None = None,
    unpaid_only: bool = False,
    awaiting_settlement: bool = False,
) -> tuple[list[Invoice], int]:
    """The counter's queue, and the settlement worklist."""
    filters: list[ColumnElement[bool]] = [
        col(Invoice.hospital_id) == hospital_id,
        col(Invoice.deleted_at).is_(None),
    ]
    if encounter_id is not None:
        filters.append(col(Invoice.encounter_id) == encounter_id)
    if patient_id is not None:
        filters.append(col(Invoice.patient_id) == patient_id)
    if status is not None:
        filters.append(col(Invoice.status) == status)
    if unpaid_only:
        filters.append(col(Invoice.status).in_(_OPEN_INVOICE_STATUSES))
        filters.append(col(Invoice.balance_due) > 0)
    if awaiting_settlement:
        # Bills left behind by a death, a referral or a patient who walked out.
        filters.append(col(Invoice.settlement_context).is_not(None))
        filters.append(col(Invoice.settled_at).is_(None))
        filters.append(col(Invoice.balance_due) > 0)

    total = (
        await session.execute(select(func.count()).select_from(Invoice).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Invoice)
                .where(*filters)
                # Bills that need a person first, then oldest.
                .order_by(
                    col(Invoice.settlement_context).is_(None),
                    col(Invoice.created_at),
                )
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def _charges_on(session: AsyncSession, invoice_id: uuid.UUID) -> list[Charge]:
    rows = (
        (
            await session.execute(
                select(Charge)
                .where(
                    col(Charge.invoice_id) == invoice_id,
                    col(Charge.status) != ChargeStatus.CANCELLED,
                    col(Charge.deleted_at).is_(None),
                )
                .order_by(col(Charge.captured_at))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _recompute_totals(session: AsyncSession, invoice: Invoice) -> Invoice:
    """Re-add the footer from the invoice's own lines.

    Waived charges are excluded from the money but stay attached to the invoice,
    so the document can still show "Dressing — waived" rather than silently
    omitting care the patient received.
    """
    charges = [
        charge
        for charge in await _charges_on(session, invoice.id)
        if charge.status is not ChargeStatus.WAIVED
    ]
    lines: list[tuple[Decimal, Decimal, LineTax]] = [
        (
            money(charge.unit_price * charge.quantity),
            charge.discount_amount,
            LineTax(
                taxable_amount=charge.taxable_amount,
                gst_rate=charge.gst_rate,
                cgst_amount=charge.cgst_amount,
                sgst_amount=charge.sgst_amount,
                igst_amount=charge.igst_amount,
                total_amount=charge.total_amount,
            ),
        )
        for charge in charges
    ]
    totals = sum_lines(lines)

    invoice.subtotal = totals.subtotal
    invoice.discount_total = totals.discount_total
    invoice.taxable_total = totals.taxable_total
    invoice.cgst_total = totals.cgst_total
    invoice.sgst_total = totals.sgst_total
    invoice.igst_total = totals.igst_total
    invoice.tax_total = totals.tax_total
    invoice.round_off = totals.round_off
    invoice.grand_total = totals.grand_total
    invoice.balance_due = money(totals.grand_total - invoice.amount_paid)
    invoice.updated_at = utc_now()
    session.add(invoice)
    await session.flush()
    await session.refresh(invoice)
    return invoice


async def assemble_draft(
    session: AsyncSession,
    payload: InvoiceDraftRequest,
    *,
    hospital_id: uuid.UUID,
    actor: User | None = None,
) -> Invoice:
    """Gather every pending charge on a visit into a draft invoice.

    Re-runnable: called again after another charge lands, it pulls the new line
    onto the existing draft rather than opening a second one. That is what makes
    "reception reviews the invoice, never rebuilds it" (CLAUDE.md §7b) work in a
    clinic where the pharmacy charge arrives after the consultation one.
    """
    encounter = await clinical_service.get_encounter(
        session, payload.encounter_id, hospital_id=hospital_id
    )
    card = await resolve_rate_card(
        session, hospital_id=hospital_id, preferred_id=payload.rate_card_id
    )

    invoice = await get_draft_for_encounter(session, encounter.id, hospital_id=hospital_id)
    if invoice is None:
        hospital = await tenancy_service.get_hospital(session, hospital_id)
        invoice = Invoice(
            hospital_id=hospital_id,
            invoice_number=await _next_number(session, hospital_id, "INV"),
            encounter_id=encounter.id,
            patient_id=encounter.patient_id,
            rate_card_id=card.id if card else None,
            payer_type=payload.payer_type or (card.payer_type if card else PayerType.CASH),
            status=InvoiceStatus.DRAFT,
            payer_liability=money(payload.payer_liability),
            # Snapshotted now rather than at issue so a draft printed for a
            # patient to look at already carries the right registration.
            hospital_gstin=hospital.gstin,
            place_of_supply=payload.place_of_supply or hospital.state,
            is_interstate=payload.is_interstate,
            notes=payload.notes,
        )
        session.add(invoice)
        await session.flush()
    elif payload.payer_liability:
        invoice.payer_liability = money(payload.payer_liability)
        session.add(invoice)

    pending = (
        (
            await session.execute(
                select(Charge).where(
                    col(Charge.encounter_id) == encounter.id,
                    col(Charge.hospital_id) == hospital_id,
                    col(Charge.status) == ChargeStatus.PENDING,
                    col(Charge.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for charge in pending:
        charge.status = ChargeStatus.INVOICED
        charge.invoice_id = invoice.id
        charge.updated_at = utc_now()
        session.add(charge)
    await session.flush()

    return await _recompute_totals(session, invoice)


async def issue_invoice(
    session: AsyncSession,
    invoice: Invoice,
    *,
    actor: User,
    hospital_id: uuid.UUID,
    notes: str | None = None,
) -> Invoice:
    """Turn a draft into a document. One-way door.

    After this the lines are frozen and the patient has a printout. An empty
    invoice is refused: handing somebody a bill for nothing is not a document,
    it is a mistake with a serial number attached — and that serial number is
    then permanently missing from a GST series that has to be consecutive.
    """
    assert_invoice_transition(invoice.status, InvoiceStatus.ISSUED)

    charges = await _charges_on(session, invoice.id)
    if not charges:
        raise ValidationError("There is nothing on this invoice yet.", code="invoice_empty")

    invoice = await _recompute_totals(session, invoice)

    moment = utc_now()
    invoice.status = InvoiceStatus.ISSUED
    invoice.issued_at = moment
    invoice.issued_by_id = actor.id
    if notes:
        invoice.notes = notes
    invoice.updated_at = moment
    session.add(invoice)
    await session.flush()

    await event_bus.publish(
        InvoiceIssued(
            hospital_id=hospital_id,
            actor_id=actor.id,
            invoice_id=invoice.id,
            encounter_id=invoice.encounter_id,
            patient_id=invoice.patient_id,
            invoice_number=invoice.invoice_number,
            grand_total=invoice.grand_total,
            balance_due=invoice.balance_due,
        ),
        session=session,
    )
    await session.refresh(invoice)
    return invoice


async def cancel_invoice(
    session: AsyncSession,
    invoice: Invoice,
    *,
    reason: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> Invoice:
    """Void an invoice and release its charges back onto the running bill.

    Refused once money has been received against it. A cancelled invoice that
    has been paid would leave the payment pointing at nothing; the remedy after
    a payment is a reversal, then a cancellation, in that order.
    """
    if invoice.amount_paid > 0:
        raise ConflictError(
            "Money has been received against this invoice. Reverse the payment first.",
            code="invoice_has_payments",
            details={"amount_paid": str(invoice.amount_paid)},
        )
    assert_invoice_transition(invoice.status, InvoiceStatus.CANCELLED)

    for charge in await _charges_on(session, invoice.id):
        if charge.status is ChargeStatus.INVOICED:
            # Back onto the running bill, not cancelled: the care still happened
            # and still has to appear on whatever invoice replaces this one.
            charge.status = ChargeStatus.PENDING
            charge.invoice_id = None
            charge.updated_at = utc_now()
            session.add(charge)

    invoice.status = InvoiceStatus.CANCELLED
    invoice.cancellation_reason = reason
    invoice.balance_due = ZERO
    invoice.updated_at = utc_now()
    session.add(invoice)
    await session.flush()
    await session.refresh(invoice)
    return invoice


async def write_off_invoice(
    session: AsyncSession,
    invoice: Invoice,
    *,
    reason: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> Invoice:
    """Stop chasing an unpaid bill.

    Terminal, and it clears the visit's closure gate — which is the point. A
    patient who absconded owing ₹4,000 leaves an encounter that would otherwise
    sit open forever; somebody has to be able to make that decision, and it has
    to be recorded as a decision with a name on it rather than a status quietly
    flipped.
    """
    assert_invoice_transition(invoice.status, InvoiceStatus.WRITTEN_OFF)

    invoice.status = InvoiceStatus.WRITTEN_OFF
    invoice.write_off_reason = reason
    invoice.write_off_by_id = actor.id
    invoice.balance_due = ZERO
    invoice.settled_at = utc_now()
    invoice.updated_at = utc_now()
    session.add(invoice)
    await session.flush()

    await _close_visit_if_settled(session, invoice, actor=actor, hospital_id=hospital_id)
    await session.refresh(invoice)
    return invoice


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
async def get_payment(
    session: AsyncSession, payment_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Payment:
    payment = (
        (
            await session.execute(
                select(Payment).where(
                    col(Payment.id) == payment_id,
                    col(Payment.hospital_id) == hospital_id,
                    col(Payment.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if payment is None:
        raise NotFoundError("Payment not found.", code="payment_not_found")
    return payment


async def list_payments(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    invoice_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    method: PaymentMethod | None = None,
    since: datetime | None = None,
) -> tuple[list[Payment], int]:
    """Also the day's collection report, filtered by method."""
    filters: list[ColumnElement[bool]] = [
        col(Payment.hospital_id) == hospital_id,
        col(Payment.deleted_at).is_(None),
    ]
    if invoice_id is not None:
        filters.append(col(Payment.invoice_id) == invoice_id)
    if patient_id is not None:
        filters.append(col(Payment.patient_id) == patient_id)
    if method is not None:
        filters.append(col(Payment.method) == method)
    if since is not None:
        filters.append(col(Payment.received_at) >= since)

    total = (
        await session.execute(select(func.count()).select_from(Payment).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Payment)
                .where(*filters)
                .order_by(col(Payment.received_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def _apply_receipts(session: AsyncSession, invoice: Invoice) -> Invoice:
    """Recompute what has been received, and move the invoice's status with it.

    Summed from the payment rows rather than incremented in place. An increment
    that is applied twice — a retry, a double-click — silently marks a bill paid
    that is not, and nothing downstream would notice.
    """
    received = (
        await session.execute(
            select(func.coalesce(func.sum(col(Payment.amount)), 0)).where(
                col(Payment.invoice_id) == invoice.id,
                col(Payment.status) == PaymentStatus.RECORDED,
                col(Payment.deleted_at).is_(None),
            )
        )
    ).scalar_one()

    invoice.amount_paid = money(Decimal(received))
    invoice.balance_due = money(invoice.grand_total - invoice.amount_paid)

    if invoice.status in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID):
        if invoice.balance_due <= 0:
            target = InvoiceStatus.PAID
        elif invoice.amount_paid > 0:
            target = InvoiceStatus.PARTIALLY_PAID
        else:
            target = InvoiceStatus.ISSUED
        if target is not invoice.status:
            assert_invoice_transition(invoice.status, target)
            invoice.status = target

    invoice.updated_at = utc_now()
    session.add(invoice)
    await session.flush()
    await session.refresh(invoice)
    return invoice


async def record_payment(
    session: AsyncSession,
    invoice: Invoice,
    *,
    amount: Decimal,
    method: PaymentMethod,
    actor: User,
    hospital_id: uuid.UUID,
    reference: str | None = None,
    payer_name: str | None = None,
    received_at: datetime | None = None,
    notes: str | None = None,
) -> tuple[Payment, Invoice]:
    """Take money against an issued invoice.

    Refuses a draft: money must never be received against a total that can still
    change. Refuses an overpayment too — a patient handing over more than the
    bill is an advance, which is a different record with a different meaning at
    year end, not a payment that happens to be too big.

    If this clears the last balance the visit closes itself, exactly as verifying
    the last lab report does (CLAUDE.md §6). The cashier never has to know.
    """
    if invoice.status is InvoiceStatus.DRAFT:
        raise ConflictError(
            "Issue the invoice before taking payment — a draft total can still change.",
            code="invoice_not_issued",
        )
    if invoice.status in (InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF):
        raise ConflictError(
            f"This invoice is {invoice.status.value.lower().replace('_', ' ')}.",
            code="invoice_closed",
        )

    taken = money(amount)
    if taken <= 0:
        raise ValidationError("A payment must be more than zero.", code="payment_not_positive")
    if taken > invoice.balance_due:
        raise ValidationError(
            f"That is more than the {invoice.balance_due} outstanding on this invoice.",
            code="payment_exceeds_balance",
            details={"balance_due": str(invoice.balance_due)},
        )

    payment = Payment(
        hospital_id=hospital_id,
        receipt_number=await _next_number(session, hospital_id, "RCP"),
        invoice_id=invoice.id,
        patient_id=invoice.patient_id,
        amount=taken,
        method=method,
        reference=reference,
        payer_name=payer_name,
        status=PaymentStatus.RECORDED,
        received_at=received_at or utc_now(),
        received_by_id=actor.id,
        notes=notes,
    )
    session.add(payment)
    await session.flush()

    invoice = await _apply_receipts(session, invoice)

    await event_bus.publish(
        PaymentReceived(
            hospital_id=hospital_id,
            actor_id=actor.id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            patient_id=invoice.patient_id,
            receipt_number=payment.receipt_number,
            amount=payment.amount,
            method=method.value,
            balance_due=invoice.balance_due,
        ),
        session=session,
    )

    if invoice.status is InvoiceStatus.PAID:
        invoice.settled_at = utc_now()
        session.add(invoice)
        await session.flush()
        await event_bus.publish(
            InvoicePaid(
                hospital_id=hospital_id,
                actor_id=actor.id,
                invoice_id=invoice.id,
                encounter_id=invoice.encounter_id,
                patient_id=invoice.patient_id,
                invoice_number=invoice.invoice_number,
                grand_total=invoice.grand_total,
            ),
            session=session,
        )

    await _close_visit_if_settled(session, invoice, actor=actor, hospital_id=hospital_id)
    await session.refresh(payment)
    return payment, invoice


async def reverse_payment(
    session: AsyncSession,
    payment: Payment,
    *,
    reason: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> tuple[Payment, Invoice]:
    """Give money back, by writing a second row rather than editing the first.

    The original receipt stays exactly as it was printed. The cash drawer has to
    reconcile against what actually happened during the shift, and an edited
    payment makes that impossible to do honestly.
    """
    assert_payment_transition(payment.status, PaymentStatus.REVERSED)

    moment = utc_now()
    payment.status = PaymentStatus.REVERSED
    payment.reversed_at = moment
    payment.reversal_reason = reason
    payment.updated_at = moment
    session.add(payment)

    session.add(
        Payment(
            hospital_id=hospital_id,
            receipt_number=await _next_number(session, hospital_id, "RCP"),
            invoice_id=payment.invoice_id,
            patient_id=payment.patient_id,
            # Recorded as its own reversed row so the pair nets to zero in every
            # report without any report having to know about reversals.
            amount=payment.amount,
            method=payment.method,
            reference=payment.reference,
            status=PaymentStatus.REVERSED,
            received_at=moment,
            received_by_id=actor.id,
            reverses_payment_id=payment.id,
            reversal_reason=reason,
        )
    )
    await session.flush()

    invoice = await get_invoice(session, payment.invoice_id, hospital_id=hospital_id)
    invoice.settled_at = None
    invoice = await _apply_receipts(session, invoice)

    await event_bus.publish(
        PaymentReversed(
            hospital_id=hospital_id,
            actor_id=actor.id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            patient_id=invoice.patient_id,
            amount=payment.amount,
            reason=reason,
        ),
        session=session,
    )
    await session.refresh(payment)
    return payment, invoice


async def _close_visit_if_settled(
    session: AsyncSession, invoice: Invoice, *, actor: User | None, hospital_id: uuid.UUID
) -> None:
    """Let the visit move on now that this bill no longer holds it.

    A direct call into `clinical.service`, not an event, for the reason
    `diagnostics` gives for the same decision: the bus is fire-and-forget, and a
    swallowed handler here would leave a fully paid visit sitting in
    `PENDING_CLEARANCE` with nobody able to say why.
    """
    encounter = await clinical_service.get_encounter(
        session, invoice.encounter_id, hospital_id=hospital_id
    )
    if encounter.is_closed:
        return
    await clinical_service.reevaluate_closure(session, encounter, actor=actor)


# ---------------------------------------------------------------------------
# Settlement (CLAUDE.md §6 — a deceased or referred patient still owes)
# ---------------------------------------------------------------------------
async def settle_on_closure(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    encounter_id: uuid.UUID,
    patient_id: uuid.UUID,
    final_status: str,
    actor_id: uuid.UUID | None = None,
) -> Invoice | None:
    """Run the settlement flow for a visit that has just closed.

    CLAUDE.md §6 is explicit that a deceased or referred patient usually still
    has an outstanding bill, and that it must go through settlement rather than
    disappearing with the patient. So: any pending charge is gathered onto a
    draft, and every open invoice for the visit is stamped with *why* it needs a
    person. It is deliberately **not** issued automatically — issuing is a staff
    act, and a bill for a family that has just been bereaved should be handed
    over by somebody, not generated at them.

    Returns the invoice that needs attention, or None when nothing is owed.
    """
    pending_total = (
        await session.execute(
            select(func.coalesce(func.sum(col(Charge.total_amount)), 0)).where(
                col(Charge.encounter_id) == encounter_id,
                col(Charge.status) == ChargeStatus.PENDING,
                col(Charge.deleted_at).is_(None),
            )
        )
    ).scalar_one()

    invoice: Invoice | None = None
    if Decimal(pending_total) > 0:
        invoice = await assemble_draft(
            session,
            InvoiceDraftRequest(encounter_id=encounter_id),
            hospital_id=hospital_id,
        )

    open_invoices = (
        (
            await session.execute(
                select(Invoice).where(
                    col(Invoice.encounter_id) == encounter_id,
                    col(Invoice.hospital_id) == hospital_id,
                    col(Invoice.status).in_(_OPEN_INVOICE_STATUSES),
                    col(Invoice.balance_due) > 0,
                    col(Invoice.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not open_invoices:
        return None

    # Never message a patient recorded as deceased (CLAUDE.md §14). The flag
    # travels on the event so the rule does not depend on a dispatcher
    # remembering to look it up — the bill is real, the automated reminder is not.
    notify_patient = final_status != "DECEASED"

    for open_invoice in open_invoices:
        open_invoice.settlement_context = final_status
        open_invoice.settlement_note = _settlement_note(final_status)
        open_invoice.updated_at = utc_now()
        session.add(open_invoice)
        invoice = invoice or open_invoice

        await event_bus.publish(
            SettlementRequired(
                hospital_id=hospital_id,
                actor_id=actor_id,
                invoice_id=open_invoice.id,
                encounter_id=encounter_id,
                patient_id=patient_id,
                context=final_status,
                balance_due=open_invoice.balance_due,
                notify_patient=notify_patient,
            ),
            session=session,
        )

    await session.flush()
    logger.info(
        "settlement required on %d invoice(s) for encounter %s (%s)",
        len(open_invoices),
        encounter_id,
        final_status,
    )
    return invoice


def _settlement_note(final_status: str) -> str:
    """Why this bill is sitting on somebody's desk, in words they can act on."""
    return {
        "DECEASED": "Patient deceased — settle with the family in person. "
        "No automated reminders will be sent.",
        "REFERRED_OUT": "Patient referred to another facility — settle before or on transfer.",
        "LAMA": "Patient left against medical advice — balance outstanding.",
        "COMPLETED": "Visit closed with a balance outstanding.",
    }.get(final_status, "Visit closed with a balance outstanding.")


# ---------------------------------------------------------------------------
# The counter's screen
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class VisitAccountData:
    """The counter's view of one visit. A typed structure rather than a dict:
    it crosses a module boundary, and the router should not be guessing keys."""

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    uhid: str
    patient_name: str
    encounter_number: str
    encounter_status: str
    pending_charges: list[Charge]
    invoices: list[Invoice]
    pending_total: Decimal
    invoiced_total: Decimal
    paid_total: Decimal
    balance_due: Decimal
    has_unpriced_items: bool


async def visit_account(
    session: AsyncSession, encounter: Encounter, *, hospital_id: uuid.UUID
) -> VisitAccountData:
    """Everything the cash counter needs about one visit, in one round trip."""
    patient = await patients_service.get_patient(session, encounter.patient_id)

    charges, _ = await list_charges(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=hospital_id,
        encounter_id=encounter.id,
        status=ChargeStatus.PENDING,
    )
    invoices, _ = await list_invoices(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=hospital_id,
        encounter_id=encounter.id,
    )

    pending_total = money(sum((charge.total_amount for charge in charges), ZERO))
    invoiced_total = money(
        sum(
            (
                invoice.grand_total
                for invoice in invoices
                if invoice.status is not InvoiceStatus.CANCELLED
            ),
            ZERO,
        )
    )
    paid_total = money(sum((invoice.amount_paid for invoice in invoices), ZERO))

    return VisitAccountData(
        encounter_id=encounter.id,
        patient_id=patient.id,
        uhid=patient.uhid,
        patient_name=patient.full_name,
        encounter_number=encounter.encounter_number,
        encounter_status=encounter.status.value,
        pending_charges=charges,
        invoices=invoices,
        pending_total=pending_total,
        invoiced_total=invoiced_total,
        paid_total=paid_total,
        balance_due=await open_balance(session, encounter.id),
        has_unpriced_items=any(charge.needs_pricing for charge in charges),
    )


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
async def raise_claim(
    session: AsyncSession,
    payload: ClaimCreate,
    *,
    hospital_id: uuid.UUID,
    actor: User,
) -> InsuranceClaim:
    """Open a claim against an insurer or scheme for an issued invoice."""
    invoice = await get_invoice(session, payload.invoice_id, hospital_id=hospital_id)
    if invoice.status is InvoiceStatus.DRAFT:
        raise ConflictError(
            "Issue the invoice before claiming against it.", code="invoice_not_issued"
        )

    claim = InsuranceClaim(
        hospital_id=hospital_id,
        claim_number=f"CLM-{invoice.invoice_number}",
        invoice_id=invoice.id,
        patient_id=invoice.patient_id,
        payer_name=payload.payer_name,
        scheme_code=payload.scheme_code,
        policy_number=payload.policy_number,
        tpa_name=payload.tpa_name,
        status=ClaimStatus.DRAFT,
        # Defaults to what the payer is expected to cover, falling back to the
        # whole bill — a hospital claiming for less than it should is a mistake
        # nobody notices, so the default is the generous one.
        claimed_amount=money(
            payload.claimed_amount
            if payload.claimed_amount is not None
            else (invoice.payer_liability or invoice.grand_total)
        ),
        remarks=payload.remarks,
    )
    session.add(claim)
    await session.flush()
    await session.refresh(claim)
    return claim


async def get_claim(
    session: AsyncSession, claim_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> InsuranceClaim:
    claim = (
        (
            await session.execute(
                select(InsuranceClaim).where(
                    col(InsuranceClaim.id) == claim_id,
                    col(InsuranceClaim.hospital_id) == hospital_id,
                    col(InsuranceClaim.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if claim is None:
        raise NotFoundError("Claim not found.", code="claim_not_found")
    return claim


async def list_claims(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID,
    status: ClaimStatus | None = None,
    patient_id: uuid.UUID | None = None,
) -> tuple[list[InsuranceClaim], int]:
    filters: list[ColumnElement[bool]] = [
        col(InsuranceClaim.hospital_id) == hospital_id,
        col(InsuranceClaim.deleted_at).is_(None),
    ]
    if status is not None:
        filters.append(col(InsuranceClaim.status) == status)
    if patient_id is not None:
        filters.append(col(InsuranceClaim.patient_id) == patient_id)

    total = (
        await session.execute(select(func.count()).select_from(InsuranceClaim).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(InsuranceClaim)
                .where(*filters)
                .order_by(col(InsuranceClaim.created_at))
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def progress_claim(
    session: AsyncSession,
    claim: InsuranceClaim,
    payload: ClaimUpdate,
    *,
    actor: User,
    hospital_id: uuid.UUID,
) -> InsuranceClaim:
    """Move a claim along, recording what the payer said.

    When a claim settles, the settled amount is recorded against the invoice as
    a payment by the insurer — because from the hospital's side that is exactly
    what it is, and leaving it off would show a paid bill as outstanding forever.
    """
    assert_claim_transition(claim.status, payload.status)
    moment = utc_now()

    claim.status = payload.status
    if payload.approved_amount is not None:
        claim.approved_amount = money(payload.approved_amount)
    if payload.remarks:
        claim.remarks = payload.remarks

    if payload.status is ClaimStatus.SUBMITTED and claim.submitted_at is None:
        claim.submitted_at = moment
    if payload.status in (ClaimStatus.APPROVED, ClaimStatus.REJECTED, ClaimStatus.QUERIED):
        claim.responded_at = moment

    claim.updated_at = moment
    session.add(claim)
    await session.flush()

    if payload.status is ClaimStatus.SETTLED:
        settled = money(
            payload.settled_amount
            if payload.settled_amount is not None
            else (claim.approved_amount or claim.claimed_amount)
        )
        claim.settled_amount = settled
        claim.settled_at = moment
        session.add(claim)
        await session.flush()

        invoice = await get_invoice(session, claim.invoice_id, hospital_id=hospital_id)
        payable = min(settled, invoice.balance_due)
        if payable > 0:
            await record_payment(
                session,
                invoice,
                amount=payable,
                method=PaymentMethod.INSURANCE if not claim.scheme_code else PaymentMethod.SCHEME,
                actor=actor,
                hospital_id=hospital_id,
                reference=claim.claim_number,
                payer_name=claim.payer_name,
                notes=f"Claim settled by {claim.payer_name}.",
            )

    await session.refresh(claim)
    return claim
