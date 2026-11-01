"""SAFE-01 over HTTP: the refusal, the override on the record, the drug round.

Reproduces the audit's case end to end — a CRITICAL "Aspirin — anaphylaxis"
alert, then Aspirin 300 mg ordered — and pins what each person now sees: the
doctor a 409 naming the allergy, the auditor a row for every override, and the
nurse the allergy beside the dose on the round.
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
ENCOUNTERS = "/api/v1/encounters"
IPD = "/api/v1/ipd"

ASPIRIN = {
    "drug_name": "Aspirin",
    "dose": "300 mg",
    "route": "ORAL",
    "frequency": "OD",
    "times_per_day": 1,
    "dose_times": ["08:00"],
}


@pytest.fixture
async def doctor_token(api: AsyncClient, session: AsyncSession, hospital: Hospital) -> str:
    user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
    return await login(api, user)


@pytest.fixture
async def nurse_token(api: AsyncClient, session: AsyncSession, hospital: Hospital) -> str:
    user = await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)
    return await login(api, user)


@pytest.fixture
async def admin_token(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def stay(api: AsyncClient, admin_token: str) -> dict[str, str]:
    """A patient in a bed: ward, bed, registration, visit, admission."""
    ward = await api.post(
        f"{IPD}/wards",
        json={"code": "MW1", "name": "Male Medical", "bed_class": "GENERAL"},
        headers=auth(admin_token),
    )
    bed = await api.post(
        f"{IPD}/beds",
        json={"ward_id": ward.json()["id"], "code": "MW1-01", "tariff_item_code": "ROOM-GEN"},
        headers=auth(admin_token),
    )
    patient = await api.post(
        PATIENTS,
        json={
            "full_name": "Ramesh Yadav",
            "phone": "9812345670",
            "gender": "MALE",
            "age_years": 58,
        },
        headers=auth(admin_token),
    )
    encounter = await api.post(
        ENCOUNTERS, json={"patient_id": patient.json()["id"]}, headers=auth(admin_token)
    )
    admission = await api.post(
        f"{IPD}/admissions",
        json={"encounter_id": encounter.json()["id"], "bed_id": bed.json()["id"]},
        headers=auth(admin_token),
    )
    assert admission.status_code == 201, admission.text
    return {
        "patient": patient.json()["id"],
        "encounter": encounter.json()["id"],
        "admission": admission.json()["id"],
    }


async def _allergy(api: AsyncClient, token: str, patient_id: str, label: str) -> None:
    response = await api.post(
        f"{PATIENTS}/{patient_id}/alerts",
        json={"alert_type": "DRUG_ALLERGY", "label": label, "severity": "CRITICAL"},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text


async def test_the_audits_case_is_refused_with_the_allergy_named(
    api: AsyncClient, doctor_token: str, stay: dict[str, str]
) -> None:
    await _allergy(api, doctor_token, stay["patient"], "Aspirin — anaphylaxis")

    response = await api.post(
        f"{IPD}/admissions/{stay['admission']}/medications",
        json=ASPIRIN,
        headers=auth(doctor_token),
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "allergy_conflict"
    assert [row["label"] for row in error["details"]["allergies"]] == ["Aspirin — anaphylaxis"]


async def test_an_override_is_kept_on_the_chart_and_audited(
    api: AsyncClient, doctor_token: str, admin_token: str, stay: dict[str, str]
) -> None:
    await _allergy(api, doctor_token, stay["patient"], "Aspirin — anaphylaxis")
    reason = "Intolerance only; cardiology accepts the risk."

    response = await api.post(
        f"{IPD}/admissions/{stay['admission']}/medications",
        json={**ASPIRIN, "allergy_override_reason": reason},
        headers=auth(doctor_token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["allergy_override_reason"] == reason
    assert response.json()["allergy_conflicts"] == ["Aspirin — anaphylaxis"]

    logs = await api.get(
        "/api/v1/audit-logs",
        params={"action": "medication.allergy_override"},
        headers=auth(admin_token),
    )
    [entry] = logs.json()["items"]
    assert entry["changes"]["drug"] == "Aspirin"
    assert entry["changes"]["allergies"] == ["Aspirin — anaphylaxis"]
    assert entry["changes"]["reason"] == reason


async def test_the_drug_round_shows_the_allergy_beside_the_dose(
    api: AsyncClient, doctor_token: str, nurse_token: str, stay: dict[str, str]
) -> None:
    """Charted first, the allergy reported after: the round says so, and the
    dose cannot be signed as given until the drug is reviewed."""
    charted = await api.post(
        f"{IPD}/admissions/{stay['admission']}/medications",
        json=ASPIRIN,
        headers=auth(doctor_token),
    )
    assert charted.status_code == 201, charted.text
    await _allergy(api, doctor_token, stay["patient"], "Aspirin — anaphylaxis")

    round_ = await api.get(f"{IPD}/doses", params={"status": "DUE"}, headers=auth(nurse_token))
    doses = round_.json()["items"]
    assert doses, "the chart materialises the doses due"
    for row in doses:
        assert row["patient_allergies"] == ["Aspirin — anaphylaxis"]
        assert row["allergy_conflicts"] == ["Aspirin — anaphylaxis"]
        assert row["allergy_unreviewed"] is True
    dose = doses[0]

    given = await api.post(
        f"{IPD}/doses/{dose['id']}", json={"status": "GIVEN"}, headers=auth(nurse_token)
    )
    assert given.status_code == 409, given.text
    assert given.json()["error"]["code"] == "allergy_conflict"

    chart = await api.get(
        f"{IPD}/admissions/{stay['admission']}/medications", headers=auth(nurse_token)
    )
    assert chart.json()["items"][0]["allergy_conflicts"] == ["Aspirin — anaphylaxis"]


async def test_a_dose_of_an_unrelated_drug_shows_the_allergy_but_no_clash(
    api: AsyncClient, doctor_token: str, nurse_token: str, stay: dict[str, str]
) -> None:
    await _allergy(api, doctor_token, stay["patient"], "Aspirin — anaphylaxis")
    await api.post(
        f"{IPD}/admissions/{stay['admission']}/medications",
        json={**ASPIRIN, "drug_name": "Paracetamol", "dose": "650 mg"},
        headers=auth(doctor_token),
    )

    doses = (
        await api.get(f"{IPD}/doses", params={"status": "DUE"}, headers=auth(nurse_token))
    ).json()["items"]
    assert doses
    for dose in doses:
        assert dose["patient_allergies"] == ["Aspirin — anaphylaxis"]
        assert dose["allergy_conflicts"] == []
        assert dose["allergy_unreviewed"] is False


async def test_an_opd_drug_override_is_audited(
    api: AsyncClient, doctor_token: str, admin_token: str, stay: dict[str, str]
) -> None:
    await _allergy(api, doctor_token, stay["patient"], "Aspirin — anaphylaxis")
    order = {"order_type": "PHARMACY", "item_name": "Ecosprin 75"}

    refused = await api.post(
        f"{ENCOUNTERS}/{stay['encounter']}/orders", json=order, headers=auth(doctor_token)
    )
    assert refused.status_code == 409, refused.text

    placed = await api.post(
        f"{ENCOUNTERS}/{stay['encounter']}/orders",
        json={**order, "allergy_override_reason": "Post-stent; cardiology accepts the risk."},
        headers=auth(doctor_token),
    )
    assert placed.status_code == 201, placed.text
    logs = await api.get(
        "/api/v1/audit-logs",
        params={"action": "order.allergy_override"},
        headers=auth(admin_token),
    )
    assert logs.json()["total"] == 1
