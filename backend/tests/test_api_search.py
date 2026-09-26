"""One box that resolves anything a member of staff is holding (CLAUDE.md §7b).

§7b asks for "one box [that] resolves UHID, name, mobile, token, doctor, or
appointment number". Three of those live in `patients`, three in `scheduling`,
and the visit number in `clinical` — so the interesting property under test is
not that each lookup works, but that **one request** resolves all of them and
ranks them sensibly.

The alternative the frontend would otherwise be pushed into is fanning out to
three endpoints and merging client-side, which re-ranks an exact UHID match by
string distance and buries it below a near miss. These tests pin the ordering
as much as the matching.
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
SEARCH = "/api/v1/search"
AUDIT = "/api/v1/audit-logs"


@pytest.fixture
async def doctor_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(
        session, role_code=Roles.DOCTOR, hospital_id=hospital.id, full_name="Dr Vikram Rao"
    )


@pytest.fixture
async def admin_token(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def reception_token(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def doctor_id(api: AsyncClient, admin_token: str, doctor_user: User) -> str:
    created = await api.post(
        DOCTORS,
        json={"user_id": str(doctor_user.id), "specialty": "General Medicine"},
        headers=auth(admin_token),
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture
async def visit(api: AsyncClient, reception_token: str, doctor_id: str) -> dict:
    """One registered patient, queued, with a token and an open visit."""
    patient = await api.post(
        PATIENTS,
        json={
            "full_name": "Sunita Devi",
            "phone": "9876543210",
            "gender": "FEMALE",
            "age_years": 34,
        },
        headers=auth(reception_token),
    )
    assert patient.status_code == 201, patient.text

    opd = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": patient.json()["id"], "doctor_id": doctor_id},
        headers=auth(reception_token),
    )
    assert opd.status_code == 201, opd.text

    return {"patient": patient.json(), "opd": opd.json()}


async def _search(api: AsyncClient, token: str, term: str) -> list[dict]:
    response = await api.get(SEARCH, params={"q": term}, headers=auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == term
    return list(body["hits"])


class TestOneBoxResolvesEverything:
    async def test_a_uhid_finds_the_patient(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        hits = await _search(api, reception_token, visit["patient"]["uhid"])
        assert hits
        assert hits[0]["kind"] == "PATIENT"
        assert hits[0]["title"] == "Sunita Devi"
        assert hits[0]["patient_id"] == visit["patient"]["id"]

    async def test_a_mobile_number_finds_the_patient(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        hits = await _search(api, reception_token, "9876543210")
        assert any(hit["patient_id"] == visit["patient"]["id"] for hit in hits)

    async def test_a_name_finds_the_patient(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        hits = await _search(api, reception_token, "sunita")
        assert any(hit["patient_id"] == visit["patient"]["id"] for hit in hits)

    async def test_a_token_number_finds_todays_patient(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        """The one §7b named that no endpoint could answer before.

        A receptionist holding a token slip has a small integer and nothing
        else. It resolves to a person, and to the chart that person is waiting
        for — which is the whole point of composing this on the server.
        """
        token_number = visit["opd"]["token_number"]
        hits = await _search(api, reception_token, str(token_number))

        token_hits = [hit for hit in hits if hit["kind"] == "TOKEN"]
        assert token_hits, f"token {token_number} resolved nothing"
        assert token_hits[0]["title"] == "Sunita Devi"
        assert token_hits[0]["encounter_id"] == visit["opd"]["encounter_id"]

    async def test_a_visit_number_finds_the_visit(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        """Somebody holding last week's discharge slip types this."""
        hits = await _search(api, reception_token, visit["opd"]["encounter_number"])

        assert hits[0]["kind"] == "ENCOUNTER"
        assert hits[0]["encounter_id"] == visit["opd"]["encounter_id"]
        assert hits[0]["title"] == "Sunita Devi"

    async def test_a_booking_number_finds_the_appointment(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        hits = await _search(
            api, reception_token, visit["opd"]["appointment"]["appointment_number"]
        )

        appointment_hits = [hit for hit in hits if hit["kind"] == "APPOINTMENT"]
        assert appointment_hits
        assert appointment_hits[0]["appointment_id"] == visit["opd"]["appointment"]["id"]
        assert appointment_hits[0]["doctor_id"]

    async def test_a_doctors_name_finds_the_doctor(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        hits = await _search(api, reception_token, "Vikram")

        doctor_hits = [hit for hit in hits if hit["kind"] == "DOCTOR"]
        assert doctor_hits
        assert doctor_hits[0]["doctor_id"] == doctor_id
        assert doctor_hits[0]["title"] == "Dr Vikram Rao"


class TestRanking:
    async def test_an_exact_visit_number_outranks_everything(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        """An unambiguous identifier is never second.

        This is the property a client-side merge cannot preserve: three
        endpoints returning three lists give the frontend no way to know that
        one of them was an exact match on a printed number.
        """
        hits = await _search(api, reception_token, visit["opd"]["encounter_number"])
        assert hits[0]["kind"] == "ENCOUNTER"

    async def test_a_patient_is_not_listed_twice_for_one_query(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        """A short list with the same name twice looks broken to staff."""
        hits = await _search(api, reception_token, "Sunita")
        patient_ids = [hit["patient_id"] for hit in hits if hit["kind"] == "PATIENT"]
        assert len(patient_ids) == len(set(patient_ids))

    async def test_results_are_capped(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        """A lookup box, not a report: too many results means type more."""
        for index in range(12):
            await api.post(
                PATIENTS,
                json={
                    "full_name": f"Ramesh Kumar{index}",
                    "phone": f"98000000{index:02d}",
                    "gender": "MALE",
                    "age_years": 40,
                },
                headers=auth(reception_token),
            )

        hits = await _search(api, reception_token, "ramesh")
        assert 0 < len(hits) <= 8


class TestScanningAPatientAlreadyInAVisit:
    """The error a scanned card is most likely to cause.

    A receptionist scans a card, sees a patient record, and opens a fresh
    visit — while the patient is already halfway through one, sitting in a
    corridor with a token. A split visit is a split bill and a chart in two
    halves, and neither is easy to put back together.
    """

    async def test_reception_is_told_on_the_patients_own_row(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        """Reception cannot open a chart, so the warning goes where they look.

        `note:read` gates the consultation screen, and this codebase's standing
        rule is that a hit leading to a 403 is worse than a missing one. The
        *information* still has to reach them — it is what stops the second
        registration — so it lands on the patient's own row instead, costing no
        navigation at all.
        """
        hits = await _search(api, reception_token, visit["patient"]["uhid"])

        patient_hit = next(hit for hit in hits if hit["kind"] == "PATIENT")
        assert "REGISTERED" in patient_hit["subtitle"]
        assert not any(hit["kind"] == "ENCOUNTER" for hit in hits)

    async def test_a_doctor_is_also_offered_the_chart(
        self, api: AsyncClient, doctor_user: User, reception_token: str, visit: dict
    ) -> None:
        """Somebody who can read a chart gets it as a place to go.

        Ranked below the record so pressing Enter on the first hit stays safe
        whoever is holding the scanner.
        """
        doctor_token = await login(api, doctor_user)
        hits = await _search(api, doctor_token, visit["patient"]["uhid"])

        kinds = [hit["kind"] for hit in hits]
        assert "ENCOUNTER" in kinds, "a chart-reader should be offered the open visit"
        assert kinds.index("PATIENT") < kinds.index("ENCOUNTER")

        encounter = next(hit for hit in hits if hit["kind"] == "ENCOUNTER")
        assert encounter["encounter_id"] == visit["opd"]["encounter_id"]

    async def test_a_patient_with_no_open_visit_gets_no_encounter_hit(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        """Otherwise every search grows a second row that means nothing."""
        registered = await api.post(
            PATIENTS,
            json={
                "full_name": "Ramesh Yadav",
                "phone": "9811100011",
                "gender": "MALE",
                "age_years": 58,
            },
            headers=auth(reception_token),
        )

        hits = await _search(api, reception_token, registered.json()["uhid"])
        assert [hit["kind"] for hit in hits] == ["PATIENT"]
        # And the row says nothing about a visit, because there is not one.
        assert hits[0]["subtitle"] == f"{registered.json()['uhid']} · 9811100011"

    async def test_the_open_visit_costs_one_query_for_the_whole_page(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        """Bulk, like every other worklist. A search box that slows down as the
        clinic fills up is the one screen that must not."""
        for index in range(3):
            patient = await api.post(
                PATIENTS,
                json={
                    "full_name": f"Ramesh Kumar{index}",
                    "phone": f"98111100{index:02d}",
                    "gender": "MALE",
                    "age_years": 40,
                },
                headers=auth(reception_token),
            )
            await api.post(
                f"{QUEUE}/quick-opd",
                json={"patient_id": patient.json()["id"], "doctor_id": doctor_id},
                headers=auth(reception_token),
            )

        statements: list[str] = []

        from sqlalchemy import event

        from app.core.database import get_engine

        engine = get_engine().sync_engine

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if "FROM encounters" in statement:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            hits = await _search(api, reception_token, "ramesh")
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert sum(1 for hit in hits if "REGISTERED" in (hit["subtitle"] or "")) == 3
        assert len(statements) == 1, f"expected one encounters query, got {len(statements)}"


class TestGuards:
    async def test_a_single_letter_finds_nothing(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        """One letter matches most of the hospital, so it matches nothing."""
        assert await _search(api, reception_token, "a") == []

    async def test_a_single_digit_is_a_token_lookup(
        self, api: AsyncClient, reception_token: str, visit: dict
    ) -> None:
        """The rule is about shape, not length — and this one is load-bearing.

        The first nine patients of every clinic hold a single-digit token, so a
        blanket two-character minimum would break the lookup for the busiest
        hour of every morning. This test exists because that is exactly the bug
        the first version of this endpoint shipped with.
        """
        hits = await _search(api, reception_token, str(visit["opd"]["token_number"]))
        assert any(hit["kind"] == "TOKEN" for hit in hits)

    async def test_an_empty_query_is_refused(self, api: AsyncClient, reception_token: str) -> None:
        response = await api.get(SEARCH, params={"q": ""}, headers=auth(reception_token))
        assert response.status_code == 422

    async def test_an_unauthenticated_search_is_refused(self, api: AsyncClient) -> None:
        assert (await api.get(SEARCH, params={"q": "sunita"})).status_code == 401

    async def test_nothing_found_is_an_empty_list_not_an_error(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        hits = await _search(api, reception_token, "ENC-99-999999")
        assert hits == []

    async def test_a_search_is_audited(
        self, api: AsyncClient, hospital_admin: User, reception_token: str
    ) -> None:
        """DPDP: a lookup of personal data is an access, whether or not the
        record is opened afterwards."""
        await _search(api, reception_token, "sunita")

        admin_token = await login(api, hospital_admin)
        entries = (
            await api.get(AUDIT, params={"action": "search.universal"}, headers=auth(admin_token))
        ).json()

        assert entries["total"] >= 1
        assert entries["items"][0]["changes"]["query"] == "sunita"

    async def test_another_hospitals_records_are_invisible(
        self, api: AsyncClient, session: AsyncSession, reception_token: str, visit: dict
    ) -> None:
        import uuid

        from app.core import database as db

        async with db.system_context(session):
            other = Hospital(code=f"S{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        outsider = await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=other_id)
        outsider_token = await login(api, outsider)

        # Every identifier from the first tenant, tried from the second.
        for term in (
            visit["patient"]["uhid"],
            "9876543210",
            "sunita",
            visit["opd"]["encounter_number"],
            visit["opd"]["appointment"]["appointment_number"],
        ):
            assert await _search(api, outsider_token, term) == [], term
