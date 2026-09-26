"""Notifications over HTTP: the outbox, templates, suppressions and RBAC.

Two things are worth reading closely.

`TestTheOutbox` walks a real OPD visit and asserts that messages appear without
anyone asking for them — the triggers of CLAUDE.md §13 step 8, seen from the
outside.

`TestSuppressionOverHttp` is the invariant at the API boundary: no route sends a
message to a deceased patient, and no route — held by any role — lifts a death
suppression.
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
NOTIFICATIONS = "/api/v1/notifications"
TEMPLATES = "/api/v1/notifications/templates"
SUPPRESSIONS = "/api/v1/notifications/suppressions"
ENCOUNTERS = "/api/v1/encounters"


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
async def records_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.RECORDS_OFFICER, hospital_id=hospital.id)


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
async def records_token(api: AsyncClient, records_user: User) -> str:
    return await login(api, records_user)


@pytest.fixture
async def patient_id(api: AsyncClient, reception_token: str) -> str:
    """Rapid registration: the four fields CLAUDE.md §7b allows to be required."""
    response = await api.post(
        PATIENTS,
        json={
            "full_name": "Meena Kumari",
            "phone": "9811122233",
            "gender": "FEMALE",
            "age_years": 46,
        },
        headers=auth(reception_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
class TestTemplates:
    async def test_an_admin_can_write_and_preview_a_template(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        """Preview exists because the other way to find a typo in a placeholder
        name is to read it on a patient's phone."""
        created = await api.post(
            TEMPLATES,
            json={
                "code": "REPORT_READY",
                "channel": "SMS",
                "language": "en",
                "category": "REPORT",
                "body": "{{hospital_name}}: {{patient_name}}, collect your {{test_name}}.",
            },
            headers=auth(admin_token),
        )
        assert created.status_code == 201, created.text
        template_id = created.json()["id"]

        preview = await api.post(
            f"{TEMPLATES}/{template_id}/preview",
            json={"context": {"hospital_name": "Sunrise", "patient_name": "Meena"}},
            headers=auth(admin_token),
        )
        assert preview.status_code == 200
        body = preview.json()

        assert body["body"] == "Sunrise: Meena, collect your ."
        assert body["missing"] == ["test_name"]
        assert set(body["placeholders"]) == {"hospital_name", "patient_name", "test_name"}

    async def test_the_code_list_tells_an_author_what_they_can_write(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        response = await api.get(f"{TEMPLATES}/codes", headers=auth(admin_token))

        assert response.status_code == 200
        codes = response.json()
        assert "REPORT_READY" in codes
        assert "patient_name" in codes["REPORT_READY"]

    async def test_an_sms_template_may_not_carry_a_subject(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        response = await api.post(
            TEMPLATES,
            json={"code": "X", "channel": "SMS", "body": "hi", "subject": "Hello"},
            headers=auth(admin_token),
        )
        assert response.status_code == 422

    async def test_template_listing_is_paginated(self, api: AsyncClient, admin_token: str) -> None:
        for index in range(3):
            await api.post(
                TEMPLATES,
                json={
                    "code": f"CODE_{index}",
                    "channel": "SMS",
                    "language": "en",
                    "body": "hello",
                },
                headers=auth(admin_token),
            )

        response = await api.get(f"{TEMPLATES}?limit=2", headers=auth(admin_token))
        page = response.json()

        assert response.status_code == 200
        assert len(page["items"]) == 2
        assert page["total"] == 3
        assert page["limit"] == 2


# ---------------------------------------------------------------------------
# The outbox
# ---------------------------------------------------------------------------
class TestTheOutbox:
    async def test_reception_can_send_a_message_by_hand(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        response = await api.post(
            NOTIFICATIONS,
            json={
                "patient_id": patient_id,
                "body": "Please bring your old X-ray films tomorrow.",
            },
            headers=auth(reception_token),
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "SENT"
        assert body["body"] == "Please bring your old X-ray films tomorrow."
        assert len(body["attempt_log"]) == 1
        assert body["attempt_log"][0]["channel"] == "WHATSAPP"

    async def test_a_visit_produces_messages_nobody_asked_for(
        self, api: AsyncClient, reception_token: str, doctor_token: str, patient_id: str
    ) -> None:
        """The triggers, end to end. Opening and closing a visit is enough — the
        follow-up nudge arrives without any part of the workflow mentioning it."""
        opened = await api.post(
            ENCOUNTERS,
            json={"patient_id": patient_id, "encounter_type": "OPD"},
            headers=auth(reception_token),
        )
        assert opened.status_code == 201, opened.text
        encounter_id = opened.json()["id"]

        await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        completed = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/complete", json={}, headers=auth(doctor_token)
        )
        assert completed.status_code == 200, completed.text

        outbox = await api.get(
            f"{NOTIFICATIONS}?patient_id={patient_id}", headers=auth(reception_token)
        )
        codes = [row["template_code"] for row in outbox.json()["items"]]

        assert "FOLLOW_UP_REMINDER" in codes

    async def test_the_detail_view_shows_every_channel_tried(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        """ "We sent it" is not an answer to a patient who received nothing."""
        created = await api.post(
            NOTIFICATIONS,
            json={"patient_id": patient_id, "body": "Test message."},
            headers=auth(reception_token),
        )
        notification_id = created.json()["id"]

        response = await api.get(
            f"{NOTIFICATIONS}/{notification_id}", headers=auth(reception_token)
        )
        body = response.json()

        assert response.status_code == 200
        assert body["attempt_log"]
        assert body["attempt_log"][0]["gateway"] == "console"

    async def test_the_outbox_is_paginated_and_filterable(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        for index in range(3):
            await api.post(
                NOTIFICATIONS,
                json={"patient_id": patient_id, "body": f"Message {index}."},
                headers=auth(reception_token),
            )

        page = await api.get(f"{NOTIFICATIONS}?limit=2", headers=auth(reception_token))
        filtered = await api.get(
            f"{NOTIFICATIONS}?message_status=SENT", headers=auth(reception_token)
        )

        assert page.json()["total"] == 3
        assert len(page.json()["items"]) == 2
        assert filtered.json()["total"] == 3

    async def test_a_sent_message_cannot_be_cancelled(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        created = await api.post(
            NOTIFICATIONS,
            json={"patient_id": patient_id, "body": "Already gone."},
            headers=auth(reception_token),
        )
        notification_id = created.json()["id"]

        response = await api.post(
            f"{NOTIFICATIONS}/{notification_id}/cancel",
            json={"reason": "Changed my mind."},
            headers=auth(reception_token),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "notification_already_sent"

    async def test_an_outbox_row_names_the_patient(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        """The outbox exists to answer "I never got the message".

        That sentence is said by a named person at a counter, and it cannot be
        answered from a page of UUIDs. Same identity gap as the queue, the
        bench, the counter and the ward round — this is the last of them.
        """
        await api.post(
            NOTIFICATIONS,
            json={"patient_id": patient_id, "body": "Your report is ready."},
            headers=auth(reception_token),
        )

        response = await api.get(NOTIFICATIONS, headers=auth(reception_token))
        row = response.json()["items"][0]

        assert row["patient_name"] == "Meena Kumari"
        assert row["uhid"], "an outbox row without a UHID cannot be matched to a record"

    async def test_the_outbox_costs_one_patient_query_per_page(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        """Bulk, not per row. A counter list that slows down as the day gets
        busier is one staff stop opening."""
        for index in range(4):
            await api.post(
                NOTIFICATIONS,
                json={"patient_id": patient_id, "body": f"Message {index}."},
                headers=auth(reception_token),
            )

        statements: list[str] = []

        from sqlalchemy import event

        from app.core.database import get_engine

        engine = get_engine().sync_engine

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if "FROM patients" in statement:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await api.get(NOTIFICATIONS, headers=auth(reception_token))
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 4
        assert len(statements) == 1, f"expected a single patients query, got {len(statements)}"

    async def test_a_message_for_another_hospitals_patient_is_not_found(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        """Tenant isolation reported as 404, not 403: confirming a record exists
        in another tenant is itself a cross-tenant leak."""
        import uuid

        response = await api.get(f"{NOTIFICATIONS}/{uuid.uuid4()}", headers=auth(reception_token))

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Suppression at the boundary (CLAUDE.md §14)
# ---------------------------------------------------------------------------
class TestSuppressionOverHttp:
    async def test_an_opt_out_recorded_at_the_counter_stops_messages(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        blocked = await api.post(
            SUPPRESSIONS,
            json={
                "patient_id": patient_id,
                "reason": "OPTED_OUT",
                "note": "Asked at the counter.",
            },
            headers=auth(reception_token),
        )
        assert blocked.status_code == 201, blocked.text

        message = await api.post(
            NOTIFICATIONS,
            json={"patient_id": patient_id, "body": "Should not go out."},
            headers=auth(reception_token),
        )

        assert message.status_code == 201
        assert message.json()["status"] == "SUPPRESSED"
        assert message.json()["suppression_reason"] == "OPTED_OUT"
        assert message.json()["attempt_log"] == []

    async def test_the_blocked_list_says_who_is_blocked(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        """The endpoint's own summary is "who is blocked from messaging".

        A `patient_id` does not answer *who*, and this is the list somebody
        checks before wondering why a patient heard nothing.
        """
        created = await api.post(
            SUPPRESSIONS,
            json={"patient_id": patient_id, "reason": "OPTED_OUT"},
            headers=auth(reception_token),
        )
        assert created.json()["patient_name"] == "Meena Kumari"

        listed = await api.get(SUPPRESSIONS, headers=auth(reception_token))
        row = listed.json()["items"][0]

        assert row["patient_name"] == "Meena Kumari"
        assert row["uhid"]

    async def test_a_death_suppression_cannot_be_typed_in(
        self, api: AsyncClient, records_token: str, patient_id: str
    ) -> None:
        """One way in, and it is the clinical death entry."""
        response = await api.post(
            SUPPRESSIONS,
            json={"patient_id": patient_id, "reason": "DECEASED"},
            headers=auth(records_token),
        )

        assert response.status_code == 422

    async def test_no_role_can_lift_a_death_suppression(
        self,
        api: AsyncClient,
        admin_token: str,
        records_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_user: User,
        patient_id: str,
    ) -> None:
        """The refusal is in the service, not in RBAC, precisely so that a
        hospital administrator editing permission rows cannot grant it back.
        Tested with the most privileged token available."""
        opened = await api.post(
            ENCOUNTERS,
            json={"patient_id": patient_id, "encounter_type": "OPD"},
            headers=auth(reception_token),
        )
        encounter_id = opened.json()["id"]

        death = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": "2026-08-11T10:00:00Z",
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest.",
            },
            headers=auth(doctor_token),
        )
        assert death.status_code == 200, death.text

        listed = await api.get(f"{SUPPRESSIONS}?patient_id={patient_id}", headers=auth(admin_token))
        rows = listed.json()["items"]
        assert len(rows) == 1
        assert rows[0]["reason"] == "DECEASED"

        for token in (admin_token, records_token):
            response = await api.post(
                f"{SUPPRESSIONS}/{rows[0]['id']}/lift",
                json={"reason": "Recorded in error."},
                headers=auth(token),
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "suppression_permanent"

    async def test_no_route_sends_a_message_to_a_deceased_patient(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        doctor_user: User,
        patient_id: str,
    ) -> None:
        """The invariant at the API boundary, through the one route that exists
        specifically to let staff send whatever they like."""
        opened = await api.post(
            ENCOUNTERS,
            json={"patient_id": patient_id, "encounter_type": "OPD"},
            headers=auth(reception_token),
        )
        encounter_id = opened.json()["id"]
        await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": "2026-08-11T10:00:00Z",
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest.",
            },
            headers=auth(doctor_token),
        )

        response = await api.post(
            NOTIFICATIONS,
            json={"patient_id": patient_id, "body": "Please book your follow-up."},
            headers=auth(reception_token),
        )

        assert response.status_code == 201
        assert response.json()["status"] == "SUPPRESSED"
        assert response.json()["suppression_reason"] == "DECEASED"
        assert response.json()["attempt_log"] == []


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
class TestRbac:
    async def test_a_nurse_may_read_messages_but_not_send_them(
        self, api: AsyncClient, nurse_token: str, patient_id: str
    ) -> None:
        readable = await api.get(NOTIFICATIONS, headers=auth(nurse_token))
        sendable = await api.post(
            NOTIFICATIONS,
            json={"patient_id": patient_id, "body": "hello"},
            headers=auth(nurse_token),
        )

        assert readable.status_code == 200
        assert sendable.status_code == 403
        assert sendable.json()["error"]["code"] == "permission_denied"

    async def test_a_doctor_may_not_rewrite_the_hospitals_copy(
        self, api: AsyncClient, doctor_token: str
    ) -> None:
        response = await api.post(
            TEMPLATES,
            json={"code": "REPORT_READY", "channel": "SMS", "body": "hi"},
            headers=auth(doctor_token),
        )

        assert response.status_code == 403

    async def test_a_doctor_may_not_see_the_suppression_list(
        self, api: AsyncClient, doctor_token: str
    ) -> None:
        """Who has opted out of messaging is an administrative fact, not a
        clinical one."""
        response = await api.get(SUPPRESSIONS, headers=auth(doctor_token))
        assert response.status_code == 403

    async def test_reception_can_do_the_whole_counter_job(
        self, api: AsyncClient, reception_token: str, patient_id: str
    ) -> None:
        """The front desk is where "I never got the message" is said out loud."""
        assert (await api.get(NOTIFICATIONS, headers=auth(reception_token))).status_code == 200
        assert (await api.get(SUPPRESSIONS, headers=auth(reception_token))).status_code == 200
        assert (await api.get(TEMPLATES, headers=auth(reception_token))).status_code == 200
        assert (
            await api.post(
                NOTIFICATIONS,
                json={"patient_id": patient_id, "body": "hello"},
                headers=auth(reception_token),
            )
        ).status_code == 201

    async def test_an_anonymous_caller_gets_nothing(self, api: AsyncClient) -> None:
        for url in (NOTIFICATIONS, TEMPLATES, SUPPRESSIONS):
            response = await api.get(url)
            assert response.status_code == 401
