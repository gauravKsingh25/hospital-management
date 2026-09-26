"""The queue board carries enough about the patient to be a worklist.

`QueueEntryRead` holds identifiers, which is right for the queue's own logic
and useless on a screen: a doctor's list showing `a3f9c2e1…` instead of a name
is not a list anybody can work from, and CLAUDE.md §7b's targets are about
seconds saved per patient.

The alternative the frontend would otherwise be forced into is one request per
row, on the screen staff refresh most often. So the router joins queue rows to
patient identity once, through `patients.service` rather than a query across
the module boundary (CLAUDE.md §2). These tests pin both halves of that: that
the identity actually arrives, and that it costs one lookup rather than one
per row.
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


async def _register(api: AsyncClient, token: str, *, name: str, phone: str, age: int) -> str:
    response = await api.post(
        PATIENTS,
        json={"full_name": name, "phone": phone, "gender": "FEMALE", "age_years": age},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _queue_patient(api: AsyncClient, token: str, *, patient_id: str, doctor_id: str) -> None:
    response = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": patient_id, "doctor_id": doctor_id},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text


class TestTheBoardIsUsable:
    async def test_a_queue_row_carries_the_patient_identity(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        patient_id = await _register(
            api, reception_token, name="Sunita Devi", phone="9876543210", age=34
        )
        await _queue_patient(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        response = await api.get(QUEUE, headers=auth(reception_token))
        assert response.status_code == 200, response.text

        [row] = response.json()
        assert row["patient_name"] == "Sunita Devi"
        assert row["patient_uhid"].startswith("UH") or row["patient_uhid"]
        assert row["patient_age_years"] == 34
        assert row["patient_gender"] == "FEMALE"
        assert row["patient_is_deceased"] is False

    async def test_a_queue_row_links_straight_to_the_open_chart(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        """One click from the worklist to the consultation screen.

        Without `encounter_id` on the row the frontend has to resolve the
        appointment to an encounter first, which puts a round trip between a
        doctor clicking a name and anything appearing.
        """
        patient_id = await _register(
            api, reception_token, name="Kavita Sharma", phone="9811122233", age=45
        )
        opd = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )
        assert opd.status_code == 201, opd.text
        expected = opd.json()["encounter_id"]

        [row] = (await api.get(QUEUE, headers=auth(reception_token))).json()
        assert row["encounter_id"] == expected
        assert row["encounter_number"] == opd.json()["encounter_number"]

    async def test_the_doctors_own_queue_carries_it_too(
        self, api: AsyncClient, reception_token: str, doctor_user: User, doctor_id: str
    ) -> None:
        """`/queue/mine` is the screen a doctor actually opens."""
        patient_id = await _register(
            api, reception_token, name="Ramesh Yadav", phone="9812345670", age=58
        )
        await _queue_patient(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        doctor_token = await login(api, doctor_user)
        response = await api.get(f"{QUEUE}/mine", headers=auth(doctor_token))
        assert response.status_code == 200, response.text

        [row] = response.json()
        assert row["patient_name"] == "Ramesh Yadav"
        assert row["patient_age_years"] == 58

    async def test_identity_costs_one_lookup_regardless_of_queue_length(
        self, api: AsyncClient, reception_token: str, doctor_id: str, session: AsyncSession
    ) -> None:
        """The point of the bulk fetch: a longer queue is not a slower screen.

        Asserted by counting SELECTs against the patients table while the
        board is built. Without the bulk lookup this grows with the queue, and
        the regression would show up in production as "the queue screen got
        slow" long after the change that caused it.
        """
        for index in range(4):
            patient_id = await _register(
                api,
                reception_token,
                name=f"Patient Number{index}",
                phone=f"98100000{index:02d}",
                age=30 + index,
            )
            await _queue_patient(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        statements: list[str] = []

        from sqlalchemy import event

        from app.core.database import get_engine

        engine = get_engine().sync_engine

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if "FROM patients" in statement:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await api.get(QUEUE, headers=auth(reception_token))
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        assert len(response.json()) == 4

        # One SELECT for all four patients — not four.
        assert len(statements) == 1, f"expected a single patients query, got {len(statements)}"


@pytest.fixture
async def second_doctor_id(
    api: AsyncClient, admin_token: str, session: AsyncSession, hospital: Hospital
) -> str:
    user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
    response = await api.post(
        DOCTORS,
        json={"user_id": str(user.id), "specialty": "Paediatrics"},
        headers=auth(admin_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


class TestReceptionSeesOneQueuePerDoctor:
    """CLAUDE.md §7b: reception balances load, so it needs load per doctor."""

    async def test_every_doctor_has_a_column_even_with_nobody_waiting(
        self, api: AsyncClient, reception_token: str, doctor_id: str, second_doctor_id: str
    ) -> None:
        patient_id = await _register(
            api, reception_token, name="Sunita Devi", phone="9876543210", age=34
        )
        await _queue_patient(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        response = await api.get(f"{QUEUE}/board", headers=auth(reception_token))
        assert response.status_code == 200, response.text

        board = {column["doctor_id"]: column for column in response.json()}
        assert set(board) == {doctor_id, second_doctor_id}

        busy = board[doctor_id]
        assert busy["waiting"] == 1
        assert busy["in_consultation"] == 0
        assert busy["longest_wait_minutes"] == 0
        [row] = busy["entries"]
        assert row["patient_name"] == "Sunita Devi"

        # The empty column is the point: it is where the next walk-in goes.
        idle = board[second_doctor_id]
        assert idle["waiting"] == 0
        assert idle["entries"] == []
        assert idle["longest_wait_minutes"] is None

    async def test_the_busiest_doctor_comes_first(
        self, api: AsyncClient, reception_token: str, doctor_id: str, second_doctor_id: str
    ) -> None:
        for index in range(2):
            patient_id = await _register(
                api,
                reception_token,
                name=f"Waiting Person{index}",
                phone=f"98111000{index:02d}",
                age=30,
            )
            await _queue_patient(
                api, reception_token, patient_id=patient_id, doctor_id=second_doctor_id
            )

        columns = (await api.get(f"{QUEUE}/board", headers=auth(reception_token))).json()
        assert [column["doctor_id"] for column in columns] == [second_doctor_id, doctor_id]


class TestReceptionCanMoveAPatient:
    async def test_the_token_the_booking_and_the_chart_all_move(
        self, api: AsyncClient, reception_token: str, doctor_id: str, second_doctor_id: str
    ) -> None:
        patient_id = await _register(
            api, reception_token, name="Kavita Sharma", phone="9811122233", age=45
        )
        opd = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )
        assert opd.status_code == 201, opd.text
        entry_id = opd.json()["queue_entry"]["id"]
        appointment_id = opd.json()["appointment"]["id"]
        encounter_id = opd.json()["encounter_id"]

        response = await api.post(
            f"{QUEUE}/{entry_id}/reassign",
            json={"doctor_id": second_doctor_id, "reason": "Long wait"},
            headers=auth(reception_token),
        )
        assert response.status_code == 200, response.text
        moved = response.json()
        assert moved["doctor_id"] == second_doctor_id
        assert moved["token_number"] == 1
        assert moved["status"] == "WAITING"

        appointment = await api.get(
            f"/api/v1/appointments/{appointment_id}", headers=auth(reception_token)
        )
        assert appointment.json()["doctor_id"] == second_doctor_id

        encounter = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(reception_token))
        assert encounter.status_code == 200, encounter.text
        assert encounter.json()["doctor_id"] == second_doctor_id

        board = {
            column["doctor_id"]: column
            for column in (await api.get(f"{QUEUE}/board", headers=auth(reception_token))).json()
        }
        assert board[doctor_id]["entries"] == []
        assert [row["id"] for row in board[second_doctor_id]["entries"]] == [entry_id]

    async def test_the_new_doctor_sees_them_and_the_old_one_does_not(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_user: User,
        doctor_id: str,
        second_doctor_id: str,
    ) -> None:
        patient_id = await _register(
            api, reception_token, name="Ramesh Yadav", phone="9812345670", age=58
        )
        opd = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": second_doctor_id},
            headers=auth(reception_token),
        )
        entry_id = opd.json()["queue_entry"]["id"]

        doctor_token = await login(api, doctor_user)
        assert (await api.get(f"{QUEUE}/mine", headers=auth(doctor_token))).json() == []

        await api.post(
            f"{QUEUE}/{entry_id}/reassign",
            json={"doctor_id": doctor_id},
            headers=auth(reception_token),
        )

        [row] = (await api.get(f"{QUEUE}/mine", headers=auth(doctor_token))).json()
        assert row["patient_name"] == "Ramesh Yadav"

    async def test_moving_is_refused_once_the_consultation_has_started(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_user: User,
        doctor_id: str,
        second_doctor_id: str,
    ) -> None:
        """The chart is checked first, so no token is burned on a refused move."""
        patient_id = await _register(
            api, reception_token, name="Anita Kumari", phone="9876500011", age=41
        )
        opd = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )
        entry_id = opd.json()["queue_entry"]["id"]
        encounter_id = opd.json()["encounter_id"]

        doctor_token = await login(api, doctor_user)
        started = await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        assert started.status_code == 200, started.text

        response = await api.post(
            f"{QUEUE}/{entry_id}/reassign",
            json={"doctor_id": second_doctor_id},
            headers=auth(reception_token),
        )
        assert response.status_code == 409, response.text

        [row] = (await api.get(QUEUE, headers=auth(reception_token))).json()
        assert row["doctor_id"] == doctor_id
        assert row["token_number"] == 1


class TestReceptionCanReorderALine:
    async def test_dragging_a_token_to_the_top_is_what_the_board_then_shows(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        ids: list[str] = []
        for index, name in enumerate(("First Arrival", "Second Arrival", "Third Arrival")):
            patient_id = await _register(
                api, reception_token, name=name, phone=f"98123000{index:02d}", age=30
            )
            opd = await api.post(
                f"{QUEUE}/quick-opd",
                json={"patient_id": patient_id, "doctor_id": doctor_id},
                headers=auth(reception_token),
            )
            assert opd.status_code == 201, opd.text
            ids.append(opd.json()["queue_entry"]["id"])

        response = await api.post(
            f"{QUEUE}/{ids[2]}/reorder",
            json={"after_entry_id": None},
            headers=auth(reception_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["position"] == 1

        board = (await api.get(QUEUE, headers=auth(reception_token))).json()
        assert [row["id"] for row in board] == [ids[2], ids[0], ids[1]]
        assert [row["position"] for row in board] == [1, 2, 3]

        # The per-doctor board and the summary's "next token" agree with it.
        [column] = (await api.get(f"{QUEUE}/board", headers=auth(reception_token))).json()
        assert [row["id"] for row in column["entries"]] == [ids[2], ids[0], ids[1]]
        summary = await api.get(
            f"{QUEUE}/summary", params={"doctor_id": doctor_id}, headers=auth(reception_token)
        )
        assert summary.json()["next_token"] == board[0]["token_number"]

    async def test_placing_after_a_token_in_another_line_is_refused(
        self, api: AsyncClient, reception_token: str, doctor_id: str, second_doctor_id: str
    ) -> None:
        mine = await _register(api, reception_token, name="Mine", phone="9812400001", age=30)
        theirs = await _register(api, reception_token, name="Theirs", phone="9812400002", age=30)
        a = (
            await api.post(
                f"{QUEUE}/quick-opd",
                json={"patient_id": mine, "doctor_id": doctor_id},
                headers=auth(reception_token),
            )
        ).json()["queue_entry"]["id"]
        b = (
            await api.post(
                f"{QUEUE}/quick-opd",
                json={"patient_id": theirs, "doctor_id": second_doctor_id},
                headers=auth(reception_token),
            )
        ).json()["queue_entry"]["id"]

        response = await api.post(
            f"{QUEUE}/{a}/reorder", json={"after_entry_id": b}, headers=auth(reception_token)
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "queue_anchor_not_found"


class TestReceptionMarksAPatientSeen:
    async def _queue(
        self, api: AsyncClient, token: str, *, doctor_id: str, name: str, phone: str
    ) -> dict[str, str]:
        patient_id = await _register(api, token, name=name, phone=phone, age=40)
        opd = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(token),
        )
        assert opd.status_code == 201, opd.text
        return {
            "entry_id": opd.json()["queue_entry"]["id"],
            "encounter_id": opd.json()["encounter_id"],
        }

    async def test_the_patient_moves_to_the_seen_list(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        seen = await self._queue(
            api, reception_token, doctor_id=doctor_id, name="Seen Person", phone="9812500001"
        )
        waiting = await self._queue(
            api, reception_token, doctor_id=doctor_id, name="Still Waiting", phone="9812500002"
        )

        response = await api.post(f"{QUEUE}/{seen['entry_id']}/seen", headers=auth(reception_token))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "COMPLETED"

        [column] = (await api.get(f"{QUEUE}/board", headers=auth(reception_token))).json()
        assert [row["id"] for row in column["entries"]] == [waiting["entry_id"]]
        assert [row["id"] for row in column["seen"]] == [seen["entry_id"]]
        assert column["seen"][0]["patient_name"] == "Seen Person"
        assert column["waiting"] == 1
        assert column["completed"] == 1

        # The nurse's flat list and the doctor's own list drop them too.
        flat = (await api.get(QUEUE, headers=auth(reception_token))).json()
        assert [row["id"] for row in flat] == [waiting["entry_id"]]

    async def test_the_chart_records_who_reported_it(
        self, api: AsyncClient, reception_token: str, doctor_id: str, receptionist: User
    ) -> None:
        """Through the state machine, with reception as the actor — never a
        status written directly, and never passed off as the doctor."""
        queued = await self._queue(
            api, reception_token, doctor_id=doctor_id, name="Chart Person", phone="9812500011"
        )
        await api.post(f"{QUEUE}/{queued['entry_id']}/seen", headers=auth(reception_token))

        encounter = await api.get(
            f"{ENCOUNTERS}/{queued['encounter_id']}", headers=auth(reception_token)
        )
        assert encounter.json()["status"] == "IN_CONSULTATION"

        timeline = await api.get(
            f"{ENCOUNTERS}/{queued['encounter_id']}/timeline", headers=auth(reception_token)
        )
        assert timeline.status_code == 200, timeline.text
        [event] = [row for row in timeline.json() if row["to_status"] == "IN_CONSULTATION"]
        assert event["actor_id"] == str(receptionist.id)
        assert event["reason"] == "Marked as seen by reception."

    async def test_the_night_auto_close_does_not_call_it_a_no_show(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_id: str,
        session: AsyncSession,
        hospital: Hospital,
    ) -> None:
        """The reason the chart moves at all: a seen patient left REGISTERED
        would be closed as NO_SHOW by the end-of-day safety net."""
        from datetime import timedelta

        from app.core import database as db
        from app.modules.clinical import service as clinical_service

        queued = await self._queue(
            api, reception_token, doctor_id=doctor_id, name="Night Person", phone="9812500021"
        )
        await api.post(f"{QUEUE}/{queued['entry_id']}/seen", headers=auth(reception_token))

        await db.set_tenant_context(session, hospital.id)
        result = await clinical_service.close_stale_encounters(
            session, hospital_id=hospital.id, older_than=timedelta(0)
        )
        await session.commit()
        assert result["no_show"] == 0

        encounter = await api.get(
            f"{ENCOUNTERS}/{queued['encounter_id']}", headers=auth(reception_token)
        )
        assert encounter.json()["status"] == "COMPLETED"

    async def test_the_doctor_can_still_document_and_complete_the_visit(
        self, api: AsyncClient, reception_token: str, doctor_user: User, doctor_id: str
    ) -> None:
        queued = await self._queue(
            api, reception_token, doctor_id=doctor_id, name="Late Notes", phone="9812500031"
        )
        await api.post(f"{QUEUE}/{queued['entry_id']}/seen", headers=auth(reception_token))

        doctor_token = await login(api, doctor_user)
        done = await api.post(
            f"{ENCOUNTERS}/{queued['encounter_id']}/complete",
            json={},
            headers=auth(doctor_token),
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "COMPLETED"

        # The seen row still names the visit once it has closed — reception
        # sends the patient for admission from that row.
        [column] = (await api.get(f"{QUEUE}/board", headers=auth(reception_token))).json()
        [row] = [row for row in column["seen"] if row["id"] == queued["entry_id"]]
        assert row["encounter_id"] == queued["encounter_id"]

    async def test_marking_seen_twice_is_a_conflict(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        queued = await self._queue(
            api, reception_token, doctor_id=doctor_id, name="Twice Person", phone="9812500041"
        )
        first = await api.post(f"{QUEUE}/{queued['entry_id']}/seen", headers=auth(reception_token))
        assert first.status_code == 200
        again = await api.post(f"{QUEUE}/{queued['entry_id']}/seen", headers=auth(reception_token))
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "queue_entry_not_markable"

    async def test_a_role_without_queue_manage_cannot_mark_seen(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_id: str,
        session: AsyncSession,
        hospital: Hospital,
    ) -> None:
        queued = await self._queue(
            api, reception_token, doctor_id=doctor_id, name="Guarded Person", phone="9812500051"
        )
        cashier = await _make_user(session, role_code=Roles.CASHIER, hospital_id=hospital.id)
        cashier_token = await login(api, cashier)
        response = await api.post(f"{QUEUE}/{queued['entry_id']}/seen", headers=auth(cashier_token))
        assert response.status_code == 403, response.text


class TestComputedFieldsActuallySerialise:
    """Pydantic v2 drops a bare `@property`. Two of them were being dropped.

    `PatientRead.age_years` and `Page.has_more` both read as part of the API
    contract and neither reached a client — the property existed, the schema
    did not mention it, and any consumer that trusted the type got
    `undefined`. Both are `computed_field` now; these tests keep them that way.
    """

    async def test_a_patient_record_reports_an_age(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        patient_id = await _register(
            api, reception_token, name="Anita Kumari", phone="9876500011", age=41
        )

        response = await api.get(f"{PATIENTS}/{patient_id}", headers=auth(reception_token))
        assert response.status_code == 200, response.text

        body = response.json()
        assert "age_years" in body, "age_years is missing from the response entirely"
        # Registered by age rather than birth date, so the backend estimated a
        # birth date and the round trip must land back on the same year.
        assert body["age_years"] == 41

    async def test_a_paginated_response_says_whether_more_follows(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        for index in range(3):
            await _register(
                api,
                reception_token,
                name=f"Paged Patient{index}",
                phone=f"98765111{index:02d}",
                age=25,
            )

        first = await api.get(PATIENTS, params={"limit": 2}, headers=auth(reception_token))
        assert first.status_code == 200, first.text
        assert "has_more" in first.json(), "has_more is missing from the page envelope"
        assert first.json()["has_more"] is True

        rest = await api.get(
            PATIENTS, params={"limit": 200, "offset": 0}, headers=auth(reception_token)
        )
        assert rest.json()["has_more"] is False
