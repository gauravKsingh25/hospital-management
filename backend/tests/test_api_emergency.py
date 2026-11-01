"""Emergency registration and identification over HTTP (UX-01).

The service tests pin the record. These pin the counter's experience of it:
one call puts an unknown patient at the top of a doctor's line with a chart
open, and one call later says who they were — with every side door shut.
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

PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
QUEUE = "/api/v1/queue"
ENCOUNTERS = "/api/v1/encounters"
ADMISSION_REQUESTS = "/api/v1/ipd/admission-requests"


@pytest.fixture
async def doctor_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)


@pytest.fixture
async def admin_token(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def reception_token(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def doctor_id(api: AsyncClient, admin_token: str, doctor_user: User) -> str:
    response = await api.post(
        DOCTORS,
        json={"user_id": str(doctor_user.id), "specialty": "Emergency Medicine"},
        headers=auth(admin_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _register(api: AsyncClient, token: str, name: str, phone: str) -> str:
    response = await api.post(
        PATIENTS,
        json={"full_name": name, "phone": phone, "gender": "MALE", "age_years": 52},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _emergency(api: AsyncClient, token: str, doctor_id: str, **extra: object) -> dict:  # type: ignore[type-arg]
    response = await api.post(
        f"{QUEUE}/emergency",
        json={"gender": "MALE", "age_years": 40, "doctor_id": doctor_id, **extra},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


class TestOneTapRegistration:
    async def test_an_unknown_patient_goes_to_the_top_of_the_line(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        waiting = await _register(api, reception_token, "Asha Rani", "9811111111")
        queued = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": waiting, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )
        assert queued.status_code == 201, queued.text

        result = await _emergency(api, reception_token, doctor_id, complaint="RTA, unconscious")

        assert result["patient_name"] == "Unknown Male EM-0001"
        assert result["emergency_tag"] == "EM-0001"
        assert result["uhid"]
        assert result["patients_ahead"] == 0
        assert result["queue_entry"]["priority"] == 10
        assert result["admission_request_id"] is None

        board = (await api.get(QUEUE, headers=auth(reception_token))).json()
        assert [row["patient_name"] for row in board] == ["Unknown Male EM-0001", "Asha Rani"]
        assert board[0]["patient_is_unidentified"] is True
        assert board[1]["patient_is_unidentified"] is False

    async def test_the_chart_is_an_emergency_visit(
        self, api: AsyncClient, reception_token: str, admin_token: str, doctor_id: str
    ) -> None:
        result = await _emergency(api, reception_token, doctor_id, complaint="RTA, unconscious")

        visit = await api.get(f"{ENCOUNTERS}/{result['encounter_id']}", headers=auth(admin_token))
        assert visit.status_code == 200, visit.text
        assert visit.json()["encounter_type"] == "EMERGENCY"
        assert visit.json()["status"] == "REGISTERED"
        assert visit.json()["chief_complaint"] == "RTA, unconscious"

    async def test_it_can_go_straight_to_the_admission_desk(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        result = await _emergency(api, reception_token, doctor_id, send_to_admission=True)

        assert result["admission_request_id"] is not None
        requests = (await api.get(ADMISSION_REQUESTS, headers=auth(reception_token))).json()[
            "items"
        ]
        assert [row["encounter_id"] for row in requests] == [result["encounter_id"]]

    async def test_it_is_all_or_nothing(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        """A failure part-way must not leave an unknown patient with no visit —
        a record nobody can name and nobody is looking after."""
        response = await api.post(
            f"{QUEUE}/emergency",
            json={
                "gender": "MALE",
                "doctor_id": "00000000-0000-0000-0000-000000000000",
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 404, response.text

        listed = await api.get(
            PATIENTS, params={"unidentified": "true"}, headers=auth(reception_token)
        )
        assert listed.json()["total"] == 0

    async def test_only_staff_who_register_patients_may_use_it(
        self, api: AsyncClient, session: AsyncSession, hospital: Hospital, doctor_id: str
    ) -> None:
        nurse = await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)
        token = await login(api, nurse)

        response = await api.post(
            f"{QUEUE}/emergency",
            json={"gender": "MALE", "doctor_id": doctor_id},
            headers=auth(token),
        )
        assert response.status_code == 403, response.text

    async def test_a_known_patient_can_be_an_emergency_too(
        self, api: AsyncClient, reception_token: str, admin_token: str, doctor_id: str
    ) -> None:
        """The registration form's Emergency switch: same line-jump, same kind
        of chart, for somebody whose name is known."""
        patient = await _register(api, reception_token, "Mohan Lal", "9822222222")
        response = await api.post(
            f"{QUEUE}/quick-opd",
            json={
                "patient_id": patient,
                "doctor_id": doctor_id,
                "priority": 10,
                "priority_reason": "Emergency",
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 201, response.text

        visit = await api.get(
            f"{ENCOUNTERS}/{response.json()['encounter_id']}", headers=auth(admin_token)
        )
        assert visit.json()["encounter_type"] == "EMERGENCY"


class TestNeedsIdentification:
    async def test_the_unidentified_are_listed_for_reception(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        result = await _emergency(api, reception_token, doctor_id)
        await _register(api, reception_token, "Asha Rani", "9811111111")

        listed = await api.get(
            PATIENTS, params={"unidentified": "true"}, headers=auth(reception_token)
        )
        assert listed.status_code == 200, listed.text
        rows = listed.json()["items"]
        assert [row["uhid"] for row in rows] == [result["uhid"]]
        assert rows[0]["phone"] is None
        assert rows[0]["is_unidentified"] is True
        assert rows[0]["emergency_tag"] == "EM-0001"


class TestIdentification:
    async def test_reception_names_a_new_person_and_the_board_follows(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        result = await _emergency(api, reception_token, doctor_id)
        patient_id = result["appointment"]["patient_id"]

        response = await api.post(
            f"{PATIENTS}/{patient_id}/identify",
            json={
                "full_name": "Suresh Yadav",
                "phone": "9898989898",
                "gender": "MALE",
                "age_years": 38,
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["full_name"] == "Suresh Yadav"
        assert body["is_unidentified"] is False
        assert body["identified_at"] is not None
        assert body["uhid"] == result["uhid"]

        [row] = (await api.get(QUEUE, headers=auth(reception_token))).json()
        assert row["patient_name"] == "Suresh Yadav"
        assert row["patient_is_unidentified"] is False

    async def test_reception_links_somebody_already_registered(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        """Reception holds `patient:update`, not `patient:merge` — and is the
        desk the relatives walk up to at night."""
        old = await _register(api, reception_token, "Ramesh Kumar", "9812345670")
        old_uhid = (await api.get(f"{PATIENTS}/{old}", headers=auth(reception_token))).json()[
            "uhid"
        ]
        result = await _emergency(api, reception_token, doctor_id)
        patient_id = result["appointment"]["patient_id"]

        response = await api.post(
            f"{PATIENTS}/{patient_id}/identify",
            json={"existing_patient_id": old},
            headers=auth(reception_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["full_name"] == "Ramesh Kumar"
        assert response.json()["id"] == patient_id

        # The old card opens the patient who is here now.
        card = await api.get(f"{PATIENTS}/by-uhid/{old_uhid}", headers=auth(reception_token))
        assert card.json()["id"] == patient_id

    async def test_an_exact_match_comes_back_as_a_record_to_link(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        old = await _register(api, reception_token, "Ramesh Kumar", "9812345670")
        result = await _emergency(api, reception_token, doctor_id)

        response = await api.post(
            f"{PATIENTS}/{result['appointment']['patient_id']}/identify",
            json={
                "full_name": "Ramesh Kumar",
                "phone": "9812345670",
                "gender": "MALE",
                "age_years": 52,
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert error["code"] == "patient_already_exists"
        assert error["details"]["patient_id"] == old

    async def test_identification_is_audited(
        self, api: AsyncClient, reception_token: str, admin_token: str, doctor_id: str
    ) -> None:
        result = await _emergency(api, reception_token, doctor_id)
        patient_id = result["appointment"]["patient_id"]
        await api.post(
            f"{PATIENTS}/{patient_id}/identify",
            json={
                "full_name": "Suresh Yadav",
                "phone": "9898989898",
                "gender": "MALE",
                "age_years": 38,
            },
            headers=auth(reception_token),
        )

        logs = await api.get(
            "/api/v1/audit-logs",
            params={"action": "patient.identify"},
            headers=auth(admin_token),
        )
        assert logs.status_code == 200, logs.text
        [entry] = logs.json()["items"]
        assert entry["resource_id"] == patient_id
        assert entry["changes"]["emergency_tag"] == "EM-0001"
        assert entry["changes"]["identified_as"] == "Suresh Yadav"


class TestSideDoors:
    async def test_the_profile_edit_cannot_name_an_unknown_patient(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        result = await _emergency(api, reception_token, doctor_id)

        response = await api.patch(
            f"{PATIENTS}/{result['appointment']['patient_id']}",
            json={"full_name": "Suresh Yadav"},
            headers=auth(reception_token),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "identify_first"

    async def test_the_general_merge_cannot_take_an_unknown_patient(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        old = await _register(api, reception_token, "Ramesh Kumar", "9812345670")
        result = await _emergency(api, reception_token, doctor_id)

        response = await api.post(
            f"{PATIENTS}/{old}/merge",
            json={"duplicate_id": result["appointment"]["patient_id"], "reason": "Same man"},
            headers=auth(admin_token),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "use_identify"
