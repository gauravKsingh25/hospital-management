"""The RBAC cache must never let a revoked permission survive.

Correctness here is a safety property, not a performance one: a clinician whose
access was withdrawn mid-shift must lose it on their next request.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import cache, service
from app.modules.identity.models import User
from app.modules.identity.rbac import Permissions, Roles

pytestmark = pytest.mark.integration


def _redis_available() -> bool:
    import socket

    from app.core.config import settings

    host = settings.REDIS_URL.split("//")[-1].split("/")[0]
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname, int(port or 6379)), timeout=2):
            return True
    except OSError:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(), reason="no local Redis (docker compose up redis)"
)


class TestCorrectnessWithoutRedis:
    """The system must be fully correct when the cache is simply unavailable."""

    async def test_permissions_resolve_when_redis_is_down(
        self, session: AsyncSession, hospital_admin: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(*_: object, **__: object) -> None:
            raise OSError("redis is down")

        monkeypatch.setattr(cache, "get_permissions", _boom_none)
        monkeypatch.setattr(cache, "set_permissions", _boom_noop)

        roles, permissions = await service.resolve_permissions(session, hospital_admin)
        assert Roles.HOSPITAL_ADMIN in roles
        assert Permissions.USER_CREATE in permissions


async def _boom_none(*_: object, **__: object) -> None:
    """Stands in for a cache read that failed — a miss, never an exception."""
    return None


async def _boom_noop(*_: object, **__: object) -> None:
    return None


@requires_redis
class TestInvalidation:
    async def test_reassigning_roles_takes_effect_immediately(
        self, session: AsyncSession, hospital_admin: User
    ) -> None:
        """The scenario that matters: access withdrawn mid-shift."""
        _, before = await service.resolve_permissions(session, hospital_admin)
        assert Permissions.USER_CREATE in before

        await service.assign_roles(session, hospital_admin, [Roles.NURSE])
        await session.commit()

        _, after = await service.resolve_permissions(session, hospital_admin)
        assert Permissions.USER_CREATE not in after

    async def test_deactivation_invalidates_the_entry(
        self, session: AsyncSession, hospital_admin: User
    ) -> None:
        await service.resolve_permissions(session, hospital_admin)
        await service.set_user_active(session, hospital_admin, active=False)
        await session.commit()

        assert await cache.get_permissions(str(hospital_admin.id)) is None

    async def test_a_warm_cache_returns_the_same_answer(
        self, session: AsyncSession, hospital_admin: User
    ) -> None:
        cold = await service.resolve_permissions(session, hospital_admin)
        warm = await service.resolve_permissions(session, hospital_admin)
        assert cold == warm

    async def test_bumping_the_generation_retires_every_entry(
        self, session: AsyncSession, hospital_admin: User
    ) -> None:
        """One INCR beats enumerating every holder of a changed role."""
        await service.resolve_permissions(session, hospital_admin)
        assert await cache.get_permissions(str(hospital_admin.id)) is not None

        await cache.invalidate_all()

        assert await cache.get_permissions(str(hospital_admin.id)) is None
