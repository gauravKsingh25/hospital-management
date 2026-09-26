"""FastAPI application factory and router registration.

Module routers are mounted in `_register_routers` as each module lands
(Phase 2 onwards). Phase 0 exposes health endpoints only.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import (
    assert_rls_enforced,
    check_database,
    dispose_engine,
    get_engine,
)
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.redis import check_redis, close_redis
from app.modules.billing import router as billing_router
from app.modules.clinical import router as clinical_router
from app.modules.diagnostics import router as diagnostics_router
from app.modules.identity import router as identity_router
from app.modules.ipd import router as ipd_router
from app.modules.notifications import router as notifications_router
from app.modules.patients import router as patients_router
from app.modules.reporting import router as reporting_router
from app.modules.scheduling import router as scheduling_router
from app.modules.tenancy import router as tenancy_router

# Imported for its side effects: it registers every module's tables AND every
# event subscription. Importing a module's router is not enough — a `billing`
# that is loaded but not subscribed would drop charges silently.
from app.registry import target_metadata  # noqa: F401

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info(
        "starting %s environment=%s debug=%s",
        settings.APP_NAME,
        settings.ENVIRONMENT,
        settings.DEBUG,
    )

    # Warm the pool so the first real request does not eat Neon's cold start.
    # A failure here is logged, not fatal: the API should still come up and
    # report unhealthy rather than crash-loop while the database wakes.
    get_engine()
    try:
        status = await check_database()
        logger.info(
            "database ready host=%s version=%s latency=%sms pooled=%s role=%s rls_enforced=%s",
            status["host"],
            status["server_version"],
            status["latency_ms"],
            status["pooled"],
            status["role"],
            status["rls_enforced"],
        )
        # Refuses to boot in production if the role can bypass RLS.
        await assert_rls_enforced()
    except Exception as exc:  # noqa: BLE001 - startup must not hard-fail on this
        logger.error("database not reachable at startup: %s", exc)

    yield

    await close_redis()
    await dispose_engine()
    logger.info("shutdown complete")


# --- Health ----------------------------------------------------------------
health_router = APIRouter(tags=["health"])


@health_router.get("/health", summary="Liveness probe")
async def health() -> dict[str, Any]:
    """Cheap, dependency-free. Answers 'is the process up?'."""
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
    }


@health_router.get("/health/db", summary="Database connectivity")
async def health_db() -> JSONResponse:
    try:
        return JSONResponse(await check_database())
    except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
        logger.error("database health check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)})


@health_router.get("/health/redis", summary="Redis connectivity")
async def health_redis() -> JSONResponse:
    try:
        return JSONResponse(await check_redis())
    except Exception as exc:  # noqa: BLE001
        logger.error("redis health check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)})


@health_router.get("/health/ready", summary="Readiness probe")
async def health_ready() -> JSONResponse:
    """Answers 'can this instance serve traffic?' — checks every dependency."""
    results: dict[str, Any] = {}
    healthy = True
    for name, probe in (("database", check_database), ("redis", check_redis)):
        try:
            results[name] = await probe()
        except Exception as exc:  # noqa: BLE001
            healthy = False
            results[name] = {"status": "error", "detail": str(exc)}
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": results},
    )


def _register_routers(app: FastAPI) -> None:
    app.include_router(health_router)

    prefix = settings.API_V1_PREFIX
    app.include_router(identity_router.auth_router, prefix=prefix)
    app.include_router(identity_router.users_router, prefix=prefix)
    app.include_router(identity_router.roles_router, prefix=prefix)
    app.include_router(identity_router.audit_router, prefix=prefix)
    app.include_router(tenancy_router.router, prefix=prefix)
    app.include_router(patients_router.router, prefix=prefix)
    app.include_router(tenancy_router.departments_router, prefix=prefix)
    app.include_router(scheduling_router.doctors_router, prefix=prefix)
    app.include_router(scheduling_router.appointments_router, prefix=prefix)
    app.include_router(scheduling_router.queue_router, prefix=prefix)
    app.include_router(scheduling_router.search_router, prefix=prefix)
    app.include_router(clinical_router.encounters_router, prefix=prefix)
    app.include_router(clinical_router.orders_router, prefix=prefix)
    app.include_router(clinical_router.templates_router, prefix=prefix)
    app.include_router(diagnostics_router.catalogue_router, prefix=prefix)
    app.include_router(diagnostics_router.specimens_router, prefix=prefix)
    app.include_router(diagnostics_router.reports_router, prefix=prefix)
    app.include_router(diagnostics_router.worklist_router, prefix=prefix)
    app.include_router(billing_router.rate_cards_router, prefix=prefix)
    app.include_router(billing_router.services_router, prefix=prefix)
    app.include_router(billing_router.charges_router, prefix=prefix)
    app.include_router(billing_router.accounts_router, prefix=prefix)
    app.include_router(billing_router.invoices_router, prefix=prefix)
    app.include_router(billing_router.payments_router, prefix=prefix)
    app.include_router(billing_router.claims_router, prefix=prefix)
    # Templates and suppressions are mounted before the outbox: both sit under
    # /notifications/..., and a bare `/notifications/{notification_id}` would
    # otherwise swallow `/notifications/templates` as a UUID path parameter.
    app.include_router(notifications_router.templates_router, prefix=prefix)
    app.include_router(notifications_router.suppressions_router, prefix=prefix)
    app.include_router(notifications_router.notifications_router, prefix=prefix)
    app.include_router(ipd_router.wards_router, prefix=prefix)
    app.include_router(ipd_router.beds_router, prefix=prefix)
    app.include_router(ipd_router.admissions_router, prefix=prefix)
    app.include_router(ipd_router.chart_router, prefix=prefix)
    app.include_router(ipd_router.summaries_router, prefix=prefix)
    app.include_router(ipd_router.admission_requests_router, prefix=prefix)
    app.include_router(reporting_router.router, prefix=prefix)

    # Further module routers mount here as they are built (CLAUDE.md §13).


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        description="Patient-journey management for a multi-department hospital.",
        lifespan=lifespan,
        # API docs are an information-disclosure surface — off in production.
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)
    _register_routers(app)
    return app


app = create_app()
