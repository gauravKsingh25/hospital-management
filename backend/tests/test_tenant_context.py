"""The tenant-binding mechanism itself.

Every other module's isolation rests on these two behaviours, and both have
already broken once during development — which is precisely why they are pinned
here rather than left as an implementation detail.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.identity.models import User
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration


async def _current(session: AsyncSession, key: str) -> str:
    value = (
        await session.execute(sa.text("SELECT current_setting(:key, true)"), {"key": key})
    ).scalar_one_or_none()
    return value or ""


class TestBindingSurvivesCommit:
    async def test_the_tenant_stays_bound_after_a_commit(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        """The GUC is transaction-local, so a commit clears it in the database.

        Without re-application the session would carry on unbound — matching no
        rows and looking exactly like the hospital's data had disappeared.
        """
        await db.set_tenant_context(session, hospital.id)
        assert await _current(session, db.TENANT_GUC) == str(hospital.id)

        await session.commit()

        assert await _current(session, db.TENANT_GUC) == str(hospital.id)

    async def test_clearing_the_tenant_survives_a_commit_too(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        await db.set_tenant_context(session, hospital.id)
        await db.clear_tenant_context(session)
        await session.commit()

        assert await _current(session, db.TENANT_GUC) == ""


class TestBypassIsAlwaysGivenBack:
    async def test_leaving_a_system_context_restores_isolation(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        await db.set_tenant_context(session, hospital.id)

        async with db.system_context(session):
            assert await _current(session, db.BYPASS_RLS_GUC) == "on"

        assert await _current(session, db.BYPASS_RLS_GUC) == ""

    async def test_a_commit_inside_a_system_context_does_not_strand_the_bypass(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        """The regression this file exists for.

        Restoring the flag from the database read it back as empty after a
        commit, so the block "restored" bypass to on and left every later query
        in that session ignoring row-level security.
        """
        await db.set_tenant_context(session, hospital.id)

        async with db.system_context(session):
            await session.commit()

        assert await _current(session, db.BYPASS_RLS_GUC) == ""

    async def test_nesting_does_not_downgrade_a_platform_admin_session(
        self, session: AsyncSession
    ) -> None:
        """A platform-admin request runs with bypass on for its whole session."""
        await db.set_bypass_rls(session, enabled=True)

        async with db.system_context(session):
            pass

        assert await _current(session, db.BYPASS_RLS_GUC) == "on"

    async def test_isolation_really_applies_again_afterwards(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        """Not just the flag — the rows."""
        async with db.system_context(session):
            other = Hospital(code=f"T{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            session.add(
                User(
                    hospital_id=other.id,
                    email="elsewhere@example.com",
                    full_name="Elsewhere",
                    password_hash="x",
                    is_active=True,
                )
            )
            await session.flush()
        await session.commit()

        await db.set_tenant_context(session, hospital.id)
        visible = (await session.execute(sa.select(User.email))).scalars().all()

        assert "elsewhere@example.com" not in visible
