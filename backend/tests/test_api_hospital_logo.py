"""The hospital's logo, for the top of every bill.

Stored per tenant in its own table, versioned: a new upload adds a version and
an issued bill keeps pointing at the one it was printed with. Uploaded as the
raw file body (no multipart — the backend carries no form parser), checked by
its first bytes rather than by what the browser claims it is.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

# The smallest valid PNG: signature, IHDR, IDAT, IEND (1x1, transparent).
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


def _logo(hospital: Hospital) -> str:
    return f"/api/v1/hospitals/{hospital.id}/logo"


async def _upload(api: AsyncClient, token: str, hospital: Hospital, body: bytes, kind: str):
    return await api.put(
        _logo(hospital), content=body, headers={**auth(token), "Content-Type": kind}
    )


@pytest.fixture
async def admin_token(api: AsyncClient, session: AsyncSession, hospital: Hospital) -> str:
    user = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=hospital.id)
    return await login(api, user)


async def test_admin_uploads_a_png_and_it_versions(
    api: AsyncClient, admin_token: str, hospital: Hospital
) -> None:
    first = await _upload(api, admin_token, hospital, PNG, "image/png")
    assert first.status_code == 200, first.text
    assert first.json() == {"version": 1}

    second = await _upload(api, admin_token, hospital, JPEG, "image/jpeg")
    assert second.json() == {"version": 2}

    latest = await api.get(_logo(hospital), headers=auth(admin_token))
    assert latest.status_code == 200
    assert latest.headers["content-type"] == "image/jpeg"
    assert latest.content == JPEG

    old = await api.get(_logo(hospital), params={"version": 1}, headers=auth(admin_token))
    assert old.content == PNG
    assert old.headers["content-type"] == "image/png"

    profile = await api.get(f"/api/v1/hospitals/{hospital.id}", headers=auth(admin_token))
    assert profile.json()["logo_version"] == 2


async def test_a_gif_or_oversized_file_is_refused(
    api: AsyncClient, admin_token: str, hospital: Hospital
) -> None:
    disguised = await _upload(api, admin_token, hospital, GIF, "image/png")
    assert disguised.status_code == 422, disguised.text
    assert disguised.json()["error"]["code"] == "logo_invalid"

    huge = await _upload(api, admin_token, hospital, PNG + b"\0" * 512_001, "image/png")
    assert huge.status_code == 422, huge.text
    assert huge.json()["error"]["code"] == "logo_invalid"

    missing = await api.get(_logo(hospital), headers=auth(admin_token))
    assert missing.status_code == 404


async def test_a_receptionist_cannot_change_the_logo(
    api: AsyncClient, session: AsyncSession, hospital: Hospital
) -> None:
    user = await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=hospital.id)
    token = await login(api, user)

    response = await _upload(api, token, hospital, PNG, "image/png")

    assert response.status_code == 403


async def test_another_hospital_cannot_read_the_logo(
    api: AsyncClient, session: AsyncSession, admin_token: str, hospital: Hospital
) -> None:
    await _upload(api, admin_token, hospital, PNG, "image/png")
    async with db.system_context(session):
        other = Hospital(code=f"H{uuid.uuid4().hex[:6].upper()}", name="Other Hospital")
        session.add(other)
        await session.flush()
    await session.commit()
    outsider = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=other.id)

    response = await api.get(_logo(hospital), headers=auth(await login(api, outsider)))

    assert response.status_code == 404
