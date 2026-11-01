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
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

# `sa_select` is SQLAlchemy's own `select`. SQLModel's is typed for up to four
# clauses and biased towards model classes; the open-accounts aggregate selects
# five labelled columns. Everything else in this module stays on the SQLModel
# one, imported below.
from sqlalchemy import ColumnElement, Select, case, func, literal, text, union_all
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
    InvoiceScope,
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
    BillAdd,
    BillDiscount,
    BillEdit,
    BillOp,
    BillRemove,
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
from app.modules.billing.stays import StayInfo, stay_for, stays_for
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
    "StayInfo",
    "add_charge",
    "apply_bill_changes",
    "assemble_draft",
    "bill_version",
    "cancel_charges_for_source",
    "cancel_invoice",
    "capture_charge",
    "catalogue_prices",
    "cost_breakdown",
    "create_rate_card",
    "create_service_item",
    "discount_charge",
    "edit_charge",
    "finalise_ipd_bill",
    "get_charge",
    "get_invoice",
    "get_payment",
    "get_rate_card_by_code",
    "get_service_item_by_code",
    "ipd_bill",
    "issue_invoice",
    "list_charges",
    "list_invoices",
    "list_open_accounts",
    "list_payments",
    "open_balance",
    "raise_claim",
    "record_payment",
    "resolve_rate_card",
    "reverse_charge_for_source",
    "reverse_payment",
    "set_price",
    "settle_on_closure",
    "split_pre_admission",
    "stay_for",
    "stays_for",
    "sync_catalogue_item",
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
    session: AsyncSession, code: str, *, hospital_id: uuid.UUID, include_inactive: bool = False
) -> ServiceItem | None:
    filters: list[ColumnElement[bool]] = [
        col(ServiceItem.hospital_id) == hospital_id,
        col(ServiceItem.code) == code.strip().upper(),
        col(ServiceItem.deleted_at).is_(None),
    ]
    if not include_inactive:
        filters.append(col(ServiceItem.is_active).is_(True))
    return (await session.execute(select(ServiceItem).where(*filters))).scalars().first()


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


async def sync_catalogue_item(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    code: str,
    name: str,
    unit: str | None,
    is_active: bool,
    price: Decimal | None,
    category: ChargeCategory = ChargeCategory.PHARMACY,
) -> ServiceItem:
    """Keep the priced half of another module's catalogue item in step.

    The medicine list (`ipd`) owns what an item *is*; this module owns what it
    *costs*. The two are linked by `code`, as every catalogue here is, and this
    is the one door through which the list writes its half: create the service
    on first sight, keep its name, unit and active flag in step afterwards,
    and — when `price` is given — set it on the default rate card, which is the
    card `capture_charge` resolves for a patient with no scheme.

    GST is left alone after creation. The administrator's price list is where
    tax is set, and an edit to a medicine's name must not quietly reset it.

    Refuses to set a price with no default rate card, rather than creating one
    on the caller's behalf: a price list appearing out of nowhere is exactly
    the kind of configuration nobody can later explain.
    """
    code = code.strip().upper()
    item = await get_service_item_by_code(
        session, code, hospital_id=hospital_id, include_inactive=True
    )
    if item is None:
        item = ServiceItem(
            hospital_id=hospital_id,
            code=code,
            name=name[:200],
            category=category,
            unit=unit[:24] if unit else None,
            is_active=is_active,
        )
    else:
        item.name = name[:200]
        item.unit = unit[:24] if unit else None
        item.is_active = is_active
        item.updated_at = utc_now()
    session.add(item)
    await session.flush()

    if price is not None:
        card = await get_default_rate_card(session, hospital_id=hospital_id)
        if card is None:
            raise ConflictError(
                "There is no default price list yet. An administrator sets one up under "
                "Administration → Services; prices can be added after that.",
                code="no_default_rate_card",
            )
        await set_price(session, item, ServicePriceUpsert(rate_card_id=card.id, price=price))

    await session.refresh(item)
    return item


async def catalogue_prices(
    session: AsyncSession, codes: Collection[str], *, hospital_id: uuid.UUID
) -> dict[str, Decimal]:
    """What each code costs on the default rate card — the price a cash patient pays.

    One query for a whole list, keyed by code. A code with no price is simply
    absent, which the caller shows as "not priced" rather than as zero: a
    visible gap in a price list gets filled, a zero gets charged.
    """
    wanted = {code.strip().upper() for code in codes if code}
    if not wanted:
        return {}
    card = await get_default_rate_card(session, hospital_id=hospital_id)
    if card is None:
        return {}

    rows = await session.execute(
        select(col(ServiceItem.code), col(ServicePrice.price))
        .join(ServicePrice, col(ServicePrice.service_item_id) == col(ServiceItem.id))
        .where(
            col(ServiceItem.hospital_id) == hospital_id,
            col(ServiceItem.code).in_(wanted),
            col(ServiceItem.deleted_at).is_(None),
            col(ServicePrice.rate_card_id) == card.id,
            col(ServicePrice.deleted_at).is_(None),
        )
    )
    return {row[0]: row[1] for row in rows}


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
    fallback_to_category_default: bool = True,
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

    `fallback_to_category_default` decides what an unmapped `item_code` means.
    For a consultation the hospital-wide fee is the right answer. For a named
    item from the medicine list it is not: the first pharmacy service on file
    is some *other* drug, and charging its price for this one is a wrong
    number presented as a right one. Those callers pass False, and a missing
    item lands on the exception list instead.
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
        await get_service_item_by_code(
            session,
            item_code,
            hospital_id=hospital_id,
            # A named catalogue item (the medicine list) switched off *after* it
            # was prescribed is still the item that was given, and its price
            # still applies. Switching off stops new picks, not running courses
            # — without this, every later dose of the course would land at zero
            # on the exception worklist.
            include_inactive=not fallback_to_category_default,
        )
        if item_code
        else None
    )
    if item is None and doctor_id is not None and category is ChargeCategory.CONSULTATION:
        item = await _consultation_item_for(session, hospital_id=hospital_id, doctor_id=doctor_id)
    if item is None and fallback_to_category_default:
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


async def reverse_charge_for_source(
    session: AsyncSession,
    *,
    hospital_id: uuid.UUID,
    source_module: str,
    source_type: str,
    source_id: uuid.UUID,
    reason: str,
) -> Charge | None:
    """Take an act's charge off the bill because the act itself is being undone.

    The strict sibling of `cancel_charges_for_source`. That one is for acts
    that were *called off* upstream, and quietly leaves an invoiced line alone.
    This one is for a record being corrected — "we did not use that IV set" —
    and a correction the bill cannot follow must not half-happen: it refuses,
    so the caller's whole transaction rolls back and the record still matches
    the invoice the patient is holding.
    """
    charge = await find_charge_for_source(
        session,
        hospital_id=hospital_id,
        source_module=source_module,
        source_type=source_type,
        source_id=source_id,
    )
    if charge is not None and charge.status is ChargeStatus.INVOICED:
        raise ConflictError(
            "This is already on an invoice, so it cannot simply be removed. "
            "Billing can raise a credit note for it.",
            code="charge_invoiced",
            details={"invoice_id": str(charge.invoice_id)},
        )
    return await cancel_charges_for_source(
        session,
        hospital_id=hospital_id,
        source_module=source_module,
        source_type=source_type,
        source_id=source_id,
        reason=reason,
    )


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


def _assert_charge_editable(charge: Charge, *, draft_id: uuid.UUID | None = None) -> None:
    """`draft_id` is the stay's running IPD draft: its lines are still the
    accountant's to correct, because a draft is a working total, not a document."""
    if charge.status is ChargeStatus.INVOICED and draft_id and charge.invoice_id == draft_id:
        return
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


async def edit_charge(
    session: AsyncSession,
    charge: Charge,
    *,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
    description: str | None = None,
    draft_id: uuid.UUID | None = None,
) -> Charge:
    """Correct a line's quantity, rate or wording; tax and total follow.

    A discount the smaller line can no longer carry is refused, for the same
    reason `discount_charge` refuses one: somebody meant something else.
    """
    _assert_charge_editable(charge, draft_id=draft_id)
    if quantity is not None:
        charge.quantity = quantity
    if unit_price is not None:
        charge.unit_price = money(unit_price)
        charge.needs_pricing = False
    if description is not None:
        charge.description = description
    gross = money(charge.unit_price * charge.quantity)
    if charge.discount_amount > gross:
        raise ValidationError(
            f"The discount of {charge.discount_amount} is more than the charge of {gross}.",
            code="discount_exceeds_charge",
        )
    return await _reprice(session, charge)


async def price_charge(session: AsyncSession, charge: Charge, *, unit_price: Decimal) -> Charge:
    """Put a price on a line that was captured without one."""
    _assert_charge_editable(charge)
    charge.unit_price = money(unit_price)
    charge.needs_pricing = False
    return await _reprice(session, charge)


async def discount_charge(
    session: AsyncSession,
    charge: Charge,
    *,
    amount: Decimal,
    reason: str,
    actor: User,
    draft_id: uuid.UUID | None = None,
) -> Charge:
    """Reduce a line, with a reason attached.

    A discount larger than the line is refused rather than clamped: somebody
    meant to type something else, and silently turning ₹5,000 off a ₹500 line
    into ₹500 off hides the typo.
    """
    _assert_charge_editable(charge, draft_id=draft_id)
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
    session: AsyncSession,
    charge: Charge,
    *,
    reason: str,
    actor: User,
    draft_id: uuid.UUID | None = None,
) -> Charge:
    """Decide this line is not owed.

    Distinct from cancelling: the act happened and stays on the clinical record;
    what changed is that nobody is paying for it. Held behind its own permission
    because it is the lever somebody would pull to make money disappear.
    """
    _assert_charge_editable(charge, draft_id=draft_id)
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
class VisitBalance:
    """What a visit owes and what it has paid — the reception row's money chip."""

    balance_due: Decimal
    amount_paid: Decimal


async def balances_for(
    session: AsyncSession, encounter_ids: Collection[uuid.UUID], *, hospital_id: uuid.UUID
) -> dict[uuid.UUID, VisitBalance]:
    """`open_balance` for a whole queue board in two queries, not two per row.

    A visit with no billing rows at all is absent from the result, which the
    caller reads as "nothing to collect" — not as a zero that was paid.
    """
    if not encounter_ids:
        return {}
    # An inpatient's bill is the accountant's, not the counter's: a stay is
    # left out, so the reception row shows nothing to collect.
    stays = await stays_for(session, encounter_ids, hospital_id=hospital_id)
    encounter_ids = [encounter_id for encounter_id in encounter_ids if encounter_id not in stays]
    if not encounter_ids:
        return {}

    pending_rows = (
        await session.execute(
            sa_select(col(Charge.encounter_id), func.sum(col(Charge.total_amount)))
            .where(
                col(Charge.hospital_id) == hospital_id,
                col(Charge.encounter_id).in_(encounter_ids),
                col(Charge.status) == ChargeStatus.PENDING,
                col(Charge.deleted_at).is_(None),
            )
            .group_by(col(Charge.encounter_id))
        )
    ).all()
    invoice_rows = (
        await session.execute(
            sa_select(
                col(Invoice.encounter_id),
                func.sum(
                    case(
                        (col(Invoice.status).in_(_OPEN_INVOICE_STATUSES), col(Invoice.balance_due)),
                        else_=ZERO,
                    )
                ),
                func.sum(col(Invoice.amount_paid)),
            )
            .where(
                col(Invoice.hospital_id) == hospital_id,
                col(Invoice.encounter_id).in_(encounter_ids),
                col(Invoice.status) != InvoiceStatus.CANCELLED,
                col(Invoice.deleted_at).is_(None),
            )
            .group_by(col(Invoice.encounter_id))
        )
    ).all()

    pending = {encounter_id: Decimal(total) for encounter_id, total in pending_rows}
    owed = {encounter_id: Decimal(balance) for encounter_id, balance, _ in invoice_rows}
    paid = {encounter_id: Decimal(amount) for encounter_id, _, amount in invoice_rows}
    return {
        encounter_id: VisitBalance(
            balance_due=money(pending.get(encounter_id, ZERO) + owed.get(encounter_id, ZERO)),
            amount_paid=money(paid.get(encounter_id, ZERO)),
        )
        for encounter_id in pending.keys() | owed.keys()
    }


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
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    hospital_id: uuid.UUID,
    for_update: bool = False,
) -> Invoice:
    """`for_update` before taking money against it: a second payment on the same
    invoice then waits for the first and checks the balance the first left,
    rather than the balance both of them read."""
    query = select(Invoice).where(
        col(Invoice.id) == invoice_id,
        col(Invoice.hospital_id) == hospital_id,
        col(Invoice.deleted_at).is_(None),
    )
    if for_update:
        query = query.with_for_update()
    invoice = (await session.execute(query)).scalars().first()
    if invoice is None:
        raise NotFoundError("Invoice not found.", code="invoice_not_found")
    return invoice


async def get_draft_for_encounter(
    session: AsyncSession,
    encounter_id: uuid.UUID,
    *,
    hospital_id: uuid.UUID,
    scope: InvoiceScope | None = None,
) -> Invoice | None:
    query = select(Invoice).where(
        col(Invoice.encounter_id) == encounter_id,
        col(Invoice.hospital_id) == hospital_id,
        col(Invoice.status) == InvoiceStatus.DRAFT,
        col(Invoice.deleted_at).is_(None),
    )
    if scope is not None:
        query = query.where(col(Invoice.scope) == scope)
    return (await session.execute(query)).scalars().first()


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
    # A stay's bill holds only what was charged from the admission on; what
    # came before is its own OPD bill (`split_pre_admission`). Split again
    # here, so lines from before the admission can never be stranded: after
    # that OPD bill was cancelled to correct it, or on a stay admitted before
    # the split existed, whose OPD draft still holds them.
    stay = await stay_for(session, encounter.id, hospital_id=hospital_id)
    if stay is not None:
        await split_pre_admission(
            session, encounter, admitted_at=stay.admitted_at, actor=actor, hospital_id=hospital_id
        )
    scope = InvoiceScope.IPD if stay is not None else InvoiceScope.OPD

    invoice = await get_draft_for_encounter(
        session, encounter.id, hospital_id=hospital_id, scope=scope
    )
    if invoice is None and stay is None:
        # A visit whose admission was cancelled is an OPD visit again; the
        # draft it ran as a stay becomes its OPD draft rather than a second one.
        invoice = await get_draft_for_encounter(
            session, encounter.id, hospital_id=hospital_id, scope=InvoiceScope.IPD
        )
        if invoice is not None:
            invoice.scope = InvoiceScope.OPD
            session.add(invoice)
    if invoice is None:
        invoice = await _new_draft(session, encounter, payload, scope=scope)
    elif payload.payer_liability:
        invoice.payer_liability = money(payload.payer_liability)
        session.add(invoice)

    await _gather_pending(session, invoice, since=stay.admitted_at if stay else None)
    return await _recompute_totals(session, invoice)


async def _new_draft(
    session: AsyncSession,
    encounter: Encounter,
    payload: InvoiceDraftRequest,
    *,
    scope: InvoiceScope,
) -> Invoice:
    hospital_id = encounter.hospital_id
    card = await resolve_rate_card(
        session, hospital_id=hospital_id, preferred_id=payload.rate_card_id
    )
    hospital = await tenancy_service.get_hospital(session, hospital_id)
    invoice = Invoice(
        hospital_id=hospital_id,
        invoice_number=await _next_number(session, hospital_id, "INV"),
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        rate_card_id=card.id if card else None,
        payer_type=payload.payer_type or (card.payer_type if card else PayerType.CASH),
        scope=scope,
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
    return invoice


async def _gather_pending(
    session: AsyncSession,
    invoice: Invoice,
    *,
    since: datetime | None = None,
    before: datetime | None = None,
) -> None:
    """Move the visit's pending charges onto `invoice`, optionally bounded by
    when they were captured."""
    query = select(Charge).where(
        col(Charge.encounter_id) == invoice.encounter_id,
        col(Charge.hospital_id) == invoice.hospital_id,
        col(Charge.status) == ChargeStatus.PENDING,
        col(Charge.deleted_at).is_(None),
    )
    if since is not None:
        query = query.where(col(Charge.captured_at) >= since)
    if before is not None:
        query = query.where(col(Charge.captured_at) < before)
    for charge in (await session.execute(query)).scalars().all():
        charge.status = ChargeStatus.INVOICED
        charge.invoice_id = invoice.id
        charge.updated_at = utc_now()
        session.add(charge)
    await session.flush()


async def split_pre_admission(
    session: AsyncSession,
    encounter: Encounter,
    *,
    admitted_at: datetime,
    actor: User | None,
    hospital_id: uuid.UUID,
) -> Invoice | None:
    """Freeze what a visit owed before its admission as its own OPD bill.

    A patient admitted from a still-open OPD visit carries the visit — and its
    consultation fee — into the stay. That fee is an OPD bill, payable at the
    counter like any other, and must not disappear into the IPD bill the
    accountant edits. So at the moment of admission every charge captured
    before it, and any OPD draft the counter had started, is issued as one OPD
    invoice. Already paid: nothing is pending, nothing happens.

    An unpriced line is issued at its visible zero rather than blocking the
    admission; billing staff cancel and re-issue the OPD bill to correct it.

    Returns the issued invoice, or None when nothing was owed.
    """
    await _lock_visit_billing(session, encounter.id, hospital_id=hospital_id)
    invoice = await get_draft_for_encounter(
        session, encounter.id, hospital_id=hospital_id, scope=InvoiceScope.OPD
    )
    if invoice is None:
        owed_before = (
            await session.execute(
                select(func.count())
                .select_from(Charge)
                .where(
                    col(Charge.encounter_id) == encounter.id,
                    col(Charge.hospital_id) == hospital_id,
                    col(Charge.status) == ChargeStatus.PENDING,
                    col(Charge.captured_at) < admitted_at,
                    col(Charge.deleted_at).is_(None),
                )
            )
        ).scalar_one()
        if not owed_before:
            return None
        invoice = await _new_draft(
            session,
            encounter,
            InvoiceDraftRequest(encounter_id=encounter.id),
            scope=InvoiceScope.OPD,
        )
    for charge in await _charges_on(session, invoice.id):
        if charge.captured_at >= admitted_at and charge.status is ChargeStatus.INVOICED:
            # An OPD draft from before this rule existed may hold lines from
            # after the admission; they belong to the IPD bill.
            charge.status = ChargeStatus.PENDING
            charge.invoice_id = None
            charge.updated_at = utc_now()
            session.add(charge)
    await session.flush()
    await _gather_pending(session, invoice, before=admitted_at)
    if not await _charges_on(session, invoice.id):
        # An empty draft the counter left behind. Closed: a stay's OPD bill is
        # issued or absent, never a draft that sits open beside the IPD one.
        invoice.status = InvoiceStatus.CANCELLED
        invoice.cancellation_reason = "Empty draft closed at admission."
        invoice.updated_at = utc_now()
        session.add(invoice)
        await session.flush()
        return None
    return await issue_invoice(session, invoice, actor=actor, hospital_id=hospital_id)


async def rebill_pre_admission(
    session: AsyncSession, encounter: Encounter, *, actor: User, hospital_id: uuid.UUID
) -> Invoice:
    """Issue again what a stay owed before its admission.

    The way the counter corrects that OPD bill: cancel it (its lines return to
    pending), price or discount the lines, then issue it here. Refused when
    nothing from before the admission is waiting.
    """
    stay = await _require_stay(session, encounter, hospital_id=hospital_id)
    invoice = await split_pre_admission(
        session, encounter, admitted_at=stay.admitted_at, actor=actor, hospital_id=hospital_id
    )
    if invoice is None:
        raise ConflictError(
            "Nothing from before the admission is waiting to be billed.",
            code="nothing_before_admission",
        )
    return invoice


async def invoice_lines(
    session: AsyncSession, invoice: Invoice, *, hospital_id: uuid.UUID
) -> list[Charge]:
    """Every line of one invoice, unpaged — what its printed copy shows.

    Bounded by the invoice, not by a page: a three-week stay charged dose by
    dose runs to hundreds of lines, and a bill missing its last days is a GST
    document that does not add up.
    """
    if invoice.hospital_id != hospital_id:
        raise NotFoundError("Invoice not found.", code="invoice_not_found")
    return await _charges_on(session, invoice.id)


async def issue_invoice(
    session: AsyncSession,
    invoice: Invoice,
    *,
    actor: User | None,
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
    # Snapshotted at issue: a reprint shows the hospital as it was that day.
    invoice.letterhead = await tenancy_service.letterhead_for(session, hospital_id)
    invoice.issued_at = moment
    invoice.issued_by_id = actor.id if actor else None
    if notes:
        invoice.notes = notes
    invoice.updated_at = moment
    session.add(invoice)
    await session.flush()

    await event_bus.publish(
        InvoiceIssued(
            hospital_id=hospital_id,
            actor_id=actor.id if actor else None,
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


# How far ahead of the server's clock a counter's clock may run.
RECEIPT_CLOCK_SKEW = timedelta(minutes=10)


async def _assert_receipt_time(
    session: AsyncSession, received_at: datetime, *, hospital_id: uuid.UUID, may_backdate: bool
) -> None:
    """When a payment says it was received must be believable (SEC-07).

    Never in the future beyond a few minutes of clock drift. Never before
    today, on the hospital's clock, unless the caller holds
    `payment:backdate`: a back-dated receipt moves money into a day whose cash
    has already been counted and handed over. The server's own time is kept
    regardless, as the payment's `created_at`.
    """
    moment = received_at if received_at.tzinfo else received_at.replace(tzinfo=UTC)
    if moment > utc_now() + RECEIPT_CLOCK_SKEW:
        raise ValidationError(
            "A payment cannot be recorded as received in the future.",
            code="received_at_in_future",
        )
    if may_backdate:
        return
    zone = await tenancy_service.local_zone(session, hospital_id)
    start_of_today = datetime.combine(
        await tenancy_service.local_today(session, hospital_id), time.min, zone
    )
    if moment < start_of_today:
        raise ValidationError(
            "A payment can only be recorded as received today. "
            "Ask accounts to enter an earlier date.",
            code="received_at_backdated",
        )


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
    may_backdate: bool = False,
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

    if received_at is not None:
        await _assert_receipt_time(
            session, received_at, hospital_id=hospital_id, may_backdate=may_backdate
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


async def _lock_visit_billing(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> None:
    """Row-lock what a collection is about to settle: pending charges, then open
    invoices — always in that order, so two collections cannot deadlock.

    Postgres re-checks a locked row's filter once the holder commits, so the
    waiting request sees charges already invoiced and invoices already paid
    drop out of its result rather than settling them a second time.
    """
    await session.execute(
        select(col(Charge.id))
        .where(
            col(Charge.hospital_id) == hospital_id,
            col(Charge.encounter_id) == encounter_id,
            col(Charge.status) == ChargeStatus.PENDING,
            col(Charge.deleted_at).is_(None),
        )
        .with_for_update()
    )
    await session.execute(
        select(col(Invoice.id))
        .where(
            col(Invoice.hospital_id) == hospital_id,
            col(Invoice.encounter_id) == encounter_id,
            col(Invoice.status).in_(_OPEN_INVOICE_STATUSES),
            col(Invoice.deleted_at).is_(None),
        )
        .with_for_update()
    )


async def collect_balance(
    session: AsyncSession,
    encounter: Encounter,
    *,
    method: PaymentMethod,
    actor: User,
    hospital_id: uuid.UUID,
    reference: str | None = None,
    payer_name: str | None = None,
    expected_amount: Decimal | None = None,
) -> list[Payment]:
    """Take everything a visit owes in one step — reception's "Paid" tap.

    The billing screen's three steps (assemble, issue, pay) run here in one
    transaction, through the same functions, so numbering, events and the §6
    closure rule behave exactly as they do there. One receipt per invoice
    settled: an invoice issued earlier and a charge that landed since are both
    cleared.

    Refuses an unpriced line rather than issuing a bill with a zero on it — a
    one-tap path is the wrong place to discover that a price is missing.

    `expected_amount` is the figure the counter showed — the amount on the UPI
    QR the patient just paid. If a charge has landed since, the balance no
    longer matches it, and recording the new total against that reference
    would be a receipt for money nobody sent. Refused, so the screen refreshes.

    Two receptionists tapping "Paid" at the same instant: the visit's billing
    rows are locked first, so the second request waits, then finds the bill
    already settled and is refused — never a second receipt.

    An IPD stay is refused outright: its bill is generated by the accountant
    after discharge, and a one-tap path must not issue it behind them.
    """
    if await stay_for(session, encounter.id, hospital_id=hospital_id) is not None:
        raise ConflictError(
            "This patient is admitted. The accounts office prepares the IPD bill.",
            code="ipd_bill_by_accounts",
        )
    await _lock_visit_billing(session, encounter.id, hospital_id=hospital_id)

    if expected_amount is not None:
        due = await open_balance(session, encounter.id)
        if money(expected_amount) != due:
            raise ConflictError(
                f"The bill changed while you were collecting — it is now {due}.",
                code="balance_changed",
                details={"balance_due": str(due)},
            )

    pending, _ = await list_charges(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=hospital_id,
        encounter_id=encounter.id,
        status=ChargeStatus.PENDING,
    )
    draft = await get_draft_for_encounter(session, encounter.id, hospital_id=hospital_id)
    lines = [*pending, *(await _charges_on(session, draft.id) if draft else [])]
    if any(line.needs_pricing for line in lines):
        raise ValidationError(
            "A charge on this visit has no price yet. Price it on the billing screen first.",
            code="unpriced_charges",
        )

    if lines:
        draft = await assemble_draft(
            session,
            InvoiceDraftRequest(encounter_id=encounter.id),
            hospital_id=hospital_id,
            actor=actor,
        )
        await issue_invoice(session, draft, actor=actor, hospital_id=hospital_id)

    unpaid, _ = await list_invoices(
        session,
        PageParams(limit=MAX_PAGE_SIZE),
        hospital_id=hospital_id,
        encounter_id=encounter.id,
        unpaid_only=True,
    )
    payments: list[Payment] = []
    for invoice in unpaid:
        if invoice.status is InvoiceStatus.DRAFT:
            continue  # An empty draft: nothing on it to take money for.
        payment, _ = await record_payment(
            session,
            invoice,
            amount=invoice.balance_due,
            method=method,
            actor=actor,
            hospital_id=hospital_id,
            reference=reference,
            payer_name=payer_name,
        )
        payments.append(payment)

    if not payments:
        raise ConflictError("Nothing is owed on this visit.", code="nothing_due")
    return payments


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
    # An IPD stay: its bill is the accounts office's, not the counter's.
    is_stay: bool = False
    # Lines from before the admission wait to be issued again as the stay's
    # OPD bill (after that bill was cancelled to correct it).
    pre_admission_pending: bool = False


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
    stay = await stay_for(session, encounter.id, hospital_id=hospital_id)

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
        is_stay=stay is not None,
        pre_admission_pending=stay is not None
        and any(charge.captured_at < stay.admitted_at for charge in charges),
    )


# The order a relative asks about a stay: the room, then the medicines, then
# everything else.
_COST_ORDER: tuple[ChargeCategory, ...] = (
    ChargeCategory.ROOM,
    ChargeCategory.PHARMACY,
    ChargeCategory.LAB,
    ChargeCategory.RADIOLOGY,
    ChargeCategory.PROCEDURE,
    ChargeCategory.CONSULTATION,
    ChargeCategory.REGISTRATION,
    ChargeCategory.OTHER,
)


@dataclass(frozen=True, slots=True)
class CostLineData:
    category: ChargeCategory
    description: str
    quantity: Decimal
    unit_price: Decimal
    total_amount: Decimal
    needs_pricing: bool


@dataclass(frozen=True, slots=True)
class CostBreakdownData:
    encounter_id: uuid.UUID
    lines: list[CostLineData]
    total: Decimal
    has_unpriced_items: bool


async def cost_breakdown(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> CostBreakdownData:
    """What a visit has cost so far, in the shape a relative asks about it.

    Every charge that stands counts — pending or already invoiced — because
    "what has this stay cost" does not care whether the counter has printed it
    yet. Cancelled and waived lines do not: nobody owes them.

    Grouped by (category, description, unit price), so twelve doses of
    ceftriaxone are one line of twelve rather than twelve lines, and aggregated
    in SQL rather than by fetching every charge — a fortnight's stay on four
    drugs is several hundred rows, well past one page of `list_charges`.

    For an IPD stay, from the admission on: the consultation before it is its
    own OPD bill (`split_pre_admission`), not part of what the stay cost.
    """
    stay = await stay_for(session, encounter_id, hospital_id=hospital_id)
    since: list[ColumnElement[bool]] = (
        [col(Charge.captured_at) >= stay.admitted_at] if stay is not None else []
    )
    rows = await session.execute(
        sa_select(
            col(Charge.category),
            col(Charge.description),
            col(Charge.unit_price),
            col(Charge.needs_pricing),
            func.sum(col(Charge.quantity)),
            func.sum(col(Charge.total_amount)),
        )
        .where(
            col(Charge.hospital_id) == hospital_id,
            col(Charge.encounter_id) == encounter_id,
            col(Charge.deleted_at).is_(None),
            col(Charge.status).in_([ChargeStatus.PENDING, ChargeStatus.INVOICED]),
            *since,
        )
        .group_by(
            col(Charge.category),
            col(Charge.description),
            col(Charge.unit_price),
            col(Charge.needs_pricing),
        )
    )
    lines = [
        CostLineData(
            category=ChargeCategory(row[0]),
            description=row[1],
            unit_price=row[2],
            needs_pricing=row[3],
            quantity=row[4],
            total_amount=money(row[5]),
        )
        for row in rows
    ]
    lines.sort(key=lambda line: (_COST_ORDER.index(line.category), line.description))
    return CostBreakdownData(
        encounter_id=encounter_id,
        lines=lines,
        total=money(sum((line.total_amount for line in lines), ZERO)),
        has_unpriced_items=any(line.needs_pricing for line in lines),
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


# ---------------------------------------------------------------------------
# The accountant's IPD bill
# ---------------------------------------------------------------------------
# An invoice that is the stay's final bill: issued, and not cancelled.
_FINAL_STATUSES: tuple[InvoiceStatus, ...] = (
    InvoiceStatus.ISSUED,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.PAID,
    InvoiceStatus.WRITTEN_OFF,
)
_ON_THE_BILL: tuple[ChargeStatus, ...] = (ChargeStatus.PENDING, ChargeStatus.INVOICED)


@dataclass(frozen=True, slots=True)
class IpdBillData:
    encounter_id: uuid.UUID
    version: str
    stay: StayInfo
    charges: list[Charge]
    total: Decimal
    has_unpriced_items: bool
    final_invoice: Invoice | None
    can_finalise: bool


@dataclass(frozen=True, slots=True)
class BillChange:
    """One applied change, for the router to audit with its before and after."""

    op: str
    charge: Charge
    before: dict[str, Any]
    after: dict[str, Any]
    reason: str


def _snapshot(charge: Charge) -> dict[str, Any]:
    return {
        "description": charge.description,
        "quantity": str(charge.quantity),
        "unit_price": str(charge.unit_price),
        "discount_amount": str(charge.discount_amount),
        "total_amount": str(charge.total_amount),
        "status": charge.status.value,
    }


async def _require_stay(
    session: AsyncSession, encounter: Encounter, *, hospital_id: uuid.UUID
) -> StayInfo:
    stay = await stay_for(session, encounter.id, hospital_id=hospital_id)
    if stay is None:
        raise ConflictError(
            "An OPD bill is frozen once it is made. Only an IPD stay's bill can be changed.",
            code="opd_bill_locked",
        )
    return stay


async def _stay_charges(
    session: AsyncSession, encounter_id: uuid.UUID, stay: StayInfo, *, hospital_id: uuid.UUID
) -> list[Charge]:
    """Every line from the admission on, waived ones included (shown struck
    through); cancelled acts never happened and are left out."""
    rows = (
        (
            await session.execute(
                select(Charge)
                .where(
                    col(Charge.hospital_id) == hospital_id,
                    col(Charge.encounter_id) == encounter_id,
                    col(Charge.captured_at) >= stay.admitted_at,
                    col(Charge.status) != ChargeStatus.CANCELLED,
                    col(Charge.deleted_at).is_(None),
                )
                .order_by(col(Charge.captured_at), col(Charge.id))
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _final_ipd_invoice(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> Invoice | None:
    return (
        (
            await session.execute(
                select(Invoice)
                .where(
                    col(Invoice.hospital_id) == hospital_id,
                    col(Invoice.encounter_id) == encounter_id,
                    col(Invoice.scope) == InvoiceScope.IPD,
                    col(Invoice.status).in_(_FINAL_STATUSES),
                    col(Invoice.deleted_at).is_(None),
                )
                .order_by(col(Invoice.issued_at).desc())
            )
        )
        .scalars()
        .first()
    )


def _version_of(charges: list[Charge]) -> str:
    latest = max((charge.updated_at for charge in charges), default=None)
    stamp = latest.isoformat() if latest else "none"
    return f"{stamp}#{len(charges)}"


async def bill_version(
    session: AsyncSession, encounter_id: uuid.UUID, *, hospital_id: uuid.UUID
) -> str:
    """The editor's concurrency token: the newest change and the line count.

    Any write to a line bumps its `updated_at`; a line captured or cancelled
    changes the count. So two loads of an unchanged bill agree, and anything
    that touched the stay in between — another accountant, a night's bed-day —
    makes the token stale.
    """
    stay = await stay_for(session, encounter_id, hospital_id=hospital_id)
    if stay is None:
        return ""
    return _version_of(await _stay_charges(session, encounter_id, stay, hospital_id=hospital_id))


async def ipd_bill(
    session: AsyncSession, encounter: Encounter, *, hospital_id: uuid.UUID
) -> IpdBillData:
    """The stay's running bill as the accountant sees it."""
    stay = await _require_stay(session, encounter, hospital_id=hospital_id)
    charges = await _stay_charges(session, encounter.id, stay, hospital_id=hospital_id)
    live = [charge for charge in charges if charge.status in _ON_THE_BILL]
    final = await _final_ipd_invoice(session, encounter.id, hospital_id=hospital_id)
    unpriced = any(charge.needs_pricing for charge in live)
    return IpdBillData(
        encounter_id=encounter.id,
        version=_version_of(charges),
        stay=stay,
        charges=charges,
        total=money(sum((charge.total_amount for charge in live), start=ZERO)),
        has_unpriced_items=unpriced,
        final_invoice=final,
        # After the patient has left (discharge, death, LAMA and referral all
        # close the visit), and only once.
        can_finalise=encounter.is_closed and final is None and bool(live) and not unpriced,
    )


async def _apply_ops(
    session: AsyncSession,
    encounter: Encounter,
    ops: list[BillOp],
    lines: dict[uuid.UUID, Charge],
    *,
    draft_id: uuid.UUID | None,
    actor: User,
    hospital_id: uuid.UUID,
    attach_to: Invoice | None = None,
) -> list[BillChange]:
    """Apply staged changes, in order, to `lines` — the bill being changed.

    A line added joins `attach_to` when given (a corrected bill), or stays
    pending for the stay's running bill to gather. The caller holds the
    savepoint: one bad change raises and none of the others land.
    """
    applied: list[BillChange] = []
    for op in ops:
        if isinstance(op, BillAdd):
            added = await add_charge(
                session,
                ChargeAdd(
                    encounter_id=encounter.id,
                    service_item_code=op.service_item_code,
                    description=op.description,
                    category=op.category,
                    quantity=op.quantity,
                    unit_price=op.unit_price,
                ),
                hospital_id=hospital_id,
                actor=actor,
            )
            if attach_to is not None:
                added.status = ChargeStatus.INVOICED
                added.invoice_id = attach_to.id
                added.updated_at = utc_now()
                session.add(added)
                await session.flush()
            lines[added.id] = added
            applied.append(BillChange("add", added, {}, _snapshot(added), op.reason))
            continue

        charge = lines.get(op.charge_id)
        if charge is None:
            raise NotFoundError("That line is not on this bill.", code="charge_not_found")
        before = _snapshot(charge)
        if isinstance(op, BillEdit):
            charge = await edit_charge(
                session,
                charge,
                quantity=op.quantity,
                unit_price=op.unit_price,
                description=op.description,
                draft_id=draft_id,
            )
        elif isinstance(op, BillDiscount):
            charge = await discount_charge(
                session,
                charge,
                amount=op.amount,
                reason=op.reason,
                actor=actor,
                draft_id=draft_id,
            )
        elif isinstance(op, BillRemove):
            charge = await waive_charge(
                session, charge, reason=op.reason, actor=actor, draft_id=draft_id
            )
        applied.append(BillChange(op.op, charge, before, _snapshot(charge), op.reason))
    return applied


_CORRECTABLE: tuple[InvoiceStatus, ...] = (
    InvoiceStatus.ISSUED,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.PAID,
)


async def correct_invoice(
    session: AsyncSession,
    invoice: Invoice,
    ops: list[BillOp],
    *,
    reason: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> tuple[Invoice, list[BillChange]]:
    """Correct an issued bill: a new bill replaces it. All of it, or none.

    An issued bill is in the GST series and in the patient's hand, so it is
    never changed in place. Its lines move to a new bill, the staged changes
    are applied there, the new bill is issued under its own number, the
    receipts move onto it, and the old bill is cancelled with the reason and
    the new number. Refused when the corrected total is less than what was
    already received: that is a refund, which starts with reversing the
    payment.

    Returns the new bill and the changes applied, for the router to audit.
    """
    if invoice.status not in _CORRECTABLE:
        raise ConflictError(
            "Only an issued bill is corrected this way.", code="invoice_not_correctable"
        )
    encounter = await clinical_service.get_encounter(
        session, invoice.encounter_id, hospital_id=hospital_id
    )
    await _lock_visit_billing(session, encounter.id, hospital_id=hospital_id)
    invoice = await get_invoice(session, invoice.id, hospital_id=hospital_id, for_update=True)
    if invoice.status not in _CORRECTABLE:
        raise ConflictError(
            "Only an issued bill is corrected this way.", code="invoice_not_correctable"
        )
    if await get_draft_for_encounter(
        session, encounter.id, hospital_id=hospital_id, scope=invoice.scope
    ):
        raise ConflictError(
            "This visit has a draft bill open. Issue or clear it before correcting.",
            code="bill_has_open_draft",
        )

    async with session.begin_nested():
        replacement = await _new_draft(
            session,
            encounter,
            InvoiceDraftRequest(
                encounter_id=encounter.id,
                rate_card_id=invoice.rate_card_id,
                payer_type=invoice.payer_type,
                payer_liability=invoice.payer_liability,
                place_of_supply=invoice.place_of_supply,
                is_interstate=invoice.is_interstate,
                notes=invoice.notes,
            ),
            scope=invoice.scope,
        )
        lines: dict[uuid.UUID, Charge] = {}
        for charge in await _charges_on(session, invoice.id):
            charge.invoice_id = replacement.id
            charge.updated_at = utc_now()
            session.add(charge)
            lines[charge.id] = charge
        await session.flush()

        changes = await _apply_ops(
            session,
            encounter,
            ops,
            lines,
            draft_id=replacement.id,
            actor=actor,
            hospital_id=hospital_id,
            attach_to=replacement,
        )
        replacement = await _recompute_totals(session, replacement)
        if any(
            charge.needs_pricing and charge.status is not ChargeStatus.WAIVED
            for charge in await _charges_on(session, replacement.id)
        ):
            raise ValidationError(
                "A line on this bill has no price yet. Price it before issuing.",
                code="unpriced_charges",
            )
        received = invoice.amount_paid
        if replacement.grand_total < received:
            raise ConflictError(
                f"{received} has been received on this bill and the corrected total is "
                f"{replacement.grand_total}. Reverse the payment first.",
                code="correction_below_paid",
                details={
                    "amount_paid": str(received),
                    "grand_total": str(replacement.grand_total),
                },
            )
        replacement = await issue_invoice(
            session, replacement, actor=actor, hospital_id=hospital_id, notes=invoice.notes
        )

        # The receipts follow the bill they pay: the patient's receipt numbers
        # stay as printed, now against the corrected bill.
        for payment in (
            (
                await session.execute(
                    select(Payment).where(
                        col(Payment.invoice_id) == invoice.id,
                        col(Payment.deleted_at).is_(None),
                    )
                )
            )
            .scalars()
            .all()
        ):
            payment.invoice_id = replacement.id
            payment.updated_at = utc_now()
            session.add(payment)
        await session.flush()
        replacement = await _apply_receipts(session, replacement)
        # Nothing is received on the old bill now, so it may be cancelled.
        invoice = await _apply_receipts(session, invoice)
        assert_invoice_transition(invoice.status, InvoiceStatus.CANCELLED)
        invoice.status = InvoiceStatus.CANCELLED
        invoice.cancellation_reason = (f"{reason} — corrected by {replacement.invoice_number}")[
            :500
        ]
        invoice.balance_due = ZERO
        invoice.updated_at = utc_now()
        session.add(invoice)
        await session.flush()
    return replacement, changes


async def apply_bill_changes(
    session: AsyncSession,
    encounter: Encounter,
    ops: list[BillOp],
    *,
    expected_version: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> list[BillChange]:
    """Apply everything the accountant staged, in order, all or nothing.

    Inside a savepoint: one bad change — a discount bigger than its line, a
    line already on the final bill — raises, and none of the others land. The
    stay's billing rows are locked before the version is compared, so two
    accountants saving at once cannot both pass the check.
    """
    stay = await _require_stay(session, encounter, hospital_id=hospital_id)
    await _lock_visit_billing(session, encounter.id, hospital_id=hospital_id)
    if await _final_ipd_invoice(session, encounter.id, hospital_id=hospital_id) is not None:
        raise ConflictError(
            "The final bill for this stay has been generated. Cancel it to make corrections.",
            code="ipd_bill_final",
        )
    current = await bill_version(session, encounter.id, hospital_id=hospital_id)
    if current != expected_version:
        raise ConflictError(
            "This bill changed since you opened it. It has been reloaded with the latest.",
            code="bill_changed",
            details={"version": current},
        )

    draft = await get_draft_for_encounter(
        session, encounter.id, hospital_id=hospital_id, scope=InvoiceScope.IPD
    )
    draft_id = draft.id if draft else None
    on_stay = {
        charge.id: charge
        for charge in await _stay_charges(session, encounter.id, stay, hospital_id=hospital_id)
    }
    async with session.begin_nested():
        applied = await _apply_ops(
            session,
            encounter,
            ops,
            on_stay,
            draft_id=draft_id,
            actor=actor,
            hospital_id=hospital_id,
        )
        if draft is not None:
            # One running document: new lines join the draft, and its footer is
            # re-added from the corrected lines.
            await _gather_pending(session, draft, since=stay.admitted_at)
            await _recompute_totals(session, draft)
    return applied


async def cancel_final_ipd_bill(
    session: AsyncSession,
    encounter: Encounter,
    *,
    reason: str,
    actor: User,
    hospital_id: uuid.UUID,
) -> Invoice:
    """Void the stay's final bill so it can be corrected and generated again.

    Its lines return to the stay's running bill, editable again. Refused once
    money has been received against it, like any invoice: reverse first.
    """
    await _require_stay(session, encounter, hospital_id=hospital_id)
    await _lock_visit_billing(session, encounter.id, hospital_id=hospital_id)
    final = await _final_ipd_invoice(session, encounter.id, hospital_id=hospital_id)
    if final is None:
        raise NotFoundError("This stay has no final bill to cancel.", code="no_final_bill")
    return await cancel_invoice(session, final, reason=reason, actor=actor, hospital_id=hospital_id)


async def finalise_ipd_bill(
    session: AsyncSession, encounter: Encounter, *, actor: User, hospital_id: uuid.UUID
) -> Invoice:
    """Generate the stay's final bill: gather what is left and issue it.

    Only once the patient has left. Discharge, death, LAMA and referral all
    close the visit, and discharge accrues the last night before it does, so
    by now nothing more is coming. Refused with an unpriced line: a final bill
    with a zero on it goes to the tax filing as a zero. Once issued it is
    frozen like any invoice; a correction is cancel, edit, generate again.
    """
    await _require_stay(session, encounter, hospital_id=hospital_id)
    if not encounter.is_closed:
        raise ConflictError(
            "The patient is still admitted. The final bill is generated after discharge.",
            code="stay_not_discharged",
        )
    await _lock_visit_billing(session, encounter.id, hospital_id=hospital_id)
    if await _final_ipd_invoice(session, encounter.id, hospital_id=hospital_id) is not None:
        raise ConflictError(
            "The final bill for this stay has already been generated.", code="ipd_bill_final"
        )
    bill = await ipd_bill(session, encounter, hospital_id=hospital_id)
    if bill.has_unpriced_items:
        raise ValidationError(
            "A charge on this stay has no price yet. Price it before the final bill.",
            code="unpriced_charges",
        )
    draft = await assemble_draft(
        session,
        InvoiceDraftRequest(encounter_id=encounter.id),
        hospital_id=hospital_id,
        actor=actor,
    )
    return await issue_invoice(session, draft, actor=actor, hospital_id=hospital_id)
