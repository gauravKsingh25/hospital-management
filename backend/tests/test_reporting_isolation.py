"""The reporting views must not leak across tenants.

This is the most important test in the module, and possibly the most important
test of its kind in the codebase.

A Postgres view runs with the **owner's** privileges by default. The owner here
is the migration role, which on Neon (and RDS, and Cloud SQL) carries
`BYPASSRLS` — the same attribute the README warns about at length for the
application role. So a `reporting_*` view created without
`WITH (security_invoker = true)` would read its base tables with row-level
security switched off, and `SELECT * FROM reporting_charges` issued by any
tenant would return every hospital's money.

Nothing about that failure is visible in the application code. There is no
missing `WHERE`, no wrong join; `service.py` would look exactly as it does now.
It is a property of a view attribute nobody looks at twice, which is why it
gets a test that queries every view as a bound tenant and counts rows.

The second test asserts the attribute directly against `pg_class`, so a view
added later without the flag fails here even if it happens to contain no data
at the time.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.clinical import service as clinical_service
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender
from app.modules.patients.schemas import PatientRegister
from app.modules.reporting.models import REPORTING_VIEWS
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.models import Hospital
from app.modules.tenancy.schemas import HospitalCreate

pytestmark = pytest.mark.integration


async def _make_hospital(session: AsyncSession, code: str) -> Hospital:
    async with db.system_context(session):
        hospital = await tenancy_service.create_hospital(
            session, HospitalCreate(code=code, name=f"{code} Hospital")
        )
        await session.flush()
    await session.commit()
    return hospital


async def _visit(session: AsyncSession, hospital: Hospital, name: str, phone: str) -> None:
    """A patient with an open encounter — enough to populate the views."""
    from tests.conftest import _make_user

    await db.set_tenant_context(session, hospital.id)
    doctor: User = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
    await db.set_tenant_context(session, hospital.id)

    patient, _ = await patients_service.register_patient(
        session,
        PatientRegister(full_name=name, phone=phone, gender=Gender.MALE, age_years=40),
        hospital_id=hospital.id,
    )
    await clinical_service.open_encounter(
        session, hospital_id=hospital.id, patient_id=patient.id, actor=doctor
    )
    await session.commit()


@pytest.fixture
async def two_hospitals(session: AsyncSession) -> tuple[Hospital, Hospital]:
    first = await _make_hospital(session, "RPTONE")
    second = await _make_hospital(session, "RPTTWO")
    await _visit(session, first, "Anil Kumar", "9811110001")
    await _visit(session, second, "Bhaskar Rao", "9811110002")
    return first, second


async def test_every_reporting_view_is_security_invoker(session: AsyncSession) -> None:
    """Asserted against the catalogue, so a view added later cannot forget it.

    Checked structurally rather than behaviourally on purpose: a new view with
    no rows in it yet would pass a data-leak test by accident, and then start
    leaking the first time somebody used the feature it reports on.
    """
    async with db.system_context(session):
        rows = (
            await session.execute(
                sa.text(
                    """
                    SELECT c.relname,
                           coalesce(
                               (SELECT option_value
                                  FROM pg_options_to_table(c.reloptions)
                                 WHERE option_name = 'security_invoker'),
                               'false'
                           ) AS security_invoker
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE c.relkind = 'v'
                       AND n.nspname = 'public'
                       AND c.relname = ANY(:names)
                    """
                ),
                {"names": list(REPORTING_VIEWS)},
            )
        ).all()

    found = dict(rows)
    assert set(found) == set(REPORTING_VIEWS), f"missing views: {set(REPORTING_VIEWS) - set(found)}"

    not_invoker = [name for name, flag in found.items() if flag != "true"]
    assert not not_invoker, (
        f"these views run as their owner and therefore bypass RLS: {not_invoker}"
    )


@pytest.mark.parametrize("view", REPORTING_VIEWS)
async def test_a_bound_tenant_sees_only_its_own_rows(
    session: AsyncSession, two_hospitals: tuple[Hospital, Hospital], view: str
) -> None:
    """The behavioural half: query each view as one tenant, count the other's rows.

    Parametrised per view rather than looped, so a failure names the view that
    leaked instead of the first one the loop reached.
    """
    first, second = two_hospitals

    await db.set_tenant_context(session, first.id)
    visible = (
        await session.execute(
            sa.text(f"SELECT hospital_id, count(*) AS n FROM {view} GROUP BY hospital_id")  # noqa: S608
        )
    ).all()

    tenants = {row.hospital_id for row in visible}
    assert second.id not in tenants, f"{view} leaked rows belonging to another hospital"
    assert tenants <= {first.id}


async def test_an_unbound_session_sees_nothing_through_the_views(
    session: AsyncSession, two_hospitals: tuple[Hospital, Hospital]
) -> None:
    """No tenant bound means no rows — NULL never equals anything.

    The same property the base-table policies rely on (see the RLS migration's
    note on `NULLIF(current_setting(...), '')`), verified end to end through the
    reporting layer rather than assumed to carry over.
    """
    await db.clear_tenant_context(session)

    for view in REPORTING_VIEWS:
        count = (
            await session.execute(sa.text(f"SELECT count(*) FROM {view}"))  # noqa: S608
        ).scalar_one()
        assert count == 0, f"{view} returned {count} rows to a session with no tenant bound"


async def test_a_nonexistent_tenant_sees_nothing(
    session: AsyncSession, two_hospitals: tuple[Hospital, Hospital]
) -> None:
    """Binding a made-up hospital id must not fall back to showing everything."""
    await db.set_tenant_context(session, uuid.uuid4())

    total = (
        await session.execute(sa.text("SELECT count(*) FROM reporting_encounters"))
    ).scalar_one()
    assert total == 0
