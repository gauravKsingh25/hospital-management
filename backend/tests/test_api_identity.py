"""End-to-end HTTP behaviour: authentication, RBAC gating, tenant scoping.

These go through the real dependency stack — bearer token, permission check,
tenant binding — which is the only place all three are exercised together.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import TEST_PASSWORD, auth, login

pytestmark = pytest.mark.integration

STRONG_PASSWORD = "a-perfectly-reasonable-passphrase"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
USERS = "/api/v1/users"
HOSPITALS = "/api/v1/hospitals"
AUDIT = "/api/v1/audit-logs"


class TestLoginEndpoint:
    async def test_valid_credentials_return_a_token_pair(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        response = await api.post(
            LOGIN, json={"email": receptionist.email, "password": TEST_PASSWORD}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0

    async def test_bad_credentials_return_401_without_detail(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        response = await api.post(
            LOGIN, json={"email": receptionist.email, "password": "wrong-password-here"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_response_never_contains_a_password_hash(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        response = await api.get(ME, headers=auth(token))

        assert response.status_code == 200
        body = response.json()
        # `must_change_password` is a legitimate flag; the secrets are not.
        for secret in ("password_hash", "totp_secret", "two_factor_secret"):
            assert secret not in body
        assert receptionist.password_hash not in response.text

    async def test_refresh_rotates_the_token(self, api: AsyncClient, receptionist: User) -> None:
        pair = (
            await api.post(LOGIN, json={"email": receptionist.email, "password": TEST_PASSWORD})
        ).json()

        rotated = await api.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != pair["refresh_token"]

    async def test_logout_then_refresh_is_refused(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        pair = (
            await api.post(LOGIN, json={"email": receptionist.email, "password": TEST_PASSWORD})
        ).json()

        logout = await api.post(
            "/api/v1/auth/logout", json={"refresh_token": pair["refresh_token"]}
        )
        assert logout.status_code == 204

        retry = await api.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert retry.status_code == 401


class TestAuthenticationRequired:
    async def test_protected_route_without_a_token_is_401(self, api: AsyncClient) -> None:
        assert (await api.get(USERS)).status_code == 401

    async def test_garbage_token_is_401(self, api: AsyncClient) -> None:
        response = await api.get(USERS, headers=auth("not-a-real-token"))
        assert response.status_code == 401

    async def test_me_returns_roles_and_permissions(
        self, api: AsyncClient, hospital_admin: User
    ) -> None:
        token = await login(api, hospital_admin)
        body = (await api.get(ME, headers=auth(token))).json()

        assert body["email"] == hospital_admin.email
        assert Roles.HOSPITAL_ADMIN in body["roles"]
        assert "user:create" in body["permissions"]


class TestPermissionGating:
    async def test_a_receptionist_cannot_list_users(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        """The receptionist role holds no `user:read` permission yet."""
        token = await login(api, receptionist)
        response = await api.get(USERS, headers=auth(token))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_hospital_admin_can_list_users(
        self, api: AsyncClient, hospital_admin: User
    ) -> None:
        token = await login(api, hospital_admin)
        response = await api.get(USERS, headers=auth(token))

        assert response.status_code == 200
        assert response.json()["total"] >= 1

    async def test_a_hospital_admin_cannot_create_a_hospital(
        self, api: AsyncClient, hospital_admin: User
    ) -> None:
        """Registering a tenant is a platform-admin act."""
        token = await login(api, hospital_admin)
        response = await api.post(
            HOSPITALS, json={"code": "NEW1", "name": "Sneaky Hospital"}, headers=auth(token)
        )

        assert response.status_code == 403

    async def test_a_platform_admin_can_create_a_hospital(
        self, api: AsyncClient, platform_admin: User
    ) -> None:
        token = await login(api, platform_admin)
        response = await api.post(
            HOSPITALS,
            json={"code": "KMC", "name": "Kasturba Medical Centre", "city": "Manipal"},
            headers=auth(token),
        )

        assert response.status_code == 201, response.text
        assert response.json()["code"] == "KMC"

    async def test_a_receptionist_cannot_read_the_audit_log(
        self, api: AsyncClient, receptionist: User
    ) -> None:
        token = await login(api, receptionist)
        assert (await api.get(AUDIT, headers=auth(token))).status_code == 403


class TestTenantScopingOverHttp:
    async def test_a_hospital_admin_sees_only_their_own_hospital(
        self, api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
    ) -> None:
        from app.core import database as db

        async with db.system_context(session):
            other = Hospital(code=f"Z{uuid.uuid4().hex[:5].upper()}", name="Rival Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        token = await login(api, hospital_admin)
        body = (await api.get(HOSPITALS, headers=auth(token))).json()

        assert body["total"] == 1
        assert body["items"][0]["id"] == str(hospital.id)

        # And addressing the other tenant directly is a 404, not a 403 — a 403
        # would confirm it exists.
        direct = await api.get(f"{HOSPITALS}/{other_id}", headers=auth(token))
        assert direct.status_code == 404

    async def test_a_platform_admin_sees_every_hospital(
        self, api: AsyncClient, hospital: Hospital, platform_admin: User
    ) -> None:
        token = await login(api, platform_admin)
        body = (await api.get(HOSPITALS, headers=auth(token))).json()
        assert body["total"] >= 1

    async def test_created_users_land_in_the_callers_tenant_not_the_requested_one(
        self, api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
    ) -> None:
        """A hospital admin must not be able to plant a user in another tenant."""
        from app.core import database as db

        async with db.system_context(session):
            other = Hospital(code=f"Y{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        token = await login(api, hospital_admin)
        response = await api.post(
            USERS,
            json={
                "email": "planted@example.com",
                "full_name": "Planted User",
                "password": STRONG_PASSWORD,
                "role_codes": [Roles.NURSE],
                "hospital_id": str(other_id),
            },
            headers=auth(token),
        )

        assert response.status_code == 403


class TestStaffAdministration:
    """The staff screen has to answer "who is a doctor here?".

    `UserRead` could not: the roles live in a join table and the schema never
    carried them, so an administration screen would have had to ask per row —
    on the one list where every row is a person whose access matters.
    """

    async def test_a_staff_row_carries_its_roles(
        self, api: AsyncClient, hospital_admin: User
    ) -> None:
        token = await login(api, hospital_admin)
        body = (await api.get(USERS, headers=auth(token))).json()

        rows = {row["email"]: row for row in body["items"]}
        assert rows[hospital_admin.email]["roles"] == [Roles.HOSPITAL_ADMIN]

    async def test_roles_cost_one_query_regardless_of_staff_count(
        self, api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
    ) -> None:
        """A hospital's full staff list must not be a request per person."""
        from tests.conftest import _make_user

        for _ in range(4):
            await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)

        token = await login(api, hospital_admin)

        statements: list[str] = []
        from sqlalchemy import event

        from app.core.database import get_engine

        engine = get_engine().sync_engine

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if "FROM user_roles" in statement or "FROM roles" in statement:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await api.get(USERS, headers=auth(token))
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        assert response.json()["total"] >= 5
        # One join for the whole page. The permission check for the *caller*
        # may add its own lookup, so this allows a small constant rather than
        # exactly one — what it refuses is growth with the number of rows.
        assert len(statements) <= 2, f"expected a bulk role lookup, got {len(statements)} queries"

    async def test_assigning_roles_returns_the_new_role_set(
        self, api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
    ) -> None:
        """The response has to show what it just changed.

        Without roles on the body the screen has to re-fetch to find out
        whether the assignment worked, and a UI that cannot confirm its own
        write is a UI people click twice.
        """
        from tests.conftest import _make_user

        nurse = await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)
        token = await login(api, hospital_admin)

        response = await api.post(
            f"{USERS}/{nurse.id}/roles",
            json={"role_codes": [Roles.NURSE, Roles.RECEPTIONIST]},
            headers=auth(token),
        )

        assert response.status_code == 200, response.text
        assert sorted(response.json()["roles"]) == sorted([Roles.NURSE, Roles.RECEPTIONIST])


class TestPagination:
    async def test_list_endpoints_are_paginated(
        self, api: AsyncClient, hospital_admin: User
    ) -> None:
        token = await login(api, hospital_admin)
        body = (await api.get(f"{USERS}?limit=1&offset=0", headers=auth(token))).json()

        # The exact envelope, asserted as a set so an accidental extra field
        # is caught rather than tolerated. `has_more` joined it when it became
        # a `computed_field` — before that it was declared on `Page` and
        # silently absent from every response in the system.
        assert set(body) == {"items", "total", "limit", "offset", "has_more"}
        assert body["limit"] == 1

    async def test_page_size_is_capped(self, api: AsyncClient, hospital_admin: User) -> None:
        """An unbounded list against a million-row table is an outage."""
        token = await login(api, hospital_admin)
        response = await api.get(f"{USERS}?limit=100000", headers=auth(token))
        assert response.status_code == 422
