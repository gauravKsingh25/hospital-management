"""Where billing listens to the rest of the hospital.

CLAUDE.md §7b: *"charges flow automatically from consultation, lab, radiology,
pharmacy via the event bus; reception reviews the invoice, never rebuilds it."*
This module is the first half of that sentence.

Every subscriber here is **transactional** (see `core/events.py`). They run
inside the publisher's transaction, holding its session, and their failures
propagate. That is the opposite of the fire-and-forget default, and the reason
is worth repeating where somebody will read it while changing this file: a
handler writing a row to the same database in the same transaction has no
independent failure domain. If the INSERT fails, the publisher's own INSERT was
going to fail too — so swallowing the exception buys no resilience and loses a
charge for care that was actually delivered. Unbilled care is silent revenue
loss, silent precisely because nobody got an error.

The obligation this places on the handlers is that they must never fail for a
reason the hospital can cause. No rate card, no price, no mapped service —
`capture_charge` records the act at zero and flags it, rather than raising. A
clinic that has not finished configuring its price list must still be able to
order a blood test.

Two things deliberately do **not** capture a charge here:

* **Pharmacy orders.** A prescription's price depends on what is actually
  dispensed and in what quantity, which is not known at ordering time. The
  charge belongs to the dispensing event, and the pharmacy module lands in
  Phase 9. Capturing at order time would bill patients for drugs they never
  collected.
* **Referral orders.** Writing a referral letter is not a billable act in
  itself; any ambulance or procedure attached to it is charged on its own.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.events import event_bus
from app.modules.billing import service
from app.modules.billing.models import ChargeCategory
from app.modules.clinical import service as clinical_service
from app.modules.clinical.events import (
    EncounterClosed,
    EncounterOpened,
    OrderCancelled,
    OrderPlaced,
)
from app.modules.clinical.models import Encounter, EncounterType, OrderType
from app.modules.ipd.events import BedDayAccrued

logger = logging.getLogger(__name__)

__all__ = ["capture_bed_day", "outstanding_balance", "register_handlers"]

# Which clinical order types produce a charge the moment they are placed.
# Pharmacy and referral are absent on purpose — see the module docstring.
_BILLABLE_ORDER_TYPES: dict[str, ChargeCategory] = {
    OrderType.LAB.value: ChargeCategory.LAB,
    OrderType.RADIOLOGY.value: ChargeCategory.RADIOLOGY,
    OrderType.PROCEDURE.value: ChargeCategory.PROCEDURE,
}

# Visit types that attract a consultation fee at registration. An IPD stay bills
# per bed-day (`capture_bed_day`, below) and per procedure instead.
_CONSULTATION_TYPES = frozenset({EncounterType.OPD.value, EncounterType.EMERGENCY.value})


async def capture_consultation(event: EncounterOpened, session: AsyncSession) -> None:
    """Charge the consultation fee when a visit opens.

    At registration rather than at the end, because that is when an Indian OPD
    patient actually pays — they are handed a receipt on the way in, not chased
    on the way out. It also means the fee is on the bill even if the patient
    never reaches the consulting room, which is the case a hospital most often
    loses money on.
    """
    if event.encounter_type not in _CONSULTATION_TYPES:
        return
    if event.hospital_id is None:
        return

    await service.capture_charge(
        session,
        hospital_id=event.hospital_id,
        encounter_id=event.encounter_id,
        patient_id=event.patient_id,
        source_module="clinical",
        source_type="encounter",
        source_id=event.encounter_id,
        description=f"{event.encounter_type.title()} consultation",
        category=ChargeCategory.CONSULTATION,
        doctor_id=event.doctor_id,
        actor_id=event.actor_id,
    )


async def capture_order(event: OrderPlaced, session: AsyncSession) -> None:
    """Charge a lab test, a scan or a procedure when the doctor orders it.

    Keyed on the order id, so the charge and the clinical act are the same fact
    seen from two sides. `item_code` is what maps it onto a priced service; when
    it maps onto nothing the charge still lands, flagged for pricing.
    """
    category = _BILLABLE_ORDER_TYPES.get(event.order_type)
    if category is None or event.hospital_id is None:
        return

    await service.capture_charge(
        session,
        hospital_id=event.hospital_id,
        encounter_id=event.encounter_id,
        patient_id=event.patient_id,
        source_module="clinical",
        source_type="order",
        source_id=event.order_id,
        description=event.item_name,
        category=category,
        item_code=event.item_code,
        actor_id=event.actor_id,
    )


async def capture_bed_day(event: BedDayAccrued, session: AsyncSession) -> None:
    """Charge one night in one bed, as the nightly sweep accrues it.

    Lives here rather than in `ipd` for the reason this file exists at all:
    billing owns charge capture, and it is the first extraction candidate
    (CLAUDE.md §2). A handler listening on the bus lifts out into its own
    service unchanged; `ipd` reaching into `billing.service` directly would not.

    Keyed on the accrual key — a UUIDv5 of (admission, date) — so the sweep can
    restart mid-pass or run twice after a deploy without billing the patient
    twice for one bed. Same idempotency guarantee every other charge relies on,
    reached without a table whose only job is remembering what was billed.
    """
    if event.hospital_id is None:
        return

    await service.capture_charge(
        session,
        hospital_id=event.hospital_id,
        encounter_id=event.encounter_id,
        patient_id=event.patient_id,
        source_module="ipd",
        source_type="bed_day",
        source_id=event.accrual_key,
        description=event.description,
        category=ChargeCategory.ROOM,
        item_code=event.tariff_item_code,
        quantity=Decimal(event.nights),
    )


async def void_cancelled_order(event: OrderCancelled, session: AsyncSession) -> None:
    """Take the charge off the bill when the order is called off.

    Only while it is still pending. Once the line is on an invoice the patient
    may be holding a printout of it, and the remedy is a credit note rather than
    a silent edit — `cancel_charges_for_source` refuses and says so.
    """
    if event.hospital_id is None:
        return

    await service.cancel_charges_for_source(
        session,
        hospital_id=event.hospital_id,
        source_module="clinical",
        source_type="order",
        source_id=event.order_id,
        reason=event.reason or "Order cancelled.",
    )


async def settle_closed_visit(event: EncounterClosed, session: AsyncSession) -> None:
    """Run the settlement flow when a visit reaches a terminal status.

    `requires_settlement` is true for deceased, referred and LAMA patients as
    well as completed ones — CLAUDE.md §6 is explicit that the bill does not
    leave with the patient. What happens is that any loose charge is gathered
    onto a draft and every open invoice is stamped with *why* somebody has to
    look at it; nothing is issued automatically.
    """
    if not event.requires_settlement or event.hospital_id is None:
        return

    await service.settle_on_closure(
        session,
        hospital_id=event.hospital_id,
        encounter_id=event.encounter_id,
        patient_id=event.patient_id,
        final_status=event.final_status,
        actor_id=event.actor_id,
    )


# ---------------------------------------------------------------------------
# The closure gate (CLAUDE.md §6)
# ---------------------------------------------------------------------------
async def outstanding_balance(
    session: AsyncSession, encounter: Encounter
) -> clinical_service.PendingContribution | None:
    """Billing's answer to "is anything of yours still holding this visit open?".

    Registered into `clinical.service`'s pending-item seam, which is what the
    seam was built for. The cashier is one of the clearance stations CLAUDE.md
    §6 names by hand, so an unpaid bill keeps a visit in `PENDING_CLEARANCE`
    until somebody takes the money, waives the charge or writes the bill off —
    and the auto-close sweep surfaces it rather than tidying it away.

    `BILLING_BLOCKS_ENCOUNTER_CLOSURE` turns the blocking half off while leaving
    the balance visible on the screen. A hospital that runs corporate credit
    accounts, where patients legitimately leave and finance invoices the
    employer next month, wants that; a cash OPD does not.
    """
    balance = await service.open_balance(session, encounter.id)
    if balance <= 0:
        return None

    return clinical_service.PendingContribution(
        source="BILLING",
        count=1,
        descriptions=(f"Payment: {balance} outstanding at the counter",),
        blocks_closure=settings.BILLING_BLOCKS_ENCOUNTER_CLOSURE,
    )


def register_handlers() -> None:
    """Wire billing into the rest of the system. Called once, at import.

    Idempotent by construction on the pending-provider side (keyed by source);
    the event subscriptions are registered from `app.registry`, which is
    imported exactly once per process.
    """
    event_bus.subscribe_transactional(EncounterOpened, capture_consultation)
    event_bus.subscribe_transactional(OrderPlaced, capture_order)
    event_bus.subscribe_transactional(OrderCancelled, void_cancelled_order)
    event_bus.subscribe_transactional(BedDayAccrued, capture_bed_day)
    event_bus.subscribe_transactional(EncounterClosed, settle_closed_visit)

    clinical_service.register_pending_provider("BILLING", outstanding_balance)
    logger.debug("billing handlers registered")
