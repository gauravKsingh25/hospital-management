"""SEC-08: an update that names a field it cannot change is refused, not ignored.

`PATCH /encounters/{id}` with `{"status": "COMPLETED"}` used to return 200 and
change nothing. Not exploitable — the status did not move — but the client was
told it had succeeded, and a screen built on that believes a visit is closed
when it is not. A 422 naming the field says plainly what happened.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.modules.identity.models import User
from tests.conftest import auth, login

pytestmark = pytest.mark.integration


async def test_patching_a_visits_status_is_refused(api: AsyncClient, hospital_admin: User) -> None:
    token = await login(api, hospital_admin)
    patient = await api.post(
        "/api/v1/patients",
        json={"full_name": "Ram Prasad", "phone": "9812345670", "gender": "MALE", "age_years": 71},
        headers=auth(token),
    )
    visit = await api.post(
        "/api/v1/encounters", json={"patient_id": patient.json()["id"]}, headers=auth(token)
    )

    response = await api.patch(
        f"/api/v1/encounters/{visit.json()['id']}",
        json={"status": "COMPLETED"},
        headers=auth(token),
    )
    assert response.status_code == 422, response.text
    assert "status" in response.text

    still = await api.get(f"/api/v1/encounters/{visit.json()['id']}", headers=auth(token))
    assert still.json()["status"] == "REGISTERED"


async def test_patching_a_patients_uhid_is_refused(api: AsyncClient, hospital_admin: User) -> None:
    token = await login(api, hospital_admin)
    patient = await api.post(
        "/api/v1/patients",
        json={"full_name": "Ram Prasad", "phone": "9812345671", "gender": "MALE", "age_years": 71},
        headers=auth(token),
    )
    response = await api.patch(
        f"/api/v1/patients/{patient.json()['id']}",
        json={"uhid": "FORGED-1"},
        headers=auth(token),
    )
    assert response.status_code == 422, response.text


async def test_a_real_update_still_works(api: AsyncClient, hospital_admin: User) -> None:
    token = await login(api, hospital_admin)
    patient = await api.post(
        "/api/v1/patients",
        json={"full_name": "Ram Prasad", "phone": "9812345672", "gender": "MALE", "age_years": 71},
        headers=auth(token),
    )
    response = await api.patch(
        f"/api/v1/patients/{patient.json()['id']}",
        json={"city": "Varanasi"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["city"] == "Varanasi"
