"""Inpatient care over HTTP: a whole stay, and RBAC.

The first class walks one admission from the front desk to a signed discharge
summary exactly as ward staff would — because that is the chain CLAUDE.md §13
step 9 asks for, and it only works if every link agrees.

The RBAC class is the one to read closely. Two separations are asserted by name
because getting them wrong is a patient-safety problem rather than an
inconvenience:

* a doctor prescribes and does not sign for a dose at the bedside;
* a nurse gives the drug and does not prescribe it.

That is the second pair of eyes, and it is most of what a medication chart is
for.
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
WARDS = "/api/v1/ipd/wards"
BEDS = "/api/v1/ipd/beds"
ADMISSIONS = "/api/v1/ipd/admissions"
IPD = "/api/v1/ipd"
SUMMARIES = "/api/v1/ipd/summaries"


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
async def capacity(api: AsyncClient, admin_token: str) -> dict[str, str]:
    """One ward, two beds — the smallest hospital that can still transfer."""
    ward = await api.post(
        WARDS,
        json={"code": "MW1", "name": "Male Medical", "bed_class": "GENERAL"},
        headers=auth(admin_token),
    )
    assert ward.status_code == 201, ward.text
    ward_id = ward.json()["id"]

    ids: dict[str, str] = {"ward": ward_id}
    for code in ("MW1-01", "MW1-02"):
        bed = await api.post(
            BEDS,
            json={"ward_id": ward_id, "code": code, "tariff_item_code": "ROOM-GEN"},
            headers=auth(admin_token),
        )
        assert bed.status_code == 201, bed.text
        ids[code] = bed.json()["id"]
    return ids


@pytest.fixture
async def visit(api: AsyncClient, reception_token: str) -> dict[str, str]:
    """A registered patient with an open encounter."""
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
    patient_id = patient.json()["id"]

    encounter = await api.post(
        ENCOUNTERS,
        json={"patient_id": patient_id, "chief_complaint": "Fever and cough for five days"},
        headers=auth(reception_token),
    )
    assert encounter.status_code in (200, 201), encounter.text
    return {"patient": patient_id, "encounter": encounter.json()["id"]}


@pytest.fixture
async def admitted(
    api: AsyncClient, reception_token: str, capacity: dict[str, str], visit: dict[str, str]
) -> dict:
    response = await api.post(
        ADMISSIONS,
        json={
            "encounter_id": visit["encounter"],
            "bed_id": capacity["MW1-01"],
            "provisional_diagnosis": "Community-acquired pneumonia",
            "attendant_name": "Sita Yadav",
            "attendant_relation": "Wife",
        },
        headers=auth(reception_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# A whole stay
# ---------------------------------------------------------------------------
class TestAWholeStay:
    async def test_admission_returns_the_patient_and_where_they_are(
        self, admitted: dict, capacity: dict[str, str]
    ) -> None:
        """One response carries the stay and the bed. A ward screen needs both."""
        assert admitted["admission_number"].startswith("IPD-")
        assert admitted["status"] == "ADMITTED"
        assert admitted["bed"]["code"] == "MW1-01"
        assert admitted["bed"]["status"] == "OCCUPIED"
        assert admitted["ward"]["name"] == "Male Medical"
        assert admitted["length_of_stay_days"] == 1

    async def test_the_visit_became_an_inpatient_encounter(
        self, api: AsyncClient, reception_token: str, visit: dict[str, str], admitted: dict
    ) -> None:
        response = await api.get(
            f"{ENCOUNTERS}/{visit['encounter']}", headers=auth(reception_token)
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ADMITTED"
        assert response.json()["encounter_type"] == "IPD"

    async def test_the_board_shows_the_ward_at_a_glance(
        self, api: AsyncClient, nurse_token: str, admitted: dict
    ) -> None:
        response = await api.get(f"{WARDS}/board", headers=auth(nurse_token))
        assert response.status_code == 200, response.text

        wards = response.json()
        assert len(wards) == 1
        assert wards[0]["occupied"] == 1
        assert wards[0]["available"] == 1

        occupied = [bed for bed in wards[0]["beds"] if bed["status"] == "OCCUPIED"]
        assert occupied[0]["patient_name"] == "Ramesh Yadav"
        assert occupied[0]["uhid"]

    async def test_a_second_patient_is_refused_the_occupied_bed(
        self,
        api: AsyncClient,
        reception_token: str,
        capacity: dict[str, str],
        admitted: dict,
    ) -> None:
        patient = await api.post(
            PATIENTS,
            json={
                "full_name": "Vikram Singh",
                "phone": "9812345671",
                "gender": "MALE",
                "age_years": 40,
            },
            headers=auth(reception_token),
        )
        encounter = await api.post(
            ENCOUNTERS,
            json={"patient_id": patient.json()["id"]},
            headers=auth(reception_token),
        )
        response = await api.post(
            ADMISSIONS,
            json={
                "encounter_id": encounter.json()["id"],
                "bed_id": capacity["MW1-01"],
            },
            headers=auth(reception_token),
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "bed_unavailable"

    async def test_a_transfer_moves_the_patient_and_sends_the_old_bed_to_cleaning(
        self,
        api: AsyncClient,
        nurse_token: str,
        admin_token: str,
        capacity: dict[str, str],
        admitted: dict,
    ) -> None:
        response = await api.post(
            f"{ADMISSIONS}/{admitted['id']}/transfer",
            json={"to_bed_id": capacity["MW1-02"], "reason": "Moved for infection control"},
            headers=auth(nurse_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["bed"]["code"] == "MW1-02"

        vacated = await api.get(f"{BEDS}/{capacity['MW1-01']}", headers=auth(admin_token))
        assert vacated.json()["status"] == "CLEANING"

    async def test_the_chart_runs_from_prescription_to_signature(
        self, api: AsyncClient, doctor_token: str, nurse_token: str, admitted: dict
    ) -> None:
        """The doctor writes it; the nurse gives it. Two people, by design."""
        order = await api.post(
            f"{IPD}/admissions/{admitted['id']}/medications",
            json={
                "drug_name": "Amoxicillin",
                "dose": "500 mg",
                "route": "ORAL",
                "frequency": "TDS",
                "times_per_day": 3,
                "dose_times": ["08:00", "14:00", "20:00"],
                "drug_schedule": "H",
            },
            headers=auth(doctor_token),
        )
        assert order.status_code == 201, order.text

        due = await api.get(
            f"{IPD}/doses",
            params={"admission_id": admitted["id"], "status": "DUE"},
            headers=auth(nurse_token),
        )
        assert due.status_code == 200, due.text
        assert due.json()["total"] > 0

        first = due.json()["items"][0]
        given = await api.post(
            f"{IPD}/doses/{first['id']}",
            json={"status": "GIVEN"},
            headers=auth(nurse_token),
        )
        assert given.status_code == 200, given.text
        assert given.json()["status"] == "GIVEN"
        assert given.json()["administered_by_name"]

    async def test_a_dose_not_given_is_refused_without_a_reason(
        self, api: AsyncClient, doctor_token: str, nurse_token: str, admitted: dict
    ) -> None:
        """A blank reason on a missed antibiotic is the gap a review cannot close."""
        await api.post(
            f"{IPD}/admissions/{admitted['id']}/medications",
            json={
                "drug_name": "Enoxaparin",
                "dose": "40 mg",
                "route": "SC",
                "frequency": "OD",
                "times_per_day": 1,
                "dose_times": ["08:00"],
            },
            headers=auth(doctor_token),
        )
        due = await api.get(
            f"{IPD}/doses",
            params={"admission_id": admitted["id"], "status": "DUE"},
            headers=auth(nurse_token),
        )
        dose_id = due.json()["items"][0]["id"]

        response = await api.post(
            f"{IPD}/doses/{dose_id}", json={"status": "REFUSED"}, headers=auth(nurse_token)
        )
        assert response.status_code == 422, response.text

    async def test_initiating_discharge_compiles_a_summary_and_keeps_the_bed(
        self, api: AsyncClient, doctor_token: str, admitted: dict
    ) -> None:
        response = await api.post(
            f"{ADMISSIONS}/{admitted['id']}/initiate-discharge", headers=auth(doctor_token)
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "DISCHARGE_INITIATED"
        assert response.json()["bed"]["status"] == "OCCUPIED"

        summary = await api.get(
            f"{SUMMARIES}/by-admission/{admitted['id']}", headers=auth(doctor_token)
        )
        assert summary.status_code == 200, summary.text
        assert summary.json()["status"] == "DRAFT"

    async def test_a_death_cannot_be_entered_as_a_discharge_type(
        self, api: AsyncClient, doctor_token: str, admitted: dict
    ) -> None:
        """CLAUDE.md §6: one way to record a death, and this is not it.

        The endpoint refuses, and the refusal says where the death is actually
        recorded — otherwise this is just an error somebody works around.
        """
        response = await api.post(
            f"{ADMISSIONS}/{admitted['id']}/discharge",
            json={"discharge_type": "DECEASED"},
            headers=auth(doctor_token),
        )
        assert response.status_code == 422, response.text
        assert "recorded against the visit" in response.text

    async def test_discharge_closes_the_visit_and_frees_the_bed_to_cleaning(
        self,
        api: AsyncClient,
        doctor_token: str,
        reception_token: str,
        capacity: dict[str, str],
        visit: dict[str, str],
        admitted: dict,
    ) -> None:
        response = await api.post(
            f"{ADMISSIONS}/{admitted['id']}/discharge",
            json={
                "discharge_type": "RECOVERED",
                "condition_at_discharge": "Afebrile, chest clear, mobilising independently.",
                "follow_up_instructions": "Review in OPD after one week.",
            },
            headers=auth(doctor_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "DISCHARGED"
        assert response.json()["discharge_type"] == "RECOVERED"

        bed = await api.get(f"{BEDS}/{capacity['MW1-01']}", headers=auth(doctor_token))
        assert bed.json()["status"] == "CLEANING"

        encounter = await api.get(
            f"{ENCOUNTERS}/{visit['encounter']}", headers=auth(reception_token)
        )
        assert encounter.json()["status"] == "COMPLETED"

    async def test_housekeeping_returns_the_bed_to_service(
        self,
        api: AsyncClient,
        doctor_token: str,
        nurse_token: str,
        capacity: dict[str, str],
        admitted: dict,
    ) -> None:
        """The rung that makes "available" mean available (CLAUDE.md §7b)."""
        await api.post(
            f"{ADMISSIONS}/{admitted['id']}/discharge",
            json={"discharge_type": "RECOVERED"},
            headers=auth(doctor_token),
        )
        response = await api.post(f"{BEDS}/{capacity['MW1-01']}/cleaned", headers=auth(nurse_token))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "AVAILABLE"

    async def test_the_summary_is_signed_with_a_registration_number(
        self, api: AsyncClient, doctor_token: str, admitted: dict, visit: dict[str, str]
    ) -> None:
        """The doctor reviews and signs. They do not type the document."""
        diagnosis = await api.post(
            f"{ENCOUNTERS}/{visit['encounter']}/diagnoses",
            json={
                "description": "Community-acquired pneumonia",
                "code": "J18.9",
                "diagnosis_type": "FINAL",
                "is_primary": True,
            },
            headers=auth(doctor_token),
        )
        assert diagnosis.status_code in (200, 201), diagnosis.text

        compiled = await api.post(
            f"{SUMMARIES}/compile/{admitted['id']}", headers=auth(doctor_token)
        )
        assert compiled.status_code == 200, compiled.text
        assert "Community-acquired pneumonia" in compiled.json()["diagnoses"]

        signed = await api.post(
            f"{SUMMARIES}/{compiled.json()['id']}/sign",
            json={"registration_number": "MCI-12345"},
            headers=auth(doctor_token),
        )
        assert signed.status_code == 200, signed.text
        assert signed.json()["status"] == "FINAL"
        assert signed.json()["signatory_registration_number"] == "MCI-12345"

    async def test_unsigned_summaries_are_a_worklist(
        self, api: AsyncClient, doctor_token: str, admitted: dict
    ) -> None:
        """What a consultant owes, rather than a report nobody runs."""
        await api.post(f"{SUMMARIES}/compile/{admitted['id']}", headers=auth(doctor_token))
        response = await api.get(SUMMARIES, params={"status": "DRAFT"}, headers=auth(doctor_token))
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
class TestTheWardListsPeopleNotIdentifiers:
    """A census of UUIDs is not a census.

    `AdmissionSummary` and `AdmissionRead` carried `patient_id` and nothing
    else, which is right for the module's own logic and useless on a ward
    screen. The question a charge nurse opens the census to answer is "who is
    in my ward and where", and both halves of it live in other modules' tables
    — so the router joins them, in bulk, through those modules' services
    (CLAUDE.md §2).
    """

    async def test_a_census_row_names_the_patient_and_the_bed(
        self, api: AsyncClient, reception_token: str, admitted: dict
    ) -> None:
        response = await api.get(
            ADMISSIONS, params={"open_only": True}, headers=auth(reception_token)
        )
        assert response.status_code == 200, response.text

        [row] = [r for r in response.json()["items"] if r["id"] == admitted["id"]]
        assert row["patient_name"] == "Ramesh Yadav"
        assert row["uhid"]
        assert row["patient_age_years"] == 58
        assert row["patient_gender"] == "MALE"
        # Where they actually are — the other half of the question.
        assert row["bed_code"] == "MW1-01"
        assert row["ward_name"] == "Male Medical"

    async def test_the_admission_screen_names_the_patient(
        self, api: AsyncClient, reception_token: str, admitted: dict
    ) -> None:
        """The screen where somebody transfers a bed or signs a discharge has
        to say whose stay it is, at the moment of the act."""
        response = await api.get(f"{ADMISSIONS}/{admitted['id']}", headers=auth(reception_token))
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["patient_name"] == "Ramesh Yadav"
        assert body["uhid"]
        assert body["patient_age_years"] == 58
        assert body["patient_is_deceased"] is False
        assert body["bed"]["code"] == "MW1-01"
        assert body["ward"]["name"] == "Male Medical"

    async def test_identity_costs_one_lookup_regardless_of_census_length(
        self,
        api: AsyncClient,
        reception_token: str,
        admin_token: str,
        capacity: dict[str, str],
        admitted: dict,
    ) -> None:
        """A ward round reads this list, and it is longest when the ward is
        busiest. It must not cost a request per patient."""
        # Three more beds, three more patients, three more admissions.
        for index in range(3):
            bed = await api.post(
                BEDS,
                json={"ward_id": capacity["ward"], "code": f"MW1-1{index}"},
                headers=auth(admin_token),
            )
            assert bed.status_code == 201, bed.text

            patient = await api.post(
                PATIENTS,
                json={
                    "full_name": f"Ward Patient{index}",
                    "phone": f"98123456{index:02d}",
                    "gender": "MALE",
                    "age_years": 40 + index,
                },
                headers=auth(reception_token),
            )
            encounter = await api.post(
                ENCOUNTERS,
                json={"patient_id": patient.json()["id"]},
                headers=auth(reception_token),
            )
            admission = await api.post(
                ADMISSIONS,
                json={"encounter_id": encounter.json()["id"], "bed_id": bed.json()["id"]},
                headers=auth(reception_token),
            )
            assert admission.status_code == 201, admission.text

        statements: list[str] = []

        from sqlalchemy import event

        from app.core.database import get_engine

        engine = get_engine().sync_engine

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if "FROM patients" in statement:
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = await api.get(
                ADMISSIONS, params={"open_only": True}, headers=auth(reception_token)
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 4
        assert len(statements) == 1, f"expected a single patients query, got {len(statements)}"


class TestTheRoundNamesThePatient:
    """A medication round that cannot say whose dose this is.

    `GET /ipd/doses` without an admission filter is the ward round — its own
    docstring says "what is due, **on whom**" — and `DoseRead` carried only a
    `patient_id`. Checking the patient against the chart before giving a drug
    is *the* check, and it is not one a screen full of UUIDs supports. This is
    the same identity gap as the queue, the bench and the counter, in the place
    where getting it wrong hurts most.
    """

    async def test_a_dose_row_names_the_patient_and_the_bed(
        self, api: AsyncClient, doctor_token: str, nurse_token: str, admitted: dict
    ) -> None:
        prescribed = await api.post(
            f"{IPD}/admissions/{admitted['id']}/medications",
            json={
                "drug_name": "Amoxicillin",
                "dose": "500 mg",
                "route": "ORAL",
                "frequency": "TDS",
                "times_per_day": 3,
            },
            headers=auth(doctor_token),
        )
        assert prescribed.status_code == 201, prescribed.text

        response = await api.get(f"{IPD}/doses", headers=auth(nurse_token))
        assert response.status_code == 200, response.text

        rows = [row for row in response.json()["items"] if row["drug_name"] == "Amoxicillin"]
        assert rows, "the prescribed drug produced no doses"
        assert rows[0]["patient_name"] == "Ramesh Yadav"
        assert rows[0]["uhid"]
        assert rows[0]["bed_code"] == "MW1-01"
        assert rows[0]["ward_name"] == "Male Medical"
        assert rows[0]["patient_is_deceased"] is False

    async def test_signing_returns_a_row_that_still_names_the_patient(
        self, api: AsyncClient, doctor_token: str, nurse_token: str, admitted: dict
    ) -> None:
        """The row the nurse just signed is the row they read back."""
        await api.post(
            f"{IPD}/admissions/{admitted['id']}/medications",
            json={
                "drug_name": "Paracetamol",
                "dose": "650 mg",
                "route": "ORAL",
                "frequency": "BD",
                "times_per_day": 2,
            },
            headers=auth(doctor_token),
        )
        doses = (await api.get(f"{IPD}/doses", headers=auth(nurse_token))).json()["items"]
        dose = next(row for row in doses if row["drug_name"] == "Paracetamol")

        signed = await api.post(
            f"{IPD}/doses/{dose['id']}",
            json={"status": "GIVEN"},
            headers=auth(nurse_token),
        )
        assert signed.status_code == 200, signed.text
        assert signed.json()["patient_name"] == "Ramesh Yadav"
        assert signed.json()["bed_code"] == "MW1-01"

    async def test_identity_costs_one_lookup_regardless_of_round_length(
        self, api: AsyncClient, doctor_token: str, nurse_token: str, admitted: dict
    ) -> None:
        """A ward of twenty patients on four drugs each is eighty rows."""
        for index in range(4):
            await api.post(
                f"{IPD}/admissions/{admitted['id']}/medications",
                json={
                    "drug_name": f"Drug{index}",
                    "dose": "1 tab",
                    "route": "ORAL",
                    "frequency": "TDS",
                    "times_per_day": 3,
                },
                headers=auth(doctor_token),
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
            response = await api.get(f"{IPD}/doses", headers=auth(nurse_token))
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) > 4
        assert len(statements) == 1, f"expected a single patients query, got {len(statements)}"


class TestWhoMayDoWhat:
    async def test_a_nurse_cannot_prescribe(
        self, api: AsyncClient, nurse_token: str, admitted: dict
    ) -> None:
        """The second pair of eyes, from one side."""
        response = await api.post(
            f"{IPD}/admissions/{admitted['id']}/medications",
            json={
                "drug_name": "Morphine",
                "dose": "5 mg",
                "route": "IV",
                "frequency": "SOS",
                "is_prn": True,
                "prn_indication": "for pain",
                "drug_schedule": "NARCOTIC",
            },
            headers=auth(nurse_token),
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_doctor_cannot_sign_for_a_dose_at_the_bedside(
        self, api: AsyncClient, doctor_token: str, nurse_token: str, admitted: dict
    ) -> None:
        """And the other side. A ward where one person does both has no check."""
        await api.post(
            f"{IPD}/admissions/{admitted['id']}/medications",
            json={
                "drug_name": "Amoxicillin",
                "dose": "500 mg",
                "frequency": "OD",
                "times_per_day": 1,
                "dose_times": ["08:00"],
            },
            headers=auth(doctor_token),
        )
        due = await api.get(
            f"{IPD}/doses",
            params={"admission_id": admitted["id"], "status": "DUE"},
            headers=auth(nurse_token),
        )
        dose_id = due.json()["items"][0]["id"]

        response = await api.post(
            f"{IPD}/doses/{dose_id}", json={"status": "GIVEN"}, headers=auth(doctor_token)
        )
        assert response.status_code == 403, response.text

    async def test_a_nurse_cannot_create_beds(
        self, api: AsyncClient, nurse_token: str, capacity: dict[str, str]
    ) -> None:
        """A hospital whose bed count changes on a typo has lost its census."""
        response = await api.post(
            BEDS,
            json={"ward_id": capacity["ward"], "code": "MW1-99"},
            headers=auth(nurse_token),
        )
        assert response.status_code == 403, response.text

    async def test_a_cashier_may_read_the_census_and_not_admit(
        self, api: AsyncClient, cashier_token: str, visit: dict[str, str], capacity: dict[str, str]
    ) -> None:
        """The counter needs the bed class to price the stay, and nothing more."""
        readable = await api.get(ADMISSIONS, headers=auth(cashier_token))
        assert readable.status_code == 200, readable.text

        response = await api.post(
            ADMISSIONS,
            json={"encounter_id": visit["encounter"], "bed_id": capacity["MW1-01"]},
            headers=auth(cashier_token),
        )
        assert response.status_code == 403, response.text

    async def test_a_receptionist_cannot_sign_a_discharge_summary(
        self, api: AsyncClient, reception_token: str, doctor_token: str, admitted: dict
    ) -> None:
        """A signature carries clinical responsibility and a registration number."""
        compiled = await api.post(
            f"{SUMMARIES}/compile/{admitted['id']}", headers=auth(doctor_token)
        )
        response = await api.post(
            f"{SUMMARIES}/{compiled.json()['id']}/sign",
            json={"registration_number": "NOT-A-DOCTOR"},
            headers=auth(reception_token),
        )
        assert response.status_code == 403, response.text

    async def test_an_anonymous_request_is_refused(self, api: AsyncClient, admitted: dict) -> None:
        response = await api.get(f"{WARDS}/board")
        assert response.status_code == 401, response.text
