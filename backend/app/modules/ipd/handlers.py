"""Where `ipd` listens to the rest of the hospital.

One subscriber, and it exists to keep a single promise: **there is one way to
record a death, not two.**

A patient dying on a ward, or walking out against advice, ends the stay. But
neither is recorded here. `clinical.record_death` and `clinical.record_lama` own
those, because they capture the structured metadata CLAUDE.md §6 requires — the
certifying doctor, the cause, whether the LAMA form was signed — and a second,
thinner path through an IPD endpoint would inevitably be the one somebody used
on a busy night. So `ipd` follows: it hears the Encounter reach a terminal
status and closes the admission behind it, freeing the bed and stopping the drug
chart.

The subscription is **transactional** (see `core/events.py`). It writes to the
same database in the same transaction as the publisher, so it has no independent
failure domain — and the thing it is writing is a freed bed. A swallowed failure
here leaves a dead patient occupying an ICU bed on the board, which is a worse
outcome than the death record failing loudly and being retried.

`ipd` deliberately registers **no** pending-item provider. The obvious candidate
— "the bed has not been released" — would never fire: the closure gate is
consulted by `complete_consultation` and the auto-close sweep, and neither runs
against an `ADMITTED` encounter (the sweep excludes it explicitly, because an
inpatient on day five is not a stale visit). A provider that cannot fire is
worse than none, because it reads like a guarantee. Unsigned discharge summaries
are surfaced as a worklist instead — `GET /ipd/summaries?status=DRAFT`.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import event_bus
from app.modules.clinical import service as clinical_service
from app.modules.clinical.events import EncounterClosed
from app.modules.clinical.models import EncounterStatus
from app.modules.ipd import service

logger = logging.getLogger(__name__)

__all__ = ["close_admission_on_encounter_close", "register_handlers"]

# Terminal statuses that can be reached from `ADMITTED` without this module
# driving the transition. `COMPLETED` is absent on purpose: that one *is* driven
# from here, by `discharge_patient`, and by the time the event arrives the
# admission is already closed — `close_admission_for_terminal_encounter` would
# no-op, but not listing it says so plainly.
_WARD_EXITS = frozenset(
    {
        EncounterStatus.DECEASED.value,
        EncounterStatus.LAMA.value,
        EncounterStatus.REFERRED_OUT.value,
    }
)


async def close_admission_on_encounter_close(event: EncounterClosed, session: AsyncSession) -> None:
    """Free the bed and stop the chart when a stay ends somewhere else.

    Cheap to reach and cheap to ignore: the lookup is by encounter id on an
    indexed column, and an encounter that was never admitted finds nothing. Most
    closures in a hospital are OPD visits, so this returns immediately for the
    overwhelming majority of events it sees.
    """
    if event.hospital_id is None or event.final_status not in _WARD_EXITS:
        return

    admission = await service.close_admission_for_terminal_encounter(
        session,
        hospital_id=event.hospital_id,
        encounter_id=event.encounter_id,
        final_status=event.final_status,
        actor_id=event.actor_id,
    )
    if admission is not None:
        logger.info(
            "freed bed and stopped chart for %s after %s",
            admission.admission_number,
            event.final_status,
        )


def register_handlers() -> None:
    """Wire `ipd` into the rest of the system. Called once, at import."""
    event_bus.subscribe_transactional(EncounterClosed, close_admission_on_encounter_close)
    # A patient waiting at the admission desk holds their OPD visit open.
    clinical_service.register_pending_provider("ADMISSION", service.pending_admission_request)
    logger.debug("ipd handlers registered")
