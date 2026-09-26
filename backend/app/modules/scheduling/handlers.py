"""`scheduling`'s subscribers.

One job: **take the token off the board when the visit ends.**

Nothing did this before. Queue entries were closed only by cancelling or
no-showing the *appointment*, so every visit that actually finished — completed
by the doctor, admitted to a ward, or ended by one of CLAUDE.md §6's three
unplanned exits — left its token on the live queue as `WAITING`, permanently.
The OPD board never emptied.

That stayed invisible for a long time because every screen reading the queue
looks for a named patient rather than at the whole list, and because the
end-to-end suite has a reset script that hid the accumulation. It became
impossible to miss the moment the terminal-outcome screens landed: a patient
recorded as deceased was still sitting first in line, waiting for a doctor.

The wiring is an event subscription rather than a call from `clinical`, because
that is the boundary CLAUDE.md §2 draws — `clinical` must not know that a queue
exists, and after extraction this handler is a message consumer with no code
change. It is the same shape `ipd` uses to free a bed on the same event.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import event_bus
from app.modules.clinical.events import EncounterAdmitted, EncounterClosed
from app.modules.scheduling import service

logger = logging.getLogger(__name__)

__all__ = [
    "close_queue_on_admission",
    "close_queue_on_encounter_close",
    "register_handlers",
]


async def close_queue_on_encounter_close(event: EncounterClosed, session: AsyncSession) -> None:
    """A finished visit is not a patient still waiting to be seen."""
    if event.appointment_id is None:
        # A casualty or direct-admission encounter opened without an
        # appointment has no token to close. Common and unremarkable.
        return

    closed = await service.close_queue_for_appointment(
        session, event.appointment_id, was_seen=event.was_seen
    )
    if closed:
        logger.info(
            "closed %d queue entr%s after the visit ended as %s",
            closed,
            "y" if closed == 1 else "ies",
            event.final_status,
        )


async def close_queue_on_admission(event: EncounterAdmitted, session: AsyncSession) -> None:
    """A patient in a bed is not a patient in the corridor.

    `ADMITTED` is not a terminal encounter status, so `EncounterClosed` never
    fires for it — which is exactly how admitted patients came to be the
    largest group stuck on the board.
    """
    if event.appointment_id is None:
        return

    closed = await service.close_queue_for_appointment(
        session, event.appointment_id, was_seen=event.was_seen
    )
    if closed:
        logger.info("closed %d queue entr%s on admission", closed, "y" if closed == 1 else "ies")


def register_handlers() -> None:
    """Wire `scheduling` onto the event bus. Called once, at import."""
    event_bus.subscribe_transactional(EncounterClosed, close_queue_on_encounter_close)
    event_bus.subscribe_transactional(EncounterAdmitted, close_queue_on_admission)
    logger.debug("scheduling handlers registered")
