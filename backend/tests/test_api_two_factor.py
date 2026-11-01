"""SEC-05: two-step sign-in with an authenticator app.

Admins and doctors signed in with a password alone. Now a role can be made to
need a second step — the six-digit code from an authenticator app — set by
`TWO_FACTOR_REQUIRED_ROLES` (admins and doctors by default in staging and
production, nobody on a developer's machine). Anybody may also turn it on for
themselves.

What these pin:

* a required role cannot sign in without enrolling, and enrolling proves the
  app works before it is switched on;
* the code is checked like a password: wrong codes count towards the
  lockout, and a code is never accepted twice;
* the challenge between the two steps is not a session;
* "remember this device" skips the code on that device only, for 12 hours,
  and stops working the moment two-step is turned off or reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.core import totp
from app.core.config import settings
from app.modules.identity.models import User
from tests.conftest import TEST_PASSWORD, auth, login

pytestmark = pytest.mark.integration

AUTH = "/api/v1/auth"


@pytest.fixture
def admins_need_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TWO_FACTOR_REQUIRED_ROLES", ["HOSPITAL_ADMIN"])


async def _password(api: AsyncClient, user: User, **extra: object) -> dict:  # type: ignore[type-arg]
    response = await api.post(
        f"{AUTH}/login", json={"email": user.email, "password": TEST_PASSWORD, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def _enrol(api: AsyncClient, user: User) -> str:
    """Sign in, enrol, and return the secret the authenticator app holds."""
    challenge = await _password(api, user)
    started = await api.post(
        f"{AUTH}/2fa/enrol/start", json={"challenge_token": challenge["challenge_token"]}
    )
    secret: str = started.json()["secret"]
    confirmed = await api.post(
        f"{AUTH}/2fa/enrol/confirm",
        json={"challenge_token": challenge["challenge_token"], "code": totp.code_at(secret)},
    )
    assert confirmed.status_code == 200, confirmed.text
    return secret


def _next_code(secret: str) -> str:
    """A code for the next 30-second window: the current one is spent."""
    return totp.code_at(secret, datetime.now(UTC) + timedelta(seconds=30))


class TestWhoNeedsIt:
    async def test_without_it_a_password_is_enough(
        self, api: AsyncClient, hospital_admin: User
    ) -> None:
        body = await _password(api, hospital_admin)
        assert body["access_token"]

    async def test_a_required_role_must_enrol_before_it_gets_in(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        body = await _password(api, hospital_admin)
        assert "access_token" not in body
        assert body["two_factor_required"] is True
        assert body["enrol"] is True

        started = await api.post(
            f"{AUTH}/2fa/enrol/start", json={"challenge_token": body["challenge_token"]}
        )
        assert started.status_code == 200, started.text
        assert started.json()["otpauth_uri"].startswith("otpauth://totp/")

        # A wrong code does not switch it on: the app must be proven first.
        wrong = await api.post(
            f"{AUTH}/2fa/enrol/confirm",
            json={"challenge_token": body["challenge_token"], "code": "000000"},
        )
        assert wrong.status_code == 401

        confirmed = await api.post(
            f"{AUTH}/2fa/enrol/confirm",
            json={
                "challenge_token": body["challenge_token"],
                "code": totp.code_at(started.json()["secret"]),
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["access_token"]


class TestSigningIn:
    async def test_once_enrolled_the_code_is_asked_for(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        secret = await _enrol(api, hospital_admin)
        body = await _password(api, hospital_admin)
        assert body["two_factor_required"] is True
        assert body["enrol"] is False

        verified = await api.post(
            f"{AUTH}/2fa/verify",
            json={"challenge_token": body["challenge_token"], "code": _next_code(secret)},
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["access_token"]

    async def test_a_code_is_never_accepted_twice(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        secret = await _enrol(api, hospital_admin)
        code = _next_code(secret)
        first = await _password(api, hospital_admin)
        ok = await api.post(
            f"{AUTH}/2fa/verify", json={"challenge_token": first["challenge_token"], "code": code}
        )
        assert ok.status_code == 200, ok.text

        second = await _password(api, hospital_admin)
        replay = await api.post(
            f"{AUTH}/2fa/verify", json={"challenge_token": second["challenge_token"], "code": code}
        )
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "invalid_code"

    async def test_wrong_codes_lock_the_account_like_wrong_passwords(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        await _enrol(api, hospital_admin)
        body = await _password(api, hospital_admin)
        for _ in range(5):
            await api.post(
                f"{AUTH}/2fa/verify",
                json={"challenge_token": body["challenge_token"], "code": "000000"},
            )

        locked = await api.post(
            f"{AUTH}/login", json={"email": hospital_admin.email, "password": TEST_PASSWORD}
        )
        assert locked.status_code == 401
        assert locked.json()["error"]["code"] == "account_locked"

    async def test_the_challenge_is_not_a_session(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        body = await _password(api, hospital_admin)
        me = await api.get(f"{AUTH}/me", headers=auth(body["challenge_token"]))
        assert me.status_code == 401


class TestRememberedDevice:
    async def test_a_remembered_device_skips_the_code(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        secret = await _enrol(api, hospital_admin)
        body = await _password(api, hospital_admin)
        verified = await api.post(
            f"{AUTH}/2fa/verify",
            json={
                "challenge_token": body["challenge_token"],
                "code": _next_code(secret),
                "remember_device": True,
            },
        )
        device = verified.json()["device_token"]
        assert device

        again = await _password(api, hospital_admin, device_token=device)
        assert again["access_token"]

    async def test_a_device_token_is_worthless_for_anyone_else(
        self, api: AsyncClient, hospital_admin: User, receptionist: User, admins_need_it: None
    ) -> None:
        secret = await _enrol(api, hospital_admin)
        body = await _password(api, hospital_admin)
        device = (
            await api.post(
                f"{AUTH}/2fa/verify",
                json={
                    "challenge_token": body["challenge_token"],
                    "code": _next_code(secret),
                    "remember_device": True,
                },
            )
        ).json()["device_token"]

        # The receptionist turns two-step on for herself; the admin's device
        # token does not let anyone past *her* code.
        token = await login(api, receptionist)
        setup = await api.post(f"{AUTH}/2fa/setup", headers=auth(token))
        their_secret = setup.json()["secret"]
        enabled = await api.post(
            f"{AUTH}/2fa/enable", json={"code": totp.code_at(their_secret)}, headers=auth(token)
        )
        assert enabled.status_code == 200, enabled.text

        body = await _password(api, receptionist, device_token=device)
        assert body.get("two_factor_required") is True


class TestTurningItOnAndOff:
    async def test_anyone_may_turn_it_on_and_off_for_themselves(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        setup = await api.post(f"{AUTH}/2fa/setup", headers=auth(token))
        assert setup.status_code == 200, setup.text
        secret = setup.json()["secret"]

        enabled = await api.post(
            f"{AUTH}/2fa/enable", json={"code": totp.code_at(secret)}, headers=auth(token)
        )
        assert enabled.status_code == 200, enabled.text
        assert (await api.get(f"{AUTH}/me", headers=auth(token))).json()["two_factor_enabled"]
        assert (await _password(api, receptionist))["two_factor_required"] is True

        disabled = await api.post(
            f"{AUTH}/2fa/disable",
            json={"password": TEST_PASSWORD, "code": _next_code(secret)},
            headers=auth(token),
        )
        assert disabled.status_code == 200, disabled.text
        assert (await _password(api, receptionist))["access_token"]

    async def test_a_required_role_cannot_turn_it_off(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        secret = await _enrol(api, hospital_admin)
        body = await _password(api, hospital_admin)
        token = (
            await api.post(
                f"{AUTH}/2fa/verify",
                json={"challenge_token": body["challenge_token"], "code": _next_code(secret)},
            )
        ).json()["access_token"]

        refused = await api.post(
            f"{AUTH}/2fa/disable",
            json={"password": TEST_PASSWORD, "code": "123456"},
            headers=auth(token),
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "two_factor_required_by_role"

    async def test_turning_it_off_forgets_every_remembered_device(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        secret = (await api.post(f"{AUTH}/2fa/setup", headers=auth(token))).json()["secret"]
        await api.post(
            f"{AUTH}/2fa/enable", json={"code": totp.code_at(secret)}, headers=auth(token)
        )
        body = await _password(api, receptionist)
        device = (
            await api.post(
                f"{AUTH}/2fa/verify",
                json={
                    "challenge_token": body["challenge_token"],
                    "code": _next_code(secret),
                    "remember_device": True,
                },
            )
        ).json()["device_token"]

        # Off, then on again with a fresh secret.
        await api.post(
            f"{AUTH}/2fa/disable",
            # Turning it off needs the password as well as a code, so the code
            # is checked but not spent — it may be the one just used to sign in.
            json={"password": TEST_PASSWORD, "code": _next_code(secret)},
            headers=auth(token),
        )
        fresh = (await api.post(f"{AUTH}/2fa/setup", headers=auth(token))).json()["secret"]
        await api.post(
            f"{AUTH}/2fa/enable", json={"code": totp.code_at(fresh)}, headers=auth(token)
        )

        body = await _password(api, receptionist, device_token=device)
        assert body.get("two_factor_required") is True

    async def test_switching_it_on_is_audited(
        self, api: AsyncClient, receptionist: User, hospital_admin: User
    ) -> None:
        token = await login(api, receptionist)
        secret = (await api.post(f"{AUTH}/2fa/setup", headers=auth(token))).json()["secret"]
        await api.post(
            f"{AUTH}/2fa/enable", json={"code": totp.code_at(secret)}, headers=auth(token)
        )

        logs = await api.get(
            "/api/v1/audit-logs",
            params={"action": "auth.2fa.enable"},
            headers=auth(await login(api, hospital_admin)),
        )
        assert [row["resource_id"] for row in logs.json()["items"]] == [str(receptionist.id)]


class TestNoGuessingAroundTheLock:
    async def test_a_correct_password_does_not_reset_the_count_of_wrong_codes(
        self, api: AsyncClient, hospital_admin: User, admins_need_it: None
    ) -> None:
        """Otherwise someone who has the password alternates: password, four
        guesses, password, four guesses — and never reaches the lock."""
        await _enrol(api, hospital_admin)
        for _ in range(2):
            body = await _password(api, hospital_admin)
            for _ in range(3):
                await api.post(
                    f"{AUTH}/2fa/verify",
                    json={"challenge_token": body["challenge_token"], "code": "000000"},
                )

        locked = await api.post(
            f"{AUTH}/login", json={"email": hospital_admin.email, "password": TEST_PASSWORD}
        )
        assert locked.status_code == 401
        assert locked.json()["error"]["code"] == "account_locked"
