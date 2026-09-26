"""Scheduled maintenance jobs.

Six safety nets live here. Most exist because real staff on a real shift forget
clicks, and the rest because providers have bad afternoons and money accrues
whether or not anybody remembers to charge for it:

* **Encounter auto-close** (CLAUDE.md §6) — a visit left in a non-terminal state
  with nothing outstanding is closed after a configurable buffer.
* **Overdue no-shows** — appointments nobody turned up for are written off. This
  was built with `scheduling` in Phase 3 and deliberately left unscheduled until
  there was a worker to run it; this is that worker.
* **Failed-message retry** — messages that every channel refused get another
  walk down the ladder, up to `NOTIFICATION_MAX_ATTEMPTS`. Note this is *not* a
  transactional outbox: these rows already exist and already failed, so it is
  resilience on an existing record rather than a second delivery path. The
  dispatcher re-checks suppression before every send, which is what makes it
  safe for a message queued days ago to go out now.
* **Bed-day accrual** — one room charge per inpatient per night, so the running
  bill is live rather than assembled in a panic at discharge. Keyed on a
  deterministic UUID of (admission, date), which is what makes a re-run harmless.
* **Missed-dose marking** — a scheduled dose that passed its window with nothing
  recorded becomes `MISSED`, flagged `auto_missed`. This is the sweep that turns
  silence into a record: a chart where nobody wrote anything looks identical to a
  chart where nothing was due, and only one of those is safe.
* **Chart top-up** — extends every active medication order to the materialisation
  horizon, so tomorrow's doses exist before tomorrow's night shift needs them.

All six are *idempotent* and *per tenant*. Idempotent because a job that runs
twice after a restart must not do damage; per tenant because they run under
row-level security like everything else, with no cross-hospital query anywhere.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from app.core.config import settings
from app.core.database import session_scope, tenant_session
from app.core.pagination import MAX_PAGE_SIZE, PageParams
from app.modules.clinical import service as clinical_service
from app.modules.ipd import service as ipd_service
from app.modules.notifications import service as notifications_service
from app.modules.scheduling import service as scheduling_service
from app.modules.tenancy import service as tenancy_service

logger = logging.getLogger(__name__)

__all__ = [
    "run_sweeps",
    "sweep_hospital",
    "sweep_ipd",
    "sweep_no_shows",
    "sweep_notifications",
]

# How long a failed message rests before the sweep tries it again. Long enough
# that a provider blip has passed, short enough that "your report is ready" is
# still worth receiving. Not configurable on purpose — one more knob whose right
# value is the same everywhere is one more thing to get wrong.
NOTIFICATION_RETRY_AFTER = timedelta(minutes=15)


async def _active_hospital_ids() -> list[uuid.UUID]:
    """Every tenant the sweep should visit.

    Read through `tenancy.service`, which is the only module allowed to look
    across tenants (CLAUDE.md §3). Collected into a plain list and the session
    closed before any tenant work begins, so no cross-tenant session is ever
    held open while writing tenant data.
    """
    async with session_scope() as session:
        hospitals, _ = await tenancy_service.list_hospitals(
            session, PageParams(limit=MAX_PAGE_SIZE), include_inactive=False
        )
        return [hospital.id for hospital in hospitals]


async def sweep_hospital(hospital_id: uuid.UUID) -> dict[str, int]:
    """Run every safety net for one tenant."""
    buffer = timedelta(hours=settings.ENCOUNTER_AUTO_CLOSE_HOURS)

    async with tenant_session(hospital_id) as session:
        encounters = await clinical_service.close_stale_encounters(
            session, hospital_id=hospital_id, older_than=buffer
        )
        no_shows = await scheduling_service.mark_overdue_no_shows(session, hospital_id=hospital_id)
        messages = await notifications_service.retry_failed(
            session, hospital_id=hospital_id, older_than=NOTIFICATION_RETRY_AFTER
        )
        inpatient = await _sweep_ipd(hospital_id, session)

    return {**encounters, "appointments_no_show": no_shows, **messages, **inpatient}


async def _sweep_ipd(hospital_id: uuid.UUID, session: Any) -> dict[str, int]:
    """The three inpatient nets, in the order that matters.

    Doses are marked missed **before** the chart is topped up, so a slot created
    seconds ago cannot be swept as overdue by the same pass. Accrual runs last
    because it is the only one that publishes events other modules act on, and a
    failure there should not cost the ward its chart.
    """
    missed = await ipd_service.mark_missed_doses(
        session, hospital_id=hospital_id, grace=timedelta(hours=settings.IPD_DOSE_GRACE_HOURS)
    )
    topped_up = await ipd_service.top_up_charts(session, hospital_id=hospital_id)
    accrued = await ipd_service.accrue_bed_days(session, hospital_id=hospital_id)
    return {"doses_missed": missed, "dose_slots_created": topped_up, **accrued}


async def sweep_notifications(hospital_id: uuid.UUID) -> dict[str, int]:
    """Retry failed messages for one tenant, on its own."""
    async with tenant_session(hospital_id) as session:
        return await notifications_service.retry_failed(
            session, hospital_id=hospital_id, older_than=NOTIFICATION_RETRY_AFTER
        )


async def sweep_no_shows(hospital_id: uuid.UUID) -> int:
    """Write off appointments nobody arrived for, for one tenant."""
    async with tenant_session(hospital_id) as session:
        return await scheduling_service.mark_overdue_no_shows(session, hospital_id=hospital_id)


async def sweep_ipd(hospital_id: uuid.UUID) -> dict[str, int]:
    """Run the inpatient nets for one tenant, on their own."""
    async with tenant_session(hospital_id) as session:
        return await _sweep_ipd(hospital_id, session)


async def run_sweeps(ctx: dict[str, Any] | None = None) -> dict[str, int]:
    """The scheduled entry point. Returns a per-run tally for the job log.

    One tenant failing must not stop the rest: a single hospital with a wedged
    row would otherwise silently stop every other hospital's visits from ever
    being closed.
    """
    if not settings.WORKER_SWEEPS_ENABLED:
        logger.info("sweeps disabled by configuration")
        return {}

    totals: dict[str, int] = {}
    for hospital_id in await _active_hospital_ids():
        try:
            result = await sweep_hospital(hospital_id)
        except Exception:
            logger.exception("sweep failed for hospital %s", hospital_id)
            totals["failed_hospitals"] = totals.get("failed_hospitals", 0) + 1
            continue
        for key, value in result.items():
            totals[key] = totals.get(key, 0) + value

    if any(totals.values()):
        logger.info("sweep complete %s", totals)
    return totals
