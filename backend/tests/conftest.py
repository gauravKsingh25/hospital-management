"""Test harness.

Integration tests run against a **local** Postgres (`TEST_DATABASE_URL`), never
against the configured Neon database — see `.env.example` for why.

The harness deliberately mirrors production's two-role setup: migrations run as
the database owner, and the application connects as a restricted role with no
`BYPASSRLS`. Without that, every row-level-security test would pass vacuously,
because a superuser ignores policies entirely.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.config import settings
from app.core.events import event_bus
from app.core.redis import close_redis
from app.core.security import hash_password
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital

# Imported for its side effect: `registry` is what populates SQLModel.metadata
# with every module's tables. Without it a test that touches only one module
# sees half a schema, and SQLAlchemy fails resolving a foreign key to a table it
# has never heard of.
from app.registry import register_event_handlers, target_metadata  # noqa: F401

BACKEND_DIR = Path(__file__).resolve().parents[1]

TEST_PASSWORD = "correct-horse-battery-staple"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: touches a real database or Redis instance")


# ---------------------------------------------------------------------------
# Database harness
# ---------------------------------------------------------------------------
def _owner_url() -> URL:
    return make_url(settings.TEST_DATABASE_URL)


def _app_url() -> URL:
    """Same test database, reached as the restricted application role."""
    return _owner_url().set(
        username=settings.APP_DB_ROLE,
        password=settings.APP_DB_PASSWORD or "hms_app_test",
    )


def _server_available() -> bool:
    import socket

    url = _owner_url()
    try:
        with socket.create_connection((url.host or "localhost", url.port or 5432), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def database() -> Iterator[URL]:
    """Create, migrate and hand back the test database.

    Skips the whole integration suite when no local Postgres is running, so
    `pytest` still works offline (`docker compose up postgres` provides it).
    """
    if not _server_available():
        pytest.skip("no local Postgres for integration tests (docker compose up postgres)")

    owner = _owner_url()
    admin = owner.set(database="postgres", drivername="postgresql+asyncpg")
    target = owner.database
    assert target and target != "neondb", "refusing to run tests against the Neon database"

    import asyncio

    async def prepare() -> None:
        engine = sa.ext.asyncio.create_async_engine(admin, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                exists = (
                    await conn.execute(
                        sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": target},
                    )
                ).scalar_one_or_none()
                if not exists:
                    await conn.execute(sa.text(f'CREATE DATABASE "{target}"'))
        finally:
            await engine.dispose()

    asyncio.run(prepare())

    # Migrations run as the owner, in a subprocess so Alembic gets its own
    # event loop and its own settings instance.
    env = {
        **os.environ,
        "DIRECT_URL": owner.render_as_string(hide_password=False),
        "DATABASE_URL": owner.render_as_string(hide_password=False),
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
    )

    async def make_app_role() -> None:
        engine = sa.ext.asyncio.create_async_engine(
            owner.set(drivername="postgresql+asyncpg"), isolation_level="AUTOCOMMIT"
        )
        role = settings.APP_DB_ROLE
        password = settings.APP_DB_PASSWORD or "hms_app_test"
        try:
            async with engine.connect() as conn:
                exists = (
                    await conn.execute(
                        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
                    )
                ).scalar_one_or_none()
                verb = "ALTER" if exists else "CREATE"
                await conn.execute(sa.text(f"{verb} ROLE {role} WITH LOGIN PASSWORD '{password}'"))
                # The container's POSTGRES_USER is a superuser and would bypass
                # RLS; the tests must not.
                await conn.execute(sa.text(f"ALTER ROLE {role} NOSUPERUSER NOBYPASSRLS"))
                await conn.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {role}"))
                await conn.execute(
                    sa.text(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                        f"IN SCHEMA public TO {role}"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(make_app_role())

    # Point the application at the test database for the rest of the session.
    settings.DATABASE_URL = _app_url().render_as_string(hide_password=False)
    settings.DIRECT_URL = owner.render_as_string(hide_password=False)

    yield owner


@pytest.fixture(autouse=True)
def restore_event_subscriptions() -> Iterator[None]:
    """Put the event bus back exactly as the application wires it, after each test.

    Several tests subscribe a list appender and then call `event_bus.clear()` to
    tidy up. That was harmless while nothing else subscribed. It stopped being
    harmless in Phase 6: the bus now carries `billing`'s transactional charge
    capture and `diagnostics`' order-withdrawal, registered once at import of
    `app.registry` — so one `clear()` silently disabled charge capture for every
    test that ran afterwards, and the failures landed in a different file from
    the cause.

    Resetting and re-registering (rather than only re-registering) guarantees
    exactly one subscription per event whatever the test did, so no future test
    can quietly turn billing off for its neighbours.
    """
    yield
    event_bus.clear()
    register_event_handlers()


@pytest.fixture(autouse=True)
async def reset_clients() -> AsyncIterator[None]:
    """Drop cached connections between tests.

    pytest-asyncio gives each test its own event loop, but the engine and Redis
    client are process-wide singletons — reusing one across loops surfaces as
    "Event loop is closed" somewhere unrelated. Production runs a single loop
    for the process lifetime, so this is a harness concern only.
    """
    yield
    await close_redis()
    await db.dispose_engine()


@pytest.fixture(autouse=True)
async def clean_database(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Wipe tenant data between tests, leaving the seeded RBAC catalogue.

    Runs as the owner. `audit_logs` needs its append-only trigger disabled to
    be cleared at all — which is the guarantee working, not a problem.
    """
    if "database" not in request.fixturenames:
        yield
        return

    yield

    owner = _owner_url().set(drivername="postgresql+asyncpg")
    engine = sa.ext.asyncio.create_async_engine(owner, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("ALTER TABLE audit_logs DISABLE TRIGGER USER"))
            await conn.execute(
                sa.text(
                    "TRUNCATE audit_logs, refresh_tokens, user_roles, users, "
                    "patient_alerts, patient_consents, patient_identifiers, "
                    "patients, uhid_sequences, queue_entries, appointments, "
                    "doctor_availabilities, availability_exceptions, doctors, "
                    "token_sequences, appointment_sequences, departments, "
                    "result_values, diagnostic_reports, specimens, reference_ranges, "
                    "test_analytes, test_catalogue_items, accession_sequences, "
                    "notification_attempts, notifications, notification_suppressions, "
                    "notification_templates, "
                    "medication_administrations, medication_orders, discharge_summaries, "
                    "bed_assignments, admissions, beds, wards, admission_sequences, "
                    "insurance_claims, payments, charges, invoices, service_prices, "
                    "service_items, rate_cards, invoice_sequences, "
                    "encounter_events, vitals, clinical_notes, diagnoses, orders, "
                    "note_templates, encounters, encounter_sequences "
                    "RESTART IDENTITY CASCADE"
                )
            )
            await conn.execute(sa.text("ALTER TABLE audit_logs ENABLE TRIGGER USER"))
            await conn.execute(sa.text("DELETE FROM roles WHERE hospital_id IS NOT NULL"))
            await conn.execute(sa.text("DELETE FROM hospitals"))
    finally:
        await engine.dispose()
    await db.dispose_engine()


@pytest.fixture
async def session(database: URL) -> AsyncIterator[AsyncSession]:
    """A session on the test database, as the restricted application role."""
    await db.dispose_engine()
    async with db.get_sessionmaker()() as active:
        yield active


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def hospital(session: AsyncSession) -> Hospital:
    async with db.system_context(session):
        record = Hospital(code=f"H{uuid.uuid4().hex[:6].upper()}", name="Test Hospital")
        session.add(record)
        await session.flush()
        await session.refresh(record)
    await session.commit()
    return record


async def _make_user(
    session: AsyncSession,
    *,
    role_code: str,
    hospital_id: uuid.UUID | None,
    email: str | None = None,
    full_name: str = "Test Staff",
) -> User:
    """A staff account with one role.

    `full_name` is overridable because a doctor's profile takes its
    `display_name` from it, and a test that searches for a doctor by name needs
    that name to be something other than "Test Staff" for the assertion to mean
    anything.
    """
    async with db.system_context(session):
        user = User(
            hospital_id=hospital_id,
            email=email or f"{uuid.uuid4().hex[:10]}@example.com",
            full_name=full_name,
            password_hash=hash_password(TEST_PASSWORD),
            must_change_password=False,
            is_active=True,
        )
        session.add(user)
        await session.flush()

        role = (
            (
                await session.execute(
                    sa.select(Role).where(Role.code == role_code, Role.hospital_id.is_(None))
                )
            )
            .scalars()
            .one()
        )
        session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.flush()
        await session.refresh(user)
    await session.commit()
    return user


@pytest.fixture
async def platform_admin(session: AsyncSession) -> User:
    return await _make_user(session, role_code=Roles.PLATFORM_ADMIN, hospital_id=None)


@pytest.fixture
async def hospital_admin(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=hospital.id)


@pytest.fixture
async def receptionist(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=hospital.id)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """HTTP client bound directly to the ASGI app (no network, no lifespan)."""
    from app.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def api(database: URL) -> AsyncIterator[AsyncClient]:
    """HTTP client wired to the migrated test database."""
    from app.main import create_app

    await db.dispose_engine()
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def login(api: AsyncClient, user: User) -> str:
    """Log a fixture user in and return their access token."""
    response = await api.post(
        "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
