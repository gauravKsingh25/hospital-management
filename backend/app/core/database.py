"""Async database engine, session management and the RLS tenant seam.

Everything in the app talks to Postgres through `get_session` (FastAPI routes)
or `session_scope` (workers, scripts). Nothing constructs its own engine.
"""

from __future__ import annotations

import logging
import ssl
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import event, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import Settings, is_pooled, requires_tls, settings

logger = logging.getLogger(__name__)

# Postgres GUC that RLS policies read to scope every row to one hospital.
# Phase 2 (`tenancy` + `identity`) attaches the policies; the plumbing that
# sets the value lives here so no module ever invents its own mechanism.
TENANT_GUC: Final[str] = "app.hospital_id"

# Escape hatch for the few operations that legitimately cross tenants. Only
# `identity`, `tenancy` and seed migrations may set it — see `set_bypass_rls`.
BYPASS_RLS_GUC: Final[str] = "app.bypass_rls"

# Where the bindings are remembered across transaction boundaries.
_TENANT_INFO_KEY: Final[str] = "hms_hospital_id"
_BYPASS_INFO_KEY: Final[str] = "hms_bypass_rls"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


@event.listens_for(Session, "after_begin")
def _reapply_session_context(session: Session, transaction: object, connection: Connection) -> None:
    """Re-establish the tenant binding whenever a new transaction opens.

    The GUCs are transaction-local by design (safe behind PgBouncer), so a
    `COMMIT` clears them. Without this, any code that committed and kept working
    would carry on with no tenant bound — matching no rows and looking, very
    convincingly, like data loss. Re-applying here means the binding is a
    property of the session, not of one transaction.
    """
    hospital_id = session.info.get(_TENANT_INFO_KEY)
    if hospital_id is not None:
        # Interpolated rather than bound: this runs inside SQLAlchemy's event
        # machinery where a bound-parameter round trip is not available. The
        # value is a `uuid.UUID`, so its string form is hex and hyphens only.
        connection.exec_driver_sql(
            f"SELECT set_config('{TENANT_GUC}', '{uuid.UUID(str(hospital_id))}', true)"
        )
    if session.info.get(_BYPASS_INFO_KEY):
        connection.exec_driver_sql(f"SELECT set_config('{BYPASS_RLS_GUC}', 'on', true)")


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------
def _ssl_context(url: URL) -> ssl.SSLContext | bool:
    """TLS for Neon, plaintext for a local container.

    Note this is stricter than the `sslmode=require` Neon puts in its URL:
    `require` encrypts but verifies nothing. A default context verifies the
    chain and the hostname, which is what CLAUDE.md §12 ("encryption in
    transit") actually implies.
    """
    if not requires_tls(url):
        return False
    return ssl.create_default_context()


def _connect_args(config: Settings, url: URL) -> dict[str, Any]:
    args: dict[str, Any] = {
        "ssl": _ssl_context(url),
        "timeout": config.DB_CONNECT_TIMEOUT_SECONDS,
        "server_settings": {
            # Shows up in pg_stat_activity — makes it obvious which process is
            # holding a connection when debugging against a shared Neon branch.
            "application_name": f"{config.APP_NAME} [{config.ENVIRONMENT}]",
        },
    }
    if config.DB_COMMAND_TIMEOUT_SECONDS > 0:
        args["command_timeout"] = float(config.DB_COMMAND_TIMEOUT_SECONDS)

    if is_pooled(url):
        # Neon's pooled endpoint is PgBouncer in TRANSACTION mode: a server
        # connection is handed to whoever needs it next, mid-session. Named
        # server-side prepared statements do not survive that, so we disable
        # both caches and give any statement that is still prepared a unique
        # name to avoid cross-connection collisions.
        args["statement_cache_size"] = 0
        args["prepared_statement_cache_size"] = 0
        args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid.uuid4()}__"

    return args


def create_engine(
    config: Settings | None = None,
    *,
    direct: bool = False,
    url: URL | None = None,
) -> AsyncEngine:
    """Build an async engine.

    `direct=True` selects the unpooled endpoint and disables client-side
    pooling — used by Alembic and one-shot scripts, never by the API.
    `url` overrides both, for the test harness.
    """
    config = config or settings
    unpooled = direct or url is not None
    if url is None:
        url = config.async_direct_url if direct else config.async_database_url

    if unpooled:
        return create_async_engine(
            url,
            echo=config.DB_ECHO,
            poolclass=NullPool,
            connect_args=_connect_args(config, url),
        )

    return create_async_engine(
        url,
        echo=config.DB_ECHO,
        pool_size=config.DB_POOL_SIZE,
        max_overflow=config.DB_MAX_OVERFLOW,
        # Neon free-tier compute auto-suspends when idle; a pooled connection
        # can therefore be dead on arrival. pre_ping costs one cheap round-trip
        # and turns a 500 into a transparent reconnect. Recycling below the
        # idle window keeps us from handing out stale sockets in the first place.
        pool_pre_ping=config.DB_POOL_PRE_PING,
        pool_recycle=config.DB_POOL_RECYCLE_SECONDS,
        connect_args=_connect_args(config, url),
    )


def get_engine() -> AsyncEngine:
    """Process-wide engine singleton (created on first use)."""
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_engine()
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            # Objects stay usable after commit — otherwise every response
            # serialization triggers a surprise lazy refresh.
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info(
            "database engine created host=%s pooled=%s",
            _engine.url.host,
            settings.is_pooled_connection,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    """Close all pooled connections. Called on application shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        logger.info("database engine disposed")
    _engine = None
    _sessionmaker = None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    The route body owns the transaction boundary: services call `commit()`.
    Anything that escapes uncommitted is rolled back here.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session for non-request contexts (workers, CLI, seed scripts)."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Tenant / RLS context
# ---------------------------------------------------------------------------
async def set_tenant_context(session: AsyncSession, hospital_id: uuid.UUID | str) -> None:
    """Bind the session to one hospital.

    `set_config(..., is_local => true)` scopes the setting to the surrounding
    transaction, which is what makes it safe behind PgBouncer in transaction
    mode: the value cannot leak into whatever the pooler hands that server
    connection to next.

    The flip side is that `COMMIT` discards it. A service that commits midway
    would silently continue unbound, and — because an unbound session matches no
    tenant rows — start behaving as though the hospital's data had vanished.
    That is a trap, so the binding is also recorded on `session.info` and
    re-applied automatically at the start of every subsequent transaction (see
    `_reapply_session_context`). Callers set it once and stop thinking about it.
    """
    session.info[_TENANT_INFO_KEY] = uuid.UUID(str(hospital_id))
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": TENANT_GUC, "value": str(hospital_id)},
    )


async def clear_tenant_context(session: AsyncSession) -> None:
    """Drop the tenant binding (platform-admin operations only, CLAUDE.md §3)."""
    session.info.pop(_TENANT_INFO_KEY, None)
    await session.execute(
        text("SELECT set_config(:key, '', true)"),
        {"key": TENANT_GUC},
    )


async def set_bypass_rls(session: AsyncSession, *, enabled: bool = True) -> None:
    """Lift row-level isolation for the current transaction.

    Needed for the handful of operations that legitimately have no tenant yet
    or must span tenants: authenticating a user (we do not know their hospital
    until we have read their row), platform-admin hospital management, and
    seed migrations.

    Trade-off worth being explicit about: this is a real bypass, so every call
    site is a potential leak. It is deliberately confined to `identity` and
    `tenancy` — the only modules CLAUDE.md §3 permits to cross tenants — and is
    transaction-local, so it cannot outlive the operation that set it. The
    stricter alternative (a second database role that RLS cannot bypass) costs
    a second connection pool and is the right move only once we run more than
    one tenant in earnest.
    """
    if enabled:
        session.info[_BYPASS_INFO_KEY] = True
    else:
        session.info.pop(_BYPASS_INFO_KEY, None)
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": BYPASS_RLS_GUC, "value": "on" if enabled else ""},
    )


@asynccontextmanager
async def system_context(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Run a block with RLS lifted, restoring the previous setting on exit.

    The previous state is read from `session.info`, not from the database. Those
    two can disagree: the GUC is transaction-local and a commit inside the block
    clears it, while `session.info` is what `_reapply_session_context` uses to
    rebuild the binding on the next transaction. Restoring from the GUC would
    therefore leave the flag stuck on after any commit — and a session that
    silently keeps bypassing row-level security is the one failure this whole
    mechanism exists to prevent.

    Saving and restoring rather than simply switching off matters because a
    platform-admin request runs with bypass enabled for its entire session; a
    nested block must not quietly downgrade it.
    """
    previous = bool(session.info.get(_BYPASS_INFO_KEY, False))
    await set_bypass_rls(session, enabled=True)
    try:
        yield session
    finally:
        await set_bypass_rls(session, enabled=previous)


@asynccontextmanager
async def tenant_session(hospital_id: uuid.UUID | str) -> AsyncIterator[AsyncSession]:
    """Session pre-bound to a tenant, for workers processing tenant data."""
    async with session_scope() as session:
        await set_tenant_context(session, hospital_id)
        yield session


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
async def check_database() -> dict[str, Any]:
    """Round-trip the database and report latency and server version.

    A slow first response here is expected, not an error: Neon cold-starts
    suspended compute on the next connection (CLAUDE.md §5).
    """
    started = time.perf_counter()
    async with get_engine().connect() as conn:
        version = (await conn.execute(text("SHOW server_version"))).scalar_one()
        database = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        role, bypasses_rls = (
            await conn.execute(
                text(
                    "SELECT rolname, (rolsuper OR rolbypassrls) FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "status": "ok",
        "database": database,
        "server_version": version,
        "latency_ms": elapsed_ms,
        "pooled": settings.is_pooled_connection,
        "host": get_engine().url.host,
        "role": role,
        "rls_enforced": not bypasses_rls,
    }


async def assert_rls_enforced() -> None:
    """Fail fast if the application role can bypass row-level security.

    Managed Postgres owners (Neon's `neondb_owner`, RDS's master user) carry
    BYPASSRLS, which overrides even FORCE ROW LEVEL SECURITY. Connecting as one
    leaves every tenant policy in place and enforcing nothing — a silent,
    total isolation failure that no test of the application layer would catch.
    `scripts/bootstrap_db_role.py` creates the restricted role this expects.
    """
    status = await check_database()
    if status["rls_enforced"]:
        return

    message = (
        f"database role '{status['role']}' can bypass row-level security; "
        "tenant isolation is NOT enforced. Run scripts/bootstrap_db_role.py "
        "and point DATABASE_URL at the application role."
    )
    if settings.is_production:
        raise RuntimeError(message)
    logger.warning("%s", message)
