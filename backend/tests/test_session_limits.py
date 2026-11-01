"""SEC-04, the server half: a session ends when nobody uses it, and at shift end.

The refresh token lasted seven days and the web app refreshed it silently, so
a ward tablet left on the counter stayed signed in as the last nurse for as
long as the browser stayed open — against CLAUDE.md §12's "session timeouts
on all roles". Two limits now hold whatever the client does:

* idle: a refresh token not used for `REFRESH_IDLE_MINUTES` is dead;
* absolute: no chain of refreshes outlives `SESSION_MAX_HOURS` from sign-in.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.core.models import utc_now
from app.core.security import fingerprint
from app.modules.identity import service
from app.modules.identity.models import RefreshToken, User
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.integration


async def _age(session: AsyncSession, token: str, **fields: object) -> None:
    async with db.system_context(session):
        await session.execute(
            sa.update(RefreshToken)
            .where(RefreshToken.token_hash == fingerprint(token))
            .values(**fields)
        )
    await session.commit()


async def _sign_in(session: AsyncSession, user: User) -> str:
    _, _, refresh, _ = await service.login(session, email=user.email, password=TEST_PASSWORD)
    await session.commit()
    return refresh


async def test_a_session_nobody_has_used_for_a_while_ends(
    session: AsyncSession, receptionist: User
) -> None:
    refresh = await _sign_in(session, receptionist)
    await _age(
        session,
        refresh,
        created_at=utc_now() - timedelta(minutes=settings.REFRESH_IDLE_MINUTES + 1),
    )

    with pytest.raises(AuthenticationError) as raised:
        await service.rotate_refresh_token(session, refresh)
    assert raised.value.code == "session_idle"


async def test_a_session_in_use_carries_on(session: AsyncSession, receptionist: User) -> None:
    refresh = await _sign_in(session, receptionist)
    await _age(
        session,
        refresh,
        created_at=utc_now() - timedelta(minutes=settings.REFRESH_IDLE_MINUTES - 5),
    )
    _, _, rotated, _ = await service.rotate_refresh_token(session, refresh)
    assert rotated != refresh


async def test_no_session_outlives_a_shift(session: AsyncSession, receptionist: User) -> None:
    """Refreshing every few minutes all night still ends at the cap."""
    refresh = await _sign_in(session, receptionist)
    await _age(
        session,
        refresh,
        session_started_at=utc_now() - timedelta(hours=settings.SESSION_MAX_HOURS, minutes=1),
    )

    with pytest.raises(AuthenticationError) as raised:
        await service.rotate_refresh_token(session, refresh)
    assert raised.value.code == "session_expired"


async def test_refreshing_keeps_the_original_sign_in_time(
    session: AsyncSession, receptionist: User
) -> None:
    """Otherwise every refresh would restart the shift clock and the cap
    would never be reached."""
    refresh = await _sign_in(session, receptionist)
    started = utc_now() - timedelta(hours=3)
    await _age(session, refresh, session_started_at=started)

    _, _, rotated, _ = await service.rotate_refresh_token(session, refresh)
    await session.commit()

    async with db.system_context(session):
        row = (
            (
                await session.execute(
                    sa.select(RefreshToken).where(RefreshToken.token_hash == fingerprint(rotated))
                )
            )
            .scalars()
            .one()
        )
    assert abs((row.session_started_at - started).total_seconds()) < 1
