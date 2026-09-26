"""Reporting over HTTP, and the permission split that matters.

The RBAC class is the point of this file. `reporting` deliberately does not have
a single `report:read`, because that would mean handing the ward's queue screen
to a nurse also hands her the hospital's collections. These tests assert the
three-way split by name, from both directions: what each role can reach, and
what it is refused.
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
REPORTS = "/api/v1/reports"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def doctor_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)


@pytest.fixture
async def nurse_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)


@pytest.fixture
async def cashier_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.CASHIER, hospital_id=hospital.id)


@pytest.fixture
async def admin_token(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def reception_token(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def doctor_token(api: AsyncClient, doctor_user: User) -> str:
    return await login(api, doctor_user)


@pytest.fixture
async def nurse_token(api: AsyncClient, nurse_user: User) -> str:
    return await login(api, nurse_user)


@pytest.fixture
async def cashier_token(api: AsyncClient, cashier_user: User) -> str:
    return await login(api, cashier_user)


@pytest.fixture
async def a_visit(api: AsyncClient, reception_token: str) -> dict[str, str]:
    patient = await api.post(
        PATIENTS,
        json={
            "full_name": "Ramesh Yadav",
            "phone": "9812345670",
            "gender": "MALE",
            "age_years": 58,
        },
        headers=auth(reception_token),
    )
    assert patient.status_code in (200, 201), patient.text
    encounter = await api.post(
        ENCOUNTERS,
        json={"patient_id": patient.json()["id"]},
        headers=auth(reception_token),
    )
    assert encounter.status_code in (200, 201), encounter.text
    return {"patient": patient.json()["id"], "encounter": encounter.json()["id"]}


# ---------------------------------------------------------------------------
# The reports themselves
# ---------------------------------------------------------------------------
class TestTheReports:
    async def test_footfall_counts_the_visit(
        self, api: AsyncClient, admin_token: str, a_visit: dict[str, str]
    ) -> None:
        response = await api.get(f"{REPORTS}/footfall", headers=auth(admin_token))
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["total_visits"] == 1
        assert body["new_patients"] == 1
        # Trailing month, zero-filled.
        assert len(body["by_day"]) == 30

    async def test_the_window_defaults_to_the_trailing_month(
        self, api: AsyncClient, admin_token: str, a_visit: dict[str, str]
    ) -> None:
        response = await api.get(f"{REPORTS}/footfall", headers=auth(admin_token))
        window = response.json()["window"]
        assert window["date_from"] < window["date_to"]

    async def test_an_absurd_window_is_refused(self, api: AsyncClient, admin_token: str) -> None:
        """An unbounded report is a full scan somebody triggers by clearing a filter."""
        response = await api.get(
            f"{REPORTS}/footfall",
            params={"date_from": "2000-01-01", "date_to": "2026-08-12"},
            headers=auth(admin_token),
        )
        assert response.status_code == 422, response.text

    async def test_a_backwards_window_is_refused(self, api: AsyncClient, admin_token: str) -> None:
        response = await api.get(
            f"{REPORTS}/footfall",
            params={"date_from": "2026-08-12", "date_to": "2026-08-01"},
            headers=auth(admin_token),
        )
        assert response.status_code == 422, response.text

    async def test_occupancy_reports_an_empty_hospital_honestly(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        response = await api.get(f"{REPORTS}/occupancy", headers=auth(admin_token))
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["total_beds"] == 0
        assert body["occupancy_rate"] == 0.0
        assert body["by_ward"] == []

    async def test_the_queue_endpoint_carries_the_delay_threshold(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        """§7b's doctor delay alert has to say what "late" means."""
        response = await api.get(
            f"{REPORTS}/queue", params={"delay_threshold_minutes": 45}, headers=auth(admin_token)
        )
        assert response.status_code == 200, response.text
        assert response.json()["delay_threshold_minutes"] == 45
        assert response.json()["running_late"] == []

    async def test_revenue_separates_earned_collected_and_outstanding(
        self, api: AsyncClient, admin_token: str, a_visit: dict[str, str]
    ) -> None:
        response = await api.get(f"{REPORTS}/revenue", headers=auth(admin_token))
        assert response.status_code == 200, response.text

        body = response.json()
        for key in ("charges_raised", "collected", "outstanding_total"):
            assert key in body

    async def test_follow_up_reports_nothing_advised_as_unknown_not_zero(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        response = await api.get(f"{REPORTS}/follow-up", headers=auth(admin_token))
        assert response.status_code == 200, response.text
        assert response.json()["advised"] == 0
        assert response.json()["compliance_rate"] is None


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------
class TestTheDashboard:
    async def test_an_admin_gets_every_section(
        self, api: AsyncClient, admin_token: str, a_visit: dict[str, str]
    ) -> None:
        response = await api.get(f"{REPORTS}/dashboard", headers=auth(admin_token))
        assert response.status_code == 200, response.text

        body = response.json()
        for section in ("footfall", "occupancy", "queue", "inpatient", "follow_up", "revenue"):
            assert body[section] is not None, f"{section} missing for an administrator"

    async def test_a_nurse_gets_operations_and_no_money(
        self, api: AsyncClient, nurse_token: str, a_visit: dict[str, str]
    ) -> None:
        """The reason the permission is split three ways rather than one."""
        response = await api.get(f"{REPORTS}/dashboard", headers=auth(nurse_token))
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["footfall"] is not None
        assert body["occupancy"] is not None
        assert body["queue"] is not None
        assert body["revenue"] is None
        assert body["inpatient"] is None

    async def test_a_cashier_gets_money_and_no_clinical_outcomes(
        self, api: AsyncClient, cashier_token: str, a_visit: dict[str, str]
    ) -> None:
        response = await api.get(f"{REPORTS}/dashboard", headers=auth(cashier_token))
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["revenue"] is not None
        assert body["follow_up"] is None
        assert body["inpatient"] is None

    async def test_missing_sections_are_null_rather_than_absent(
        self, api: AsyncClient, nurse_token: str
    ) -> None:
        """So the frontend renders a stable layout instead of reflowing by role."""
        body = (await api.get(f"{REPORTS}/dashboard", headers=auth(nurse_token))).json()
        assert "revenue" in body and body["revenue"] is None


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
class TestWhoMaySeeWhat:
    async def test_a_nurse_cannot_read_revenue(self, api: AsyncClient, nurse_token: str) -> None:
        response = await api.get(f"{REPORTS}/revenue", headers=auth(nurse_token))
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_receptionist_cannot_read_revenue(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        response = await api.get(f"{REPORTS}/revenue", headers=auth(reception_token))
        assert response.status_code == 403, response.text

    async def test_a_cashier_cannot_read_clinical_outcomes(
        self, api: AsyncClient, cashier_token: str
    ) -> None:
        """Money and medicine are different circles."""
        for path in ("inpatient", "follow-up"):
            response = await api.get(f"{REPORTS}/{path}", headers=auth(cashier_token))
            assert response.status_code == 403, f"{path}: {response.text}"

    async def test_a_doctor_reads_clinical_but_not_revenue(
        self, api: AsyncClient, doctor_token: str
    ) -> None:
        allowed = await api.get(f"{REPORTS}/follow-up", headers=auth(doctor_token))
        assert allowed.status_code == 200, allowed.text

        refused = await api.get(f"{REPORTS}/revenue", headers=auth(doctor_token))
        assert refused.status_code == 403, refused.text

    async def test_everyone_operational_can_see_the_queue(
        self, api: AsyncClient, nurse_token: str, reception_token: str, doctor_token: str
    ) -> None:
        for token in (nurse_token, reception_token, doctor_token):
            response = await api.get(f"{REPORTS}/queue", headers=auth(token))
            assert response.status_code == 200, response.text

    async def test_an_anonymous_request_is_refused(self, api: AsyncClient) -> None:
        response = await api.get(f"{REPORTS}/dashboard")
        assert response.status_code == 401, response.text
