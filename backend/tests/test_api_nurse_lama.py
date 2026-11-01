"""UX-04: the nurse in charge records a patient who has left against advice.

On a night ward the doctor may not be on the floor, and "absconded at 02:10"
has to be written down when it is noticed — not at the morning round. The
nurse used to get a 403. Death and referral stay with doctors and records.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration


async def test_a_nurse_records_a_patient_leaving_against_advice(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    admin = await login(api, hospital_admin)
    patient = await api.post(
        "/api/v1/patients",
        json={"full_name": "Ram Prasad", "phone": "9812345670", "gender": "MALE", "age_years": 71},
        headers=auth(admin),
    )
    visit = await api.post(
        "/api/v1/encounters", json={"patient_id": patient.json()["id"]}, headers=auth(admin)
    )
    nurse = await login(
        api, await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)
    )

    response = await api.post(
        f"/api/v1/encounters/{visit.json()['id']}/lama",
        json={"lama_reason": "Not in bed at the 02:00 check; ward and grounds searched."},
        headers=auth(nurse),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "LAMA"


async def test_a_nurse_still_cannot_record_a_death(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    admin = await login(api, hospital_admin)
    patient = await api.post(
        "/api/v1/patients",
        json={"full_name": "Ram Prasad", "phone": "9812345671", "gender": "MALE", "age_years": 71},
        headers=auth(admin),
    )
    visit = await api.post(
        "/api/v1/encounters", json={"patient_id": patient.json()["id"]}, headers=auth(admin)
    )
    nurse = await login(
        api, await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)
    )
    response = await api.post(
        f"/api/v1/encounters/{visit.json()['id']}/death",
        json={"cause_of_death": "Unknown", "deceased_at": "2026-01-01T00:00:00Z"},
        headers=auth(nurse),
    )
    assert response.status_code == 403
