"""Where diagnostics listens to the rest of the hospital.

One subscriber, and it closes the gap this module shipped with in Phase 6.

Cancelling a lab order from the clinical side used to leave the diagnostics
report sitting on the lab worklist. Nothing unsafe followed — `verify_report`
refuses a report whose order was cancelled — but somebody had to notice the
stale row and cancel it by hand, and a worklist people learn to ignore entries
on is a worklist that stops working.

The fix needed the event bus to carry a session so a subscriber could safely
write, which is what Phase 7 added for `billing`. It is transactional for the
same reason billing's are: the report and the order live in the same database
and the same transaction, so there is no independent failure to absorb, and a
swallowed exception would just put the stale row back.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import event_bus
from app.modules.clinical.events import OrderCancelled
from app.modules.clinical.models import OrderType
from app.modules.diagnostics import service

logger = logging.getLogger(__name__)

__all__ = ["register_handlers", "withdraw_cancelled_order"]

_OURS = frozenset({OrderType.LAB.value, OrderType.RADIOLOGY.value})


async def withdraw_cancelled_order(event: OrderCancelled, session: AsyncSession) -> None:
    """Take the report and its sample off the bench when the order is withdrawn.

    Deliberately calls `cancel_report_for_order` rather than `cancel_report`:
    the latter cancels the clinical order too, which is what published this
    event, and the pair would recurse. A report that has already been signed is
    left alone — somebody may have acted on it, and unpicking that is an
    amendment with a reason, not a side effect.
    """
    if event.order_type not in _OURS or event.hospital_id is None:
        return

    report = await service.cancel_report_for_order(
        session,
        order_id=event.order_id,
        hospital_id=event.hospital_id,
        reason=event.reason or "The clinical order was cancelled.",
    )
    if report is not None:
        logger.info("withdrew report %s after its order was cancelled", report.report_number)


def register_handlers() -> None:
    """Wire diagnostics into the bus. Called once, from `app.registry`."""
    event_bus.subscribe_transactional(OrderCancelled, withdraw_cancelled_order)
    logger.debug("diagnostics handlers registered")
