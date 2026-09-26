"""ARQ worker entry point.

Run with:  ``arq app.workers.main.WorkerSettings``

Kept as its own process rather than a thread inside the API. A background job
that shares the web process competes with request handling for the event loop,
and the first thing that suffers is the latency of the screen a receptionist is
staring at. It also means the API can be scaled independently of the jobs, which
is the usual reason to want either.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.database import dispose_engine, get_engine
from app.core.logging import configure_logging
from app.workers.tasks import run_sweeps

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    get_engine()
    logger.info(
        "worker started environment=%s sweep_interval=%dm auto_close_after=%dh",
        settings.ENVIRONMENT,
        settings.WORKER_SWEEP_INTERVAL_MINUTES,
        settings.ENCOUNTER_AUTO_CLOSE_HOURS,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    logger.info("worker shutdown complete")


def _sweep_minutes() -> set[int]:
    """Which minutes of the hour the sweep fires on.

    A rolling interval rather than a nightly wall-clock run: "end of day" needs
    a timezone, a hospital's day does not end at midnight UTC, and the guarantee
    we actually want — "closed N hours after the patient arrived" — does not
    need to know what time it is anywhere.
    """
    interval = min(settings.WORKER_SWEEP_INTERVAL_MINUTES, 60)
    return {minute for minute in range(60) if minute % interval == 0}


class WorkerSettings:
    """ARQ configuration. Discovered by name — do not rename without updating
    the Docker command and the README."""

    functions: ClassVar[list[Any]] = [run_sweeps]
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            run_sweeps,
            minute=_sweep_minutes(),
            # A sweep that overlaps itself would try to close the same encounter
            # twice; the state machine would refuse the second, but a stream of
            # logged 409s is not a healthy signal to teach people to ignore.
            run_at_startup=False,
            max_tries=1,
        )
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    on_startup = startup
    on_shutdown = shutdown
    # Sweeps are sequential per tenant already; no benefit to more concurrency.
    max_jobs = 4
    job_timeout = 300
