"""SEC-03: the outbox is not a list of every patient's phone and every message.

The audit's evidence: as the lab, `GET /notifications/{id}` returned the
patient's phone, the full message body and the delivery log — for every
patient in the hospital.

Now:

* the hospital-wide outbox belongs to the desks that answer "I never got the
  message" — reception, records, admin — and the auditor;
* any role that reads messages can still read one patient's, from that patient;
* a role that never contacts patients sees the number masked and not the text.
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

NOTIFICATIONS = "/api/v1/notifications"
BODY = "Sunrise: your report is ready for collection."


async def _token(api: AsyncClient, session: AsyncSession, hospital: Hospital, role: str) -> str:
    return await login(api, await _make_user(session, role_code=role, hospital_id=hospital.id))


@pytest.fixture
async def reception(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def message(api: AsyncClient, reception: str) -> dict[str, str]:
    patient = await api.post(
        "/api/v1/patients",
        json={
            "full_name": "Sunita Devi",
            "phone": "9812345670",
            "gender": "FEMALE",
            "age_years": 34,
        },
        headers=auth(reception),
    )
    sent = await api.post(
        NOTIFICATIONS,
        json={
            "recipient_type": "PATIENT",
            "patient_id": patient.json()["id"],
            "category": "REPORT",
            "body": BODY,
        },
        headers=auth(reception),
    )
    assert sent.status_code == 201, sent.text
    return {"patient": patient.json()["id"], "message": sent.json()["id"]}


@pytest.mark.parametrize("role", [Roles.LAB_TECH, Roles.CASHIER, Roles.NURSE, Roles.DOCTOR])
async def test_the_whole_outbox_is_not_for_every_role(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, role: str
) -> None:
    token = await _token(api, session, hospital, role)
    response = await api.get(NOTIFICATIONS, headers=auth(token))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("role", [Roles.RECEPTIONIST, Roles.RECORDS_OFFICER, Roles.AUDITOR])
async def test_the_desks_that_answer_for_messages_keep_it(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, role: str
) -> None:
    token = await _token(api, session, hospital, role)
    assert (await api.get(NOTIFICATIONS, headers=auth(token))).status_code == 200


async def test_one_patients_messages_are_still_readable_from_that_patient(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    message: dict[str, str],
) -> None:
    token = await _token(api, session, hospital, Roles.LAB_TECH)
    response = await api.get(
        NOTIFICATIONS, params={"patient_id": message["patient"]}, headers=auth(token)
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [message["message"]]


async def test_a_role_that_never_contacts_patients_sees_no_number_and_no_text(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    message: dict[str, str],
) -> None:
    token = await _token(api, session, hospital, Roles.LAB_TECH)
    detail = await api.get(f"{NOTIFICATIONS}/{message['message']}", headers=auth(token))
    assert detail.status_code == 200, detail.text
    body = detail.json()

    assert body["recipient_phone"] == "98•••••670"
    assert body["body"] is None
    assert "9812345670" not in detail.text
    assert BODY not in detail.text
    # What happened to it is still there: the lab can say "it was sent".
    assert body["status"]


@pytest.mark.parametrize("role", [Roles.DOCTOR, Roles.RECEPTIONIST])
async def test_the_people_who_talk_to_patients_read_it_in_full(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    message: dict[str, str],
    role: str,
) -> None:
    token = await _token(api, session, hospital, role)
    body = (await api.get(f"{NOTIFICATIONS}/{message['message']}", headers=auth(token))).json()
    assert body["recipient_phone"] == "9812345670"
    assert body["body"] == BODY
