"""SEC-01: the patient directory is not a phone book anybody can take home.

The audit's evidence: logged in as the lab, `GET /patients?limit=100&offset=…`
returned all 718 patients — names, phones, birth dates — in eight calls, and
the audit log showed nothing. A departing employee could walk out with every
patient's number and nobody would ever know.

Four things now stand in the way, and each is pinned here:

* every page of the directory is an audit row;
* only the desks that look patients up by browsing — reception, records,
  admin — may browse it at all;
* a role that never dials a patient sees `98•••••670`, not the number;
* one person reading hundreds of patients in an hour is flagged to admins.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
AUDIT = "/api/v1/audit-logs"


async def _token(api: AsyncClient, session: AsyncSession, hospital: Hospital, role: str) -> str:
    user = await _make_user(session, role_code=role, hospital_id=hospital.id)
    return await login(api, user)


@pytest.fixture
async def reception(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def admin(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def patient(api: AsyncClient, reception: str) -> dict:  # type: ignore[type-arg]
    response = await api.post(
        PATIENTS,
        json={
            "full_name": "Sunita Devi",
            "phone": "9812345670",
            "gender": "FEMALE",
            "age_years": 34,
            "guardian_name": "Ram Devi",
            "guardian_phone": "9700011122",
        },
        headers=auth(reception),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


class TestEveryPageIsAudited:
    async def test_a_page_of_the_directory_leaves_an_audit_row(
        self,
        api: AsyncClient,
        reception: str,
        admin: str,
        patient: dict,  # type: ignore[type-arg]
    ) -> None:
        response = await api.get(
            PATIENTS, params={"limit": 10, "offset": 0, "gender": "FEMALE"}, headers=auth(reception)
        )
        assert response.status_code == 200, response.text

        logs = await api.get(AUDIT, params={"action": "patient.list"}, headers=auth(admin))
        [entry] = logs.json()["items"]
        assert entry["changes"]["offset"] == 0
        assert entry["changes"]["returned"] == 1
        assert entry["changes"]["filters"]["gender"] == "FEMALE"


class TestOnlyTheDesksBrowse:
    @pytest.mark.parametrize(
        "role",
        [Roles.LAB_TECH, Roles.CASHIER, Roles.ACCOUNTANT, Roles.NURSE, Roles.DOCTOR],
    )
    async def test_a_role_that_works_from_a_visit_cannot_page_the_directory(
        self, api: AsyncClient, session: AsyncSession, hospital: Hospital, role: str
    ) -> None:
        token = await _token(api, session, hospital, role)
        response = await api.get(PATIENTS, headers=auth(token))
        assert response.status_code == 403, response.text

    @pytest.mark.parametrize("role", [Roles.RECEPTIONIST, Roles.RECORDS_OFFICER])
    async def test_the_front_desk_and_records_can(
        self, api: AsyncClient, session: AsyncSession, hospital: Hospital, role: str
    ) -> None:
        token = await _token(api, session, hospital, role)
        response = await api.get(PATIENTS, headers=auth(token))
        assert response.status_code == 200, response.text

    async def test_looking_one_patient_up_still_works_for_everyone_who_reads(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        patient: dict,  # type: ignore[type-arg]
    ) -> None:
        """The lab still has to know whose sample it is."""
        token = await _token(api, session, hospital, Roles.LAB_TECH)
        assert (
            await api.get(f"{PATIENTS}/{patient['id']}", headers=auth(token))
        ).status_code == 200
        found = await api.get(f"{PATIENTS}/search", params={"q": "Sunita"}, headers=auth(token))
        assert found.status_code == 200


class TestPhonesAreMasked:
    async def test_a_role_that_never_dials_sees_a_masked_number(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        patient: dict,  # type: ignore[type-arg]
    ) -> None:
        token = await _token(api, session, hospital, Roles.LAB_TECH)

        detail = (await api.get(f"{PATIENTS}/{patient['id']}", headers=auth(token))).json()
        assert detail["phone"] == "98•••••670"
        assert detail["guardian_phone"] == "97•••••122"

        [hit] = (
            await api.get(f"{PATIENTS}/search", params={"q": "Sunita"}, headers=auth(token))
        ).json()["items"]
        assert hit["phone"] == "98•••••670"

        card = await api.get(f"{PATIENTS}/by-uhid/{patient['uhid']}", headers=auth(token))
        assert card.json()["phone"] == "98•••••670"

    async def test_the_universal_search_masks_it_too(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        patient: dict,  # type: ignore[type-arg]
    ) -> None:
        token = await _token(api, session, hospital, Roles.CASHIER)
        results = (
            await api.get("/api/v1/search", params={"q": "Sunita"}, headers=auth(token))
        ).json()
        subtitles = [hit["subtitle"] for hit in results["hits"] if hit["kind"] == "PATIENT"]
        assert subtitles and all("9812345670" not in subtitle for subtitle in subtitles)
        assert any("98•••••670" in subtitle for subtitle in subtitles)

    @pytest.mark.parametrize(
        "role", [Roles.RECEPTIONIST, Roles.DOCTOR, Roles.NURSE, Roles.ADMISSION_DESK]
    )
    async def test_the_people_who_ring_patients_see_the_number(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        patient: dict,  # type: ignore[type-arg]
        role: str,
    ) -> None:
        token = await _token(api, session, hospital, role)
        detail = (await api.get(f"{PATIENTS}/{patient['id']}", headers=auth(token))).json()
        assert detail["phone"] == "9812345670"


class TestBulkReadingIsFlagged:
    async def test_reading_past_the_threshold_alerts_the_admins_once(
        self,
        api: AsyncClient,
        reception: str,
        admin: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "PATIENT_BULK_READ_ALERT", 3)
        for index in range(5):
            await api.post(
                PATIENTS,
                json={
                    "full_name": f"Bulk Patient {uuid.uuid4().hex[:6]}",
                    "phone": f"98000000{index:02d}",
                    "gender": "MALE",
                    "age_years": 40,
                },
                headers=auth(reception),
            )

        # Past the threshold on the first page, and again on the second: one alert.
        for offset in (0, 2):
            response = await api.get(
                PATIENTS, params={"limit": 3, "offset": offset}, headers=auth(reception)
            )
            assert response.status_code == 200

        logs = await api.get(
            AUDIT, params={"action": "patient.bulk_read_alert"}, headers=auth(admin)
        )
        [entry] = logs.json()["items"]
        assert entry["changes"]["distinct_patients"] >= 3

        # And the administrators are told, not just written down.
        messages = await api.get(
            "/api/v1/notifications",
            params={"recipient_type": "STAFF", "limit": 50},
            headers=auth(admin),
        )
        alerts = [
            row for row in messages.json()["items"] if row["template_code"] == "BULK_PATIENT_READ"
        ]
        assert len(alerts) == 1
        assert alerts[0]["recipient_type"] == "STAFF"
