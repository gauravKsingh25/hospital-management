"""SEC-02: the timeline says *that* a visit ended, not the clinical why.

The audit's evidence: as the cashier, the patient timeline returned
`{"cause_of_death": "Acute MI", ...}`. The visit list already hid diagnoses
from the lab and the counter; the timeline was the gap. The cause of death,
the reason for a referral and the reason a patient left against advice are
clinical facts. The cashier needs to know the patient died — the bill goes to
the family — and nothing about why.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

ENCOUNTERS = "/api/v1/encounters"


@pytest.fixture
async def doctor(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)


async def _token(api: AsyncClient, session: AsyncSession, hospital: Hospital, role: str) -> str:
    return await login(api, await _make_user(session, role_code=role, hospital_id=hospital.id))


async def _visit(api: AsyncClient, token: str, phone: str) -> tuple[str, str]:
    patient = await api.post(
        "/api/v1/patients",
        json={"full_name": "Ram Prasad", "phone": phone, "gender": "MALE", "age_years": 71},
        headers=auth(token),
    )
    encounter = await api.post(
        ENCOUNTERS, json={"patient_id": patient.json()["id"]}, headers=auth(token)
    )
    assert encounter.status_code == 201, encounter.text
    return patient.json()["id"], encounter.json()["id"]


async def _ended_three_ways(api: AsyncClient, admin: str, doctor_token: str, doctor: User) -> str:
    """One patient, three visits: a referral, a LAMA, and a death."""
    patient_id, referred = await _visit(api, admin, "9812345670")
    response = await api.post(
        f"{ENCOUNTERS}/{referred}/referral",
        json={"referred_to_facility": "AIIMS Delhi", "referral_reason": "Needs cardiac surgery"},
        headers=auth(doctor_token),
    )
    assert response.status_code == 200, response.text

    left = (
        await api.post(ENCOUNTERS, json={"patient_id": patient_id}, headers=auth(admin))
    ).json()["id"]
    response = await api.post(
        f"{ENCOUNTERS}/{left}/lama",
        json={"lama_reason": "Family wanted ayurvedic treatment"},
        headers=auth(doctor_token),
    )
    assert response.status_code == 200, response.text

    died = (
        await api.post(ENCOUNTERS, json={"patient_id": patient_id}, headers=auth(admin))
    ).json()["id"]
    response = await api.post(
        f"{ENCOUNTERS}/{died}/death",
        json={
            "deceased_at": utc_now().isoformat(),
            "death_certified_by_id": str(doctor.id),
            "cause_of_death": "Acute MI",
            "death_place": "Ward",
            "reason": "Arrest on the ward round",
        },
        headers=auth(doctor_token),
    )
    assert response.status_code == 200, response.text
    return patient_id


async def test_the_cashier_sees_how_visits_ended_but_not_why(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User, doctor: User
) -> None:
    admin = await login(api, hospital_admin)
    patient_id = await _ended_three_ways(api, admin, await login(api, doctor), doctor)
    cashier = await _token(api, session, hospital, Roles.CASHIER)

    response = await api.get(f"{ENCOUNTERS}/patient/{patient_id}/timeline", headers=auth(cashier))
    assert response.status_code == 200, response.text
    events = response.json()["items"]
    endings = {event["to_status"]: event for event in events}

    # That the visits ended, and how, is still there: the bill depends on it.
    assert {"REFERRED_OUT", "LAMA", "DECEASED"} <= endings.keys()
    death = endings["DECEASED"]
    assert death["event_metadata"]["deceased_at"]
    # Why is not.
    text = str(events)
    for clinical in ("Acute MI", "Arrest on the ward round", "Needs cardiac surgery", "ayurvedic"):
        assert clinical not in text
    assert "cause_of_death" not in death["event_metadata"]
    assert death["reason"] is None


async def test_the_lab_sees_the_same_on_one_visit(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User, doctor: User
) -> None:
    admin = await login(api, hospital_admin)
    _, encounter = await _visit(api, admin, "9812345671")
    await api.post(
        f"{ENCOUNTERS}/{encounter}/death",
        json={
            "deceased_at": utc_now().isoformat(),
            "death_certified_by_id": str(doctor.id),
            "cause_of_death": "Acute MI",
        },
        headers=auth(await login(api, doctor)),
    )
    lab = await _token(api, session, hospital, Roles.LAB_TECH)

    response = await api.get(f"{ENCOUNTERS}/{encounter}/timeline", headers=auth(lab))
    assert response.status_code == 200, response.text
    assert "Acute MI" not in response.text

    # Nor on the visit itself, which carries the same fields.
    visit = await api.get(f"{ENCOUNTERS}/{encounter}", headers=auth(lab))
    assert visit.status_code == 200, visit.text
    assert visit.json()["status"] == "DECEASED"
    assert visit.json()["cause_of_death"] is None


async def test_the_doctor_still_reads_the_whole_story(
    api: AsyncClient, hospital_admin: User, doctor: User
) -> None:
    admin = await login(api, hospital_admin)
    doctor_token = await login(api, doctor)
    patient_id = await _ended_three_ways(api, admin, doctor_token, doctor)

    response = await api.get(
        f"{ENCOUNTERS}/patient/{patient_id}/timeline", headers=auth(doctor_token)
    )
    endings = {event["to_status"]: event for event in response.json()["items"]}
    assert endings["DECEASED"]["event_metadata"]["cause_of_death"] == "Acute MI"
    assert endings["REFERRED_OUT"]["reason"] == "Needs cardiac surgery"
    assert endings["LAMA"]["event_metadata"]["lama_reason"] == "Family wanted ayurvedic treatment"
