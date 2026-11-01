"""Five wrong passwords lock the account — over HTTP, not only in the service.

The service test proves the counter climbs within one session. This proves it
survives the request: a failed sign-in raises, and the request's session rolls
back whatever it has not committed. A counter that rolled back with it would
never reach five, and the lockout would exist only in the unit test.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.modules.identity.models import User
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.integration


async def test_five_wrong_passwords_lock_the_account(api: AsyncClient, receptionist: User) -> None:
    for _ in range(5):
        wrong = await api.post(
            "/api/v1/auth/login", json={"email": receptionist.email, "password": "not-it-at-all"}
        )
        assert wrong.status_code == 401

    right = await api.post(
        "/api/v1/auth/login", json={"email": receptionist.email, "password": TEST_PASSWORD}
    )
    assert right.status_code == 401, right.text
    assert right.json()["error"]["code"] == "account_locked"


async def test_a_failed_sign_in_leaves_an_audit_row(
    api: AsyncClient, receptionist: User, hospital_admin: User
) -> None:
    from tests.conftest import auth, login

    await api.post(
        "/api/v1/auth/login", json={"email": receptionist.email, "password": "not-it-at-all"}
    )
    token = await login(api, hospital_admin)
    logs = await api.get(
        "/api/v1/audit-logs",
        params={"action": "auth.login", "limit": 100},
        headers=auth(token),
    )
    failures = [
        row
        for row in logs.json()["items"]
        if row["succeeded"] is False and row["resource_id"] == str(receptionist.id)
    ]
    assert failures, "a failed sign-in must be on the record"


async def test_replaying_a_rotated_refresh_token_ends_every_session(
    api: AsyncClient, receptionist: User
) -> None:
    """Reuse is the signature of a stolen token; the family is revoked — and
    that revocation has to survive the error the replay is answered with."""
    first = (
        await api.post(
            "/api/v1/auth/login", json={"email": receptionist.email, "password": TEST_PASSWORD}
        )
    ).json()["refresh_token"]
    second = (await api.post("/api/v1/auth/refresh", json={"refresh_token": first})).json()[
        "refresh_token"
    ]

    replay = await api.post("/api/v1/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "token_reused"

    # The legitimate client's current token died with the family.
    current = await api.post("/api/v1/auth/refresh", json={"refresh_token": second})
    assert current.status_code == 401, current.text
