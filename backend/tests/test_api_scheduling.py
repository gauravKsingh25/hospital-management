"""Scheduling over HTTP: the OPD counter, the doctor's screen, and RBAC."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.scheduling.models import Weekday
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
APPOINTMENTS = "/api/v1/appointments"
QUEUE = "/api/v1/queue"
DEPARTMENTS = "/api/v1/departments"


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
        json={"user_id": str(doctor_user.id), "specialty": "General Medicine"},
        headers=auth(admin_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture
async def patient_id(api: AsyncClient, reception_token: str) -> str:
    response = await api.post(
        PATIENTS,
        json={
            "full_name": "Sunita Devi",
            "phone": "9876543210",
            "gender": "FEMALE",
            "age_years": 34,
        },
        headers=auth(reception_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


class TestDepartments:
    async def test_an_admin_creates_a_department(self, api: AsyncClient, admin_token: str) -> None:
        response = await api.post(
            DEPARTMENTS,
            json={"code": "CARD", "name": "Cardiology", "location": "2nd floor, Block B"},
            headers=auth(admin_token),
        )
        assert response.status_code == 201, response.text
        assert response.json()["code"] == "CARD"

    async def test_a_receptionist_cannot_create_a_department(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        response = await api.post(
            DEPARTMENTS,
            json={"code": "ORTH", "name": "Orthopaedics"},
            headers=auth(reception_token),
        )
        assert response.status_code == 403

    async def test_a_receptionist_can_read_departments(
        self, api: AsyncClient, admin_token: str, reception_token: str
    ) -> None:
        await api.post(
            DEPARTMENTS, json={"code": "GEN", "name": "General Medicine"}, headers=auth(admin_token)
        )
        response = await api.get(DEPARTMENTS, headers=auth(reception_token))
        assert response.status_code == 200
        assert response.json()["total"] == 1


class TestDoctorsAndAvailability:
    async def test_a_receptionist_cannot_create_a_doctor_profile(
        self, api: AsyncClient, reception_token: str, doctor_user: User
    ) -> None:
        response = await api.post(
            DOCTORS, json={"user_id": str(doctor_user.id)}, headers=auth(reception_token)
        )
        assert response.status_code == 403

    async def test_a_doctor_sets_their_own_clinic_hours(
        self, api: AsyncClient, doctor_user: User, doctor_id: str
    ) -> None:
        """CLAUDE.md §7: a doctor's own timetable should not need an administrator."""
        token = await login(api, doctor_user)
        response = await api.post(
            f"{DOCTORS}/{doctor_id}/availability",
            json={
                "weekday": Weekday.MONDAY.value,
                "start_time": "10:00:00",
                "end_time": "13:00:00",
                "slot_minutes": 15,
            },
            headers=auth(token),
        )
        assert response.status_code == 201, response.text

    async def test_slots_are_derived_from_the_timetable(
        self, api: AsyncClient, admin_token: str, doctor_id: str
    ) -> None:
        await api.post(
            f"{DOCTORS}/{doctor_id}/availability",
            json={
                "weekday": Weekday.MONDAY.value,
                "start_time": "10:00:00",
                "end_time": "12:00:00",
                "slot_minutes": 30,
            },
            headers=auth(admin_token),
        )
        today = utc_now().date()
        monday = today + timedelta(days=(0 - today.weekday()) % 7 or 7)

        response = await api.get(
            f"{DOCTORS}/{doctor_id}/slots",
            params={"date": monday.isoformat()},
            headers=auth(admin_token),
        )
        assert response.status_code == 200
        assert len(response.json()) == 4

    async def test_a_receptionist_cannot_set_a_doctors_hours(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        response = await api.post(
            f"{DOCTORS}/{doctor_id}/availability",
            json={
                "weekday": Weekday.MONDAY.value,
                "start_time": "10:00:00",
                "end_time": "13:00:00",
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 403


class TestQuickOpd:
    async def test_one_call_books_checks_in_and_issues_a_token(
        self, api: AsyncClient, reception_token: str, doctor_id: str, patient_id: str
    ) -> None:
        """CLAUDE.md §7b: the most repeated action of the day is one request."""
        response = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id, "reason": "Fever"},
            headers=auth(reception_token),
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["token_number"] == 1
        assert body["appointment"]["status"] == "CHECKED_IN"
        assert body["queue_entry"]["status"] == "WAITING"
        # Everything the printed token slip needs, in one response.
        assert body["doctor_name"]
        assert body["patients_ahead"] == 0

    async def test_the_slip_says_who_the_patient_is(
        self, api: AsyncClient, reception_token: str, doctor_id: str, patient_id: str
    ) -> None:
        """A slip that cannot identify its holder is a slip nobody can act on.

        It carried a token number, a doctor and a room — and nothing naming the
        person walking away with it. `uhid` is also what the QR on the slip
        encodes (CLAUDE.md §7b), so this is the field the scanned-card lookup
        is printed from.
        """
        response = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )

        body = response.json()
        assert body["patient_name"]
        assert body["uhid"]
        # The shape a scanner will send back, and what the QR must contain.
        assert body["uhid"] == body["uhid"].upper()

    async def test_the_token_slip_carries_the_department_location(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_user: User,
        patient_id: str,
    ) -> None:
        department = (
            await api.post(
                DEPARTMENTS,
                json={"code": "CARD", "name": "Cardiology", "location": "2nd floor, Block B"},
                headers=auth(admin_token),
            )
        ).json()
        doctor = (
            await api.post(
                DOCTORS,
                json={"user_id": str(doctor_user.id), "department_id": department["id"]},
                headers=auth(admin_token),
            )
        ).json()

        body = (
            await api.post(
                f"{QUEUE}/quick-opd",
                json={"patient_id": patient_id, "doctor_id": doctor["id"]},
                headers=auth(reception_token),
            )
        ).json()

        assert body["department_name"] == "Cardiology"
        assert body["location"] == "2nd floor, Block B"

    async def test_a_doctor_cannot_run_the_registration_counter(
        self, api: AsyncClient, doctor_user: User, doctor_id: str, patient_id: str
    ) -> None:
        token = await login(api, doctor_user)
        response = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(token),
        )
        assert response.status_code == 403


class TestTheDoctorsScreen:
    async def test_a_doctor_sees_their_own_queue_without_knowing_their_id(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_user: User,
        doctor_id: str,
        patient_id: str,
    ) -> None:
        await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )

        token = await login(api, doctor_user)
        response = await api.get(f"{QUEUE}/mine", headers=auth(token))

        assert response.status_code == 200
        assert len(response.json()) == 1

    async def test_call_next_then_start_then_complete(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_user: User,
        doctor_id: str,
        patient_id: str,
    ) -> None:
        await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )
        token = await login(api, doctor_user)

        called = await api.post(f"{QUEUE}/call-next", headers=auth(token))
        assert called.status_code == 200
        entry_id = called.json()["id"]
        assert called.json()["status"] == "CALLED"

        started = await api.post(f"{QUEUE}/{entry_id}/start", headers=auth(token))
        assert started.json()["status"] == "IN_CONSULTATION"

        done = await api.post(f"{QUEUE}/{entry_id}/complete", headers=auth(token))
        assert done.json()["status"] == "COMPLETED"

    async def test_calling_an_empty_queue_returns_null(
        self, api: AsyncClient, doctor_user: User, doctor_id: str
    ) -> None:
        token = await login(api, doctor_user)
        response = await api.post(f"{QUEUE}/call-next", headers=auth(token))
        assert response.status_code == 200
        assert response.json() is None

    async def test_an_illegal_transition_is_a_409_that_explains_itself(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_user: User,
        doctor_id: str,
        patient_id: str,
    ) -> None:
        body = (
            await api.post(
                f"{QUEUE}/quick-opd",
                json={"patient_id": patient_id, "doctor_id": doctor_id},
                headers=auth(reception_token),
            )
        ).json()
        entry_id = body["queue_entry"]["id"]
        token = await login(api, doctor_user)

        # Completing a token nobody has started is not a legal move.
        response = await api.post(f"{QUEUE}/{entry_id}/complete", headers=auth(token))

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "illegal_state_transition"
        assert error["details"]["allowed"]


class TestAppointments:
    async def test_booking_outside_clinic_hours_explains_the_override(
        self, api: AsyncClient, reception_token: str, doctor_id: str, patient_id: str
    ) -> None:
        response = await api.post(
            APPOINTMENTS,
            json={
                "patient_id": patient_id,
                "doctor_id": doctor_id,
                "scheduled_start": (utc_now() + timedelta(days=2)).isoformat(),
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "outside_availability"

    async def test_cancelling_records_the_reason(
        self, api: AsyncClient, reception_token: str, doctor_id: str, patient_id: str
    ) -> None:
        appointment = (
            await api.post(
                APPOINTMENTS,
                json={
                    "patient_id": patient_id,
                    "doctor_id": doctor_id,
                    "scheduled_start": (utc_now() + timedelta(days=2)).isoformat(),
                    "override_availability": True,
                },
                headers=auth(reception_token),
            )
        ).json()

        response = await api.post(
            f"{APPOINTMENTS}/{appointment['id']}/cancel",
            json={"reason": "Patient rang to cancel", "cancelled_by_patient": True},
            headers=auth(reception_token),
        )

        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        assert response.json()["cancelled_by_patient"] is True

    async def test_appointments_are_paginated(
        self, api: AsyncClient, reception_token: str, doctor_id: str, patient_id: str
    ) -> None:
        base = utc_now() + timedelta(days=3)
        for index in range(3):
            await api.post(
                APPOINTMENTS,
                json={
                    "patient_id": patient_id,
                    "doctor_id": doctor_id,
                    "scheduled_start": (base + timedelta(hours=index)).isoformat(),
                    "override_availability": True,
                },
                headers=auth(reception_token),
            )

        body = (await api.get(f"{APPOINTMENTS}?limit=2", headers=auth(reception_token))).json()
        assert set(body) == {"items", "total", "limit", "offset", "has_more"}
        assert len(body["items"]) == 2
        # Three appointments, two returned: there is a further page, and the
        # envelope says so rather than making the client work it out.
        assert body["has_more"] is True
        assert body["total"] == 3


class TestAuditing:
    async def test_quick_opd_is_audited(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        receptionist: User,
        doctor_id: str,
        patient_id: str,
    ) -> None:
        await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )

        entries = (
            await api.get(
                "/api/v1/audit-logs",
                params={"action": "queue.quick_opd"},
                headers=auth(admin_token),
            )
        ).json()

        assert entries["total"] == 1
        assert entries["items"][0]["actor_email"] == receptionist.email
        assert entries["items"][0]["changes"]["token"] == 1
