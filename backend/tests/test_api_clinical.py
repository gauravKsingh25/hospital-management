"""Clinical over HTTP: a whole OPD visit, the edge cases, and RBAC.

The first class walks one patient from the counter to a closed visit exactly as
the staff would, because that path — check-in opens the chart, the doctor
completes, the pharmacist clears the last item, the visit closes itself — is the
one CLAUDE.md §6 is really about, and it only works if every piece agrees.
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

PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
QUEUE = "/api/v1/queue"
ENCOUNTERS = "/api/v1/encounters"
ORDERS = "/api/v1/orders"
TEMPLATES = "/api/v1/note-templates"


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
async def pharmacist_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.PHARMACIST, hospital_id=hospital.id)


@pytest.fixture
async def records_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.RECORDS_OFFICER, hospital_id=hospital.id)


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


@pytest.fixture
async def encounter_id(
    api: AsyncClient, reception_token: str, patient_id: str, doctor_id: str
) -> str:
    """A checked-in patient — and therefore an open chart."""
    response = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": patient_id, "doctor_id": doctor_id, "reason": "Fever"},
        headers=auth(reception_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["encounter_id"])


# ---------------------------------------------------------------------------
# The whole visit, as the staff would do it
# ---------------------------------------------------------------------------
class TestTheOpdDay:
    async def test_quick_opd_opens_the_chart_with_the_token(
        self, api: AsyncClient, reception_token: str, patient_id: str, doctor_id: str
    ) -> None:
        """One click gives reception the token slip AND the chart (§7b)."""
        response = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id, "reason": "Fever"},
            headers=auth(reception_token),
        )
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["token_number"] == 1
        assert body["encounter_number"].startswith("ENC-")
        assert body["appointment"]["encounter_id"] == body["encounter_id"]

    async def test_a_visit_starts_registered(
        self, api: AsyncClient, reception_token: str, encounter_id: str
    ) -> None:
        response = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(reception_token))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "REGISTERED"
        assert response.json()["chief_complaint"] == "Fever"

    async def test_the_doctors_screen_opens_in_one_call(
        self, api: AsyncClient, doctor_token: str, encounter_id: str
    ) -> None:
        """Six requests instead of one is most of the §7b 60-second target."""
        response = await api.get(f"{ENCOUNTERS}/{encounter_id}/chart", headers=auth(doctor_token))
        assert response.status_code == 200, response.text

        chart = response.json()
        assert set(chart) == {
            "encounter",
            "banner",
            "vitals",
            "notes",
            "diagnoses",
            "orders",
            "pending",
        }
        assert chart["banner"]["uhid"]
        assert chart["banner"]["age_years"] == 34

    async def test_a_clean_visit_closes_when_the_doctor_finishes(
        self, api: AsyncClient, doctor_token: str, encounter_id: str
    ) -> None:
        started = await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "IN_CONSULTATION"

        done = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/complete",
            json={"follow_up_date": None},
            headers=auth(doctor_token),
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "COMPLETED"

    async def test_an_order_holds_the_visit_until_staff_clear_it(
        self,
        api: AsyncClient,
        doctor_token: str,
        pharmacist_user: User,
        encounter_id: str,
    ) -> None:
        """CLAUDE.md §6, end to end: the pharmacist's click closes the visit,
        and they never have to know that."""
        await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        order = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/orders",
            json={"order_type": "PHARMACY", "item_name": "Paracetamol 650mg"},
            headers=auth(doctor_token),
        )
        assert order.status_code == 201, order.text
        order_id = order.json()["id"]

        done = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/complete", json={}, headers=auth(doctor_token)
        )
        assert done.json()["status"] == "PENDING_CLEARANCE"

        pending = await api.get(f"{ENCOUNTERS}/{encounter_id}/pending", headers=auth(doctor_token))
        assert pending.json()["total"] == 1
        assert "Paracetamol 650mg" in pending.json()["descriptions"][0]

        pharmacy_token = await login(api, pharmacist_user)
        cleared = await api.post(f"{ORDERS}/{order_id}/complete", headers=auth(pharmacy_token))
        assert cleared.status_code == 200, cleared.text

        visit = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(doctor_token))
        assert visit.json()["status"] == "COMPLETED"

    async def test_a_nurse_records_vitals(
        self, api: AsyncClient, nurse_token: str, encounter_id: str
    ) -> None:
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/vitals",
            json={
                "temperature_c": "38.9",
                "pulse_bpm": 96,
                "systolic_bp": 118,
                "diastolic_bp": 76,
                "spo2_percent": 97,
            },
            headers=auth(nurse_token),
        )
        assert response.status_code == 201, response.text
        assert response.json()["is_abnormal"] is True

    async def test_the_timeline_reads_as_the_visit_happened(
        self, api: AsyncClient, doctor_token: str, encounter_id: str
    ) -> None:
        await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        await api.post(f"{ENCOUNTERS}/{encounter_id}/complete", json={}, headers=auth(doctor_token))

        response = await api.get(
            f"{ENCOUNTERS}/{encounter_id}/timeline", headers=auth(doctor_token)
        )
        assert response.status_code == 200, response.text
        assert [row["to_status"] for row in response.json()] == [
            "REGISTERED",
            "IN_CONSULTATION",
            "COMPLETED",
        ]

    async def test_an_illegal_move_is_refused_in_words_staff_can_repeat(
        self, api: AsyncClient, doctor_token: str, encounter_id: str
    ) -> None:
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/complete", json={}, headers=auth(doctor_token)
        )
        assert response.status_code == 409, response.text

        error = response.json()["error"]
        assert error["code"] == "illegal_state_transition"
        assert "registered" in error["message"]
        assert "IN_CONSULTATION" in error["details"]["allowed"]


# ---------------------------------------------------------------------------
# Notes, diagnoses and templates
# ---------------------------------------------------------------------------
class TestTheChart:
    async def test_a_doctor_writes_and_signs_a_note(
        self, api: AsyncClient, doctor_token: str, encounter_id: str
    ) -> None:
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/notes",
            json={"note_type": "EXAMINATION", "content": "Chest clear."},
            headers=auth(doctor_token),
        )
        assert response.status_code == 201, response.text
        assert response.json()["is_signed"] is True

    async def test_a_doctor_records_a_diagnosis(
        self, api: AsyncClient, doctor_token: str, encounter_id: str
    ) -> None:
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/diagnoses",
            json={"description": "Dengue fever", "code": "A90", "is_primary": True},
            headers=auth(doctor_token),
        )
        assert response.status_code == 201, response.text
        assert response.json()["is_primary"] is True

    async def test_a_doctor_keeps_their_own_templates(
        self, api: AsyncClient, doctor_token: str
    ) -> None:
        created = await api.post(
            TEMPLATES,
            json={"title": "URI advice", "body": "Rest, fluids, paracetamol SOS."},
            headers=auth(doctor_token),
        )
        assert created.status_code == 201, created.text

        listed = await api.get(TEMPLATES, headers=auth(doctor_token))
        assert [row["title"] for row in listed.json()] == ["URI advice"]

    async def test_a_doctor_cannot_push_a_template_to_everyone(
        self, api: AsyncClient, doctor_token: str, nurse_token: str
    ) -> None:
        """Sharing hospital-wide lands on everybody's screen, so it needs
        `template:manage`, which a nurse does not hold."""
        response = await api.post(
            TEMPLATES,
            json={"title": "Ward round", "body": "Stable overnight.", "shared": True},
            headers=auth(nurse_token),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "template_share_denied"


# ---------------------------------------------------------------------------
# The three edge cases over HTTP
# ---------------------------------------------------------------------------
class TestEdgeCases:
    async def test_records_officer_records_a_death(
        self,
        api: AsyncClient,
        records_user: User,
        doctor_user: User,
        doctor_token: str,
        encounter_id: str,
    ) -> None:
        token = await login(api, records_user)
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": utc_now().isoformat(),
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest",
                "death_place": "Casualty",
            },
            headers=auth(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "DECEASED"
        assert response.json()["closed_at"] is not None

    async def test_a_death_record_names_its_certifying_doctor(
        self,
        api: AsyncClient,
        records_user: User,
        doctor_user: User,
        encounter_id: str,
    ) -> None:
        """The one identity gap that ends up on a document a family is given.

        `death_certified_by_id` alone makes the record unreadable by everyone
        who needs to read it — the records officer, the registrar of deaths,
        the family. Attached by the router through `identity.service`, and only
        on the rare encounters that have a certifier at all.
        """
        token = await login(api, records_user)
        recorded = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": utc_now().isoformat(),
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest",
            },
            headers=auth(token),
        )
        assert recorded.json()["death_certified_by_name"] == doctor_user.full_name

        # And on every later read of the record, not only on the response to
        # the act of recording it.
        fetched = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(token))
        assert fetched.json()["death_certified_by_name"] == doctor_user.full_name

    async def test_a_living_patients_visit_costs_no_certifier_lookup(
        self, api: AsyncClient, doctor_token: str, encounter_id: str
    ) -> None:
        """The name is looked up only where there is one to look up.

        Every encounter response would otherwise pay for a death that has not
        happened, on the busiest read in the system.
        """
        statements: list[str] = []

        from sqlalchemy import event

        from app.core.database import get_engine

        engine = get_engine().sync_engine

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if "FROM users" in statement:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(doctor_token))
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        assert response.json()["death_certified_by_name"] is None
        # Only the authentication lookup, never a certifier one.
        assert len(statements) <= 1, f"unexpected user queries: {len(statements)}"

    async def test_a_death_needs_a_cause(
        self, api: AsyncClient, records_user: User, doctor_user: User, encounter_id: str
    ) -> None:
        token = await login(api, records_user)
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": utc_now().isoformat(),
                "death_certified_by_id": str(doctor_user.id),
            },
            headers=auth(token),
        )
        assert response.status_code == 422, response.text
        fields = [item["field"] for item in response.json()["error"]["details"]["fields"]]
        assert "cause_of_death" in fields

    async def test_a_death_cannot_be_dated_in_the_future(
        self, api: AsyncClient, records_user: User, doctor_user: User, encounter_id: str
    ) -> None:
        from datetime import timedelta

        token = await login(api, records_user)
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": (utc_now() + timedelta(hours=2)).isoformat(),
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest",
            },
            headers=auth(token),
        )
        assert response.status_code == 422, response.text

    async def test_a_deceased_patient_cannot_be_checked_in_again(
        self,
        api: AsyncClient,
        records_user: User,
        doctor_user: User,
        reception_token: str,
        patient_id: str,
        doctor_id: str,
        encounter_id: str,
    ) -> None:
        """The §14 invariant at its most likely leak."""
        token = await login(api, records_user)
        await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": utc_now().isoformat(),
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest",
            },
            headers=auth(token),
        )

        response = await api.post(
            f"{QUEUE}/quick-opd",
            json={"patient_id": patient_id, "doctor_id": doctor_id},
            headers=auth(reception_token),
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "patient_deceased"

    async def test_a_referral_records_where_the_patient_went(
        self, api: AsyncClient, records_user: User, encounter_id: str
    ) -> None:
        token = await login(api, records_user)
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/referral",
            json={
                "referred_to_facility": "AIIMS Delhi",
                "referral_reason": "Neurosurgery unavailable here.",
                "referral_transport": "Ambulance",
            },
            headers=auth(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "REFERRED_OUT"
        assert response.json()["referred_to_facility"] == "AIIMS Delhi"

    async def test_lama_records_whether_the_form_was_signed(
        self, api: AsyncClient, records_user: User, encounter_id: str
    ) -> None:
        token = await login(api, records_user)
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/lama",
            json={"lama_reason": "Family took the patient home.", "lama_form_signed": False},
            headers=auth(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "LAMA"
        assert response.json()["lama_form_signed"] is False


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
class TestPermissions:
    async def test_a_receptionist_cannot_open_the_chart(
        self, api: AsyncClient, reception_token: str, encounter_id: str
    ) -> None:
        """Reception runs the front of house and does not read the note."""
        response = await api.get(
            f"{ENCOUNTERS}/{encounter_id}/chart", headers=auth(reception_token)
        )
        assert response.status_code == 403, response.text

    async def test_a_receptionist_can_still_see_the_visit(
        self, api: AsyncClient, reception_token: str, encounter_id: str
    ) -> None:
        response = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(reception_token))
        assert response.status_code == 200, response.text

    async def test_a_nurse_cannot_place_an_order(
        self, api: AsyncClient, nurse_token: str, encounter_id: str
    ) -> None:
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/orders",
            json={"order_type": "LAB", "item_name": "CBC"},
            headers=auth(nurse_token),
        )
        assert response.status_code == 403, response.text

    async def test_a_receptionist_cannot_record_a_death(
        self, api: AsyncClient, reception_token: str, doctor_user: User, encounter_id: str
    ) -> None:
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": utc_now().isoformat(),
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest",
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 403, response.text

    async def test_a_cashier_cannot_clear_a_lab_order(
        self,
        api: AsyncClient,
        doctor_token: str,
        cashier_user: User,
        encounter_id: str,
    ) -> None:
        """Gated twice: the permission, then the *kind* of staff."""
        await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
        order = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/orders",
            json={"order_type": "LAB", "item_name": "CBC"},
            headers=auth(doctor_token),
        )
        order_id = order.json()["id"]

        cashier_token = await login(api, cashier_user)
        response = await api.post(f"{ORDERS}/{order_id}/complete", headers=auth(cashier_token))
        # A cashier holds neither `order:fulfil` nor the lab role.
        assert response.status_code == 403, response.text

    async def test_the_certifying_doctor_must_work_here(
        self, api: AsyncClient, records_user: User, platform_admin: User, encounter_id: str
    ) -> None:
        """A death certified by somebody from another tenant is not a record."""
        token = await login(api, records_user)
        response = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": utc_now().isoformat(),
                "death_certified_by_id": str(platform_admin.id),
                "cause_of_death": "Cardiac arrest",
            },
            headers=auth(token),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "certifier_not_found"

    async def test_an_unauthenticated_request_is_refused(
        self, api: AsyncClient, encounter_id: str
    ) -> None:
        response = await api.get(f"{ENCOUNTERS}/{encounter_id}")
        assert response.status_code == 401, response.text
