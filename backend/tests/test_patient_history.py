"""The patient's history across visits: counts, numbering, problems, merges.

The patient record answers "how many times has this person been here, and
what for?". The rules worth pinning down are the ones a plausible-looking
history gets wrong: a cancelled booking counted as a visit, a merged
duplicate's visits silently missing, an admitted OPD visit not counted as an
admission, a follow-up offered to someone who has died, and reception being
shown diagnoses its role may not read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import NotFoundError
from app.core.pagination import PageParams
from app.modules.clinical import service
from app.modules.clinical.models import (
    Diagnosis,
    DiagnosisCertainty,
    DiagnosisType,
    Encounter,
    EncounterStatus,
    OrderType,
)
from app.modules.clinical.schemas import DiagnosisCreate, OrderCreate, VitalsCreate
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister, PatientSort
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

S = EncounterStatus
TODAY = date(2026, 10, 6)


# ---------------------------------------------------------------------------
# The problem list — pure, no database
# ---------------------------------------------------------------------------
def _dx(
    description: str,
    *,
    code: str | None = None,
    encounter_id: uuid.UUID | None = None,
    days_ago: int = 0,
    diagnosis_type: DiagnosisType = DiagnosisType.FINAL,
    certainty: DiagnosisCertainty = DiagnosisCertainty.CONFIRMED,
) -> Diagnosis:
    return Diagnosis(
        hospital_id=uuid.uuid4(),
        encounter_id=encounter_id or uuid.uuid4(),
        patient_id=uuid.uuid4(),
        description=description,
        code=code,
        diagnosis_type=diagnosis_type,
        certainty=certainty,
        diagnosed_by_id=uuid.uuid4(),
        diagnosed_at=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(days=days_ago),
    )


class TestProblemList:
    def test_one_condition_on_several_visits_is_one_problem(self) -> None:
        problems = service.build_problem_list(
            [
                _dx("Type 2 diabetes", code="E11", days_ago=0),
                _dx("Type 2 diabetes mellitus", code="e11 ", days_ago=90),
                _dx("T2DM", code="E11", days_ago=200),
            ]
        )
        assert len(problems) == 1
        assert problems[0].visit_count == 3
        assert problems[0].code == "E11"
        # The newest wording is the one shown.
        assert problems[0].description == "Type 2 diabetes"
        assert problems[0].first_recorded_at < problems[0].last_recorded_at

    def test_uncoded_diagnoses_group_by_wording_ignoring_case_and_spaces(self) -> None:
        problems = service.build_problem_list(
            [_dx("Essential  hypertension"), _dx("essential hypertension", days_ago=30)]
        )
        assert [problem.visit_count for problem in problems] == [2]

    def test_two_diagnoses_on_one_visit_count_that_visit_once(self) -> None:
        visit = uuid.uuid4()
        problems = service.build_problem_list(
            [
                _dx("Asthma", code="J45", encounter_id=visit),
                _dx("Asthma", code="J45", encounter_id=visit),
            ]
        )
        assert problems[0].visit_count == 1

    def test_differentials_and_ruled_out_calls_are_not_history(self) -> None:
        """A problem list is what the patient has, not what was wondered about."""
        problems = service.build_problem_list(
            [
                _dx("Appendicitis", diagnosis_type=DiagnosisType.DIFFERENTIAL),
                _dx("Tuberculosis", certainty=DiagnosisCertainty.RULED_OUT),
                _dx("Gastritis"),
            ]
        )
        assert [problem.description for problem in problems] == ["Gastritis"]

    def test_the_latest_call_wins_the_type(self) -> None:
        problems = service.build_problem_list(
            [
                _dx("Dengue", code="A90", diagnosis_type=DiagnosisType.PROVISIONAL, days_ago=3),
                _dx("Dengue", code="A90", diagnosis_type=DiagnosisType.FINAL, days_ago=1),
            ]
        )
        assert problems[0].diagnosis_type is DiagnosisType.FINAL

    def test_the_most_recent_problem_is_listed_first(self) -> None:
        problems = service.build_problem_list(
            [_dx("Old fracture", days_ago=400), _dx("Current cough", days_ago=2)]
        )
        assert [problem.description for problem in problems] == ["Current cough", "Old fracture"]


# ---------------------------------------------------------------------------
# Service, against the database
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


@pytest.fixture
async def doctor(session: AsyncSession, tenant: Hospital) -> User:
    user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


async def _register(session: AsyncSession, hospital: Hospital, name: str, phone: str) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(full_name=name, phone=phone, gender=Gender.FEMALE, age_years=52),
        hospital_id=hospital.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    return await _register(session, tenant, "Kamla Devi", "9811122233")


async def _visit(
    session: AsyncSession,
    hospital: Hospital,
    patient: Patient,
    doctor: User,
    *,
    complaint: str,
    started_days_ago: int,
) -> Encounter:
    encounter = await service.open_encounter(
        session,
        hospital_id=hospital.id,
        patient_id=patient.id,
        actor=doctor,
        chief_complaint=complaint,
    )
    # Back-dated so the order is deterministic, not an accident of timing.
    encounter.started_at = datetime.now(UTC) - timedelta(days=started_days_ago)
    session.add(encounter)
    await session.commit()
    return encounter


async def _complete(
    session: AsyncSession, encounter: Encounter, doctor: User, *, follow_up: date | None = None
) -> None:
    await service.start_consultation(session, encounter, actor=doctor)
    await service.complete_consultation(session, encounter, actor=doctor, follow_up_date=follow_up)
    await session.commit()


@pytest.mark.integration
class TestSummary:
    async def test_counts_visits_but_not_cancelled_bookings(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        first = await _visit(
            session, tenant, patient, doctor, complaint="Cough", started_days_ago=60
        )
        await _complete(session, first, doctor)
        skipped = await _visit(session, tenant, patient, doctor, complaint="—", started_days_ago=30)
        await service.cancel_encounter(
            session, skipped, reason="Patient did not come", no_show=True
        )
        await session.commit()
        current = await _visit(
            session, tenant, patient, doctor, complaint="Fever", started_days_ago=0
        )

        summary = await service.build_patient_summary(
            session,
            patient.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=True,
            include_vitals=True,
        )

        assert summary.total_visits == 2
        assert summary.opd_visits == 2
        assert summary.inpatient_visits == 0
        assert summary.first_visit_at == first.started_at
        assert summary.last_visit_at == current.started_at
        assert summary.open_visit is not None
        assert summary.open_visit.id == current.id

    async def test_an_admitted_opd_visit_counts_as_an_admission_after_discharge(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        """It keeps its OPD type and reads COMPLETED, so the trail is the evidence."""
        visit = await _visit(
            session, tenant, patient, doctor, complaint="Chest pain", started_days_ago=5
        )
        await service.admit(session, visit, actor=doctor)
        await service.discharge(session, visit, actor=doctor)
        await session.commit()

        summary = await service.build_patient_summary(
            session,
            patient.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=False,
            include_vitals=False,
        )
        assert summary.inpatient_visits == 1
        assert summary.open_visit is None

    async def test_next_follow_up_is_the_earliest_one_still_ahead(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        past = await _visit(session, tenant, patient, doctor, complaint="BP", started_days_ago=40)
        await _complete(session, past, doctor, follow_up=TODAY - timedelta(days=10))
        near = await _visit(session, tenant, patient, doctor, complaint="BP", started_days_ago=10)
        await _complete(session, near, doctor, follow_up=TODAY + timedelta(days=7))
        far = await _visit(session, tenant, patient, doctor, complaint="BP", started_days_ago=2)
        await _complete(session, far, doctor, follow_up=TODAY + timedelta(days=30))

        summary = await service.build_patient_summary(
            session,
            patient.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=False,
            include_vitals=False,
        )
        assert summary.next_follow_up_date == TODAY + timedelta(days=7)
        assert summary.next_follow_up_encounter_id == near.id

    async def test_a_deceased_patient_is_never_offered_a_follow_up(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        """CLAUDE.md §6: no recall for a patient who has died."""
        visit = await _visit(session, tenant, patient, doctor, complaint="BP", started_days_ago=3)
        await _complete(session, visit, doctor, follow_up=TODAY + timedelta(days=14))
        patient.is_deceased = True
        session.add(patient)
        await session.commit()

        summary = await service.build_patient_summary(
            session,
            patient.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=False,
            include_vitals=False,
        )
        assert summary.next_follow_up_date is None

    async def test_problems_and_vitals_appear_only_when_asked_for(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        visit = await _visit(
            session, tenant, patient, doctor, complaint="Thirst", started_days_ago=1
        )
        await service.record_diagnosis(
            session,
            visit,
            DiagnosisCreate(
                description="Type 2 diabetes", code="E11", diagnosis_type=DiagnosisType.FINAL
            ),
            actor=doctor,
        )
        await service.record_vitals(session, visit, VitalsCreate(pulse_bpm=88), actor=doctor)
        await session.commit()

        shown = await service.build_patient_summary(
            session,
            patient.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=True,
            include_vitals=True,
        )
        assert shown.problems is not None
        assert [problem.code for problem in shown.problems] == ["E11"]
        assert shown.latest_vitals is not None
        assert shown.latest_vitals.pulse_bpm == 88

        hidden = await service.build_patient_summary(
            session,
            patient.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=False,
            include_vitals=False,
        )
        # None — "not for your role" — never an empty list that reads "healthy".
        assert hidden.problems is None
        assert hidden.latest_vitals is None

    async def test_another_hospitals_patient_is_not_found(
        self, session: AsyncSession, tenant: Hospital, patient: Patient
    ) -> None:
        with pytest.raises(NotFoundError):
            await service.build_patient_summary(
                session,
                patient.id,
                hospital_id=uuid.uuid4(),
                today=TODAY,
                include_problems=True,
                include_vitals=True,
            )


@pytest.mark.integration
class TestVisits:
    async def test_visits_are_newest_first_and_numbered_from_the_first(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        await _visit(session, tenant, patient, doctor, complaint="One", started_days_ago=90)
        cancelled = await _visit(
            session, tenant, patient, doctor, complaint="Two", started_days_ago=60
        )
        await service.cancel_encounter(session, cancelled, reason="Called off")
        await session.commit()
        await _visit(session, tenant, patient, doctor, complaint="Three", started_days_ago=1)

        visits, total = await service.list_patient_visits(
            session,
            PageParams(limit=10),
            patient.id,
            hospital_id=tenant.id,
            include_diagnoses=False,
            include_orders=False,
        )
        assert total == 3
        assert [visit.chief_complaint for visit in visits] == ["Three", "Two", "One"]
        # The cancelled booking is listed but is not a visit.
        assert [visit.visit_number for visit in visits] == [2, None, 1]

    async def test_numbering_holds_across_pages(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        for days_ago in (30, 20, 10):
            await _visit(
                session, tenant, patient, doctor, complaint=str(days_ago), started_days_ago=days_ago
            )

        second_page, _ = await service.list_patient_visits(
            session,
            PageParams(limit=2, offset=2),
            patient.id,
            hospital_id=tenant.id,
            include_diagnoses=False,
            include_orders=False,
        )
        assert [visit.visit_number for visit in second_page] == [1]

    async def test_each_visit_carries_what_it_was_for(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        visit = await _visit(
            session, tenant, patient, doctor, complaint="Fever", started_days_ago=1
        )
        await service.record_diagnosis(
            session,
            visit,
            DiagnosisCreate(description="Viral fever", is_primary=True),
            actor=doctor,
        )
        await service.place_order(
            session, visit, OrderCreate(order_type=OrderType.LAB, item_name="CBC"), actor=doctor
        )
        await session.commit()

        shown, _ = await service.list_patient_visits(
            session,
            PageParams(limit=10),
            patient.id,
            hospital_id=tenant.id,
            include_diagnoses=True,
            include_orders=True,
        )
        assert shown[0].diagnoses is not None
        assert [dx.description for dx in shown[0].diagnoses] == ["Viral fever"]
        assert shown[0].orders is not None
        assert [order.item_name for order in shown[0].orders] == ["CBC"]

        hidden, _ = await service.list_patient_visits(
            session,
            PageParams(limit=10),
            patient.id,
            hospital_id=tenant.id,
            include_diagnoses=False,
            include_orders=False,
        )
        assert hidden[0].diagnoses is None
        assert hidden[0].orders is None


@pytest.mark.integration
class TestMergedRecords:
    async def test_a_merged_duplicates_visits_join_the_surviving_history(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        """The split record a merge exists to repair must not reappear here."""
        duplicate = await _register(session, tenant, "Kamala Devi", "9811122299")
        await _visit(
            session, tenant, duplicate, doctor, complaint="Old visit", started_days_ago=100
        )
        await _visit(session, tenant, patient, doctor, complaint="New visit", started_days_ago=1)
        await patients_service.merge_patients(
            session, survivor=patient, duplicate=duplicate, reason="Same person, typo in name"
        )
        await session.commit()

        summary = await service.build_patient_summary(
            session,
            patient.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=False,
            include_vitals=False,
        )
        assert summary.total_visits == 2

        visits, _ = await service.list_patient_visits(
            session,
            PageParams(limit=10),
            patient.id,
            hospital_id=tenant.id,
            include_diagnoses=False,
            include_orders=False,
        )
        assert [visit.chief_complaint for visit in visits] == ["New visit", "Old visit"]

        stats = await service.visit_stats_for_patients(session, [patient.id], hospital_id=tenant.id)
        assert stats[0].total_visits == 2

    async def test_opening_the_duplicate_shows_the_survivors_history(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        duplicate = await _register(session, tenant, "Kamala Devi", "9811122299")
        await _visit(session, tenant, patient, doctor, complaint="Visit", started_days_ago=1)
        await patients_service.merge_patients(
            session, survivor=patient, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        summary = await service.build_patient_summary(
            session,
            duplicate.id,
            hospital_id=tenant.id,
            today=TODAY,
            include_problems=False,
            include_vitals=False,
        )
        assert summary.patient_id == patient.id
        assert summary.total_visits == 1


@pytest.mark.integration
class TestDirectoryStats:
    async def test_one_row_per_patient_asked_for_including_the_never_seen(
        self, session: AsyncSession, tenant: Hospital, patient: Patient, doctor: User
    ) -> None:
        newcomer = await _register(session, tenant, "Ravi Kumar", "9822233344")
        await _visit(session, tenant, patient, doctor, complaint="A", started_days_ago=9)
        latest = await _visit(session, tenant, patient, doctor, complaint="B", started_days_ago=2)

        stats = {
            row.patient_id: row
            for row in await service.visit_stats_for_patients(
                session, [patient.id, newcomer.id], hospital_id=tenant.id
            )
        }
        assert stats[patient.id].total_visits == 2
        assert stats[patient.id].last_visit_at == latest.started_at
        assert stats[patient.id].last_visit_status is S.REGISTERED
        assert stats[newcomer.id].total_visits == 0
        assert stats[newcomer.id].last_visit_at is None


# ---------------------------------------------------------------------------
# Over HTTP: role gating and audit
# ---------------------------------------------------------------------------
PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
QUEUE = "/api/v1/queue"
ENCOUNTERS = "/api/v1/encounters"
AUDIT = "/api/v1/audit-logs"


@pytest.mark.integration
class TestOverHttp:
    @pytest.fixture
    async def setup(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        hospital_admin: User,
        receptionist: User,
    ) -> dict[str, str]:
        doctor_user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
        admin_token = await login(api, hospital_admin)
        reception_token = await login(api, receptionist)
        doctor_token = await login(api, doctor_user)

        doctor = await api.post(
            DOCTORS,
            json={"user_id": str(doctor_user.id), "specialty": "General Medicine"},
            headers=auth(admin_token),
        )
        assert doctor.status_code == 201, doctor.text
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
        visit = await api.post(
            f"{QUEUE}/quick-opd",
            json={
                "patient_id": patient.json()["id"],
                "doctor_id": doctor.json()["id"],
                "reason": "Fever",
            },
            headers=auth(reception_token),
        )
        assert visit.status_code == 201, visit.text
        diagnosis = await api.post(
            f"{ENCOUNTERS}/{visit.json()['encounter_id']}/diagnoses",
            json={
                "description": "Dengue fever",
                "code": "A90",
                "is_primary": True,
                "diagnosis_type": "FINAL",
            },
            headers=auth(doctor_token),
        )
        assert diagnosis.status_code == 201, diagnosis.text
        return {
            "patient_id": patient.json()["id"],
            "admin": admin_token,
            "reception": reception_token,
            "doctor": doctor_token,
            "doctor_name": doctor.json()["display_name"],
        }

    async def test_a_doctor_sees_what_the_patient_was_treated_for(
        self, api: AsyncClient, setup: dict[str, str]
    ) -> None:
        summary = await api.get(
            f"{ENCOUNTERS}/patient/{setup['patient_id']}/summary", headers=auth(setup["doctor"])
        )
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["total_visits"] == 1
        assert [problem["description"] for problem in body["problems"]] == ["Dengue fever"]

        visits = await api.get(
            f"{ENCOUNTERS}/patient/{setup['patient_id']}/visits", headers=auth(setup["doctor"])
        )
        assert visits.status_code == 200, visits.text
        row = visits.json()["items"][0]
        assert row["visit_number"] == 1
        assert row["doctor_name"] == setup["doctor_name"]
        assert [dx["code"] for dx in row["diagnoses"]] == ["A90"]

    async def test_reception_sees_the_visits_but_not_the_chart(
        self, api: AsyncClient, setup: dict[str, str]
    ) -> None:
        summary = (
            await api.get(
                f"{ENCOUNTERS}/patient/{setup['patient_id']}/summary",
                headers=auth(setup["reception"]),
            )
        ).json()
        assert summary["total_visits"] == 1
        assert summary["problems"] is None
        assert summary["latest_vitals"] is None

        row = (
            await api.get(
                f"{ENCOUNTERS}/patient/{setup['patient_id']}/visits",
                headers=auth(setup["reception"]),
            )
        ).json()["items"][0]
        assert row["diagnoses"] is None
        assert row["doctor_name"] == setup["doctor_name"]

    async def test_reading_a_history_is_audited(
        self, api: AsyncClient, setup: dict[str, str]
    ) -> None:
        await api.get(
            f"{ENCOUNTERS}/patient/{setup['patient_id']}/summary", headers=auth(setup["doctor"])
        )
        entries = (
            await api.get(
                AUDIT, params={"action": "patient.history.view"}, headers=auth(setup["admin"])
            )
        ).json()
        assert entries["total"] >= 1
        assert entries["items"][0]["resource_id"] == setup["patient_id"]

    async def test_visit_stats_are_bounded_by_the_page_size(
        self, api: AsyncClient, setup: dict[str, str]
    ) -> None:
        ok = await api.get(
            f"{ENCOUNTERS}/visit-stats",
            params={"patient_id": [setup["patient_id"]]},
            headers=auth(setup["reception"]),
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()[0]["total_visits"] == 1

        too_many = await api.get(
            f"{ENCOUNTERS}/visit-stats",
            params={"patient_id": [str(uuid.uuid4()) for _ in range(201)]},
            headers=auth(setup["reception"]),
        )
        assert too_many.status_code == 422

    async def test_another_hospital_cannot_read_the_history(
        self, api: AsyncClient, session: AsyncSession, setup: dict[str, str]
    ) -> None:
        async with db.system_context(session):
            other = Hospital(code=f"H{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()
        outsider = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=other_id)
        token = await login(api, outsider)

        response = await api.get(
            f"{ENCOUNTERS}/patient/{setup['patient_id']}/summary", headers=auth(token)
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# The directory: filters and ordering
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestDirectory:
    async def test_filters_by_gender_and_by_whether_the_patient_has_died(
        self, session: AsyncSession, tenant: Hospital, patient: Patient
    ) -> None:
        man, _ = await patients_service.register_patient(
            session,
            PatientRegister(
                full_name="Arjun Rao", phone="9800000001", gender=Gender.MALE, age_years=40
            ),
            hospital_id=tenant.id,
        )
        man.is_deceased = True
        session.add(man)
        await session.commit()

        women, _ = await patients_service.list_patients(
            session, PageParams(limit=10), hospital_id=tenant.id, gender=Gender.FEMALE
        )
        assert [row.id for row in women] == [patient.id]

        dead, _ = await patients_service.list_patients(
            session, PageParams(limit=10), hospital_id=tenant.id, deceased=True
        )
        assert [row.id for row in dead] == [man.id]

    async def test_sorts_by_name_without_regard_to_case(
        self, session: AsyncSession, tenant: Hospital, patient: Patient
    ) -> None:
        await _register(session, tenant, "amit shah", "9800000002")
        await _register(session, tenant, "Zoya Khan", "9800000003")

        rows, _ = await patients_service.list_patients(
            session, PageParams(limit=10), hospital_id=tenant.id, sort=PatientSort.NAME
        )
        assert [row.full_name for row in rows] == ["amit shah", "Kamla Devi", "Zoya Khan"]

    async def test_registration_dates_are_the_hospitals_days_not_utc(
        self, session: AsyncSession, tenant: Hospital, patient: Patient
    ) -> None:
        """19:00 UTC on the 4th is 00:30 IST on the 5th — a patient of the 5th."""
        patient.registered_at = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
        session.add(patient)
        await session.commit()

        on_fifth, _ = await patients_service.list_patients(
            session,
            PageParams(limit=10),
            hospital_id=tenant.id,
            registered_from=date(2026, 10, 5),
            registered_to=date(2026, 10, 5),
        )
        on_fourth, _ = await patients_service.list_patients(
            session,
            PageParams(limit=10),
            hospital_id=tenant.id,
            registered_from=date(2026, 10, 4),
            registered_to=date(2026, 10, 4),
        )
        assert [row.id for row in on_fifth] == [patient.id]
        assert on_fourth == []

    async def test_a_merged_duplicate_is_not_listed(
        self, session: AsyncSession, tenant: Hospital, patient: Patient
    ) -> None:
        duplicate = await _register(session, tenant, "Kamala Devi", "9811122299")
        await patients_service.merge_patients(
            session, survivor=patient, duplicate=duplicate, reason="Same person"
        )
        await session.commit()

        rows, total = await patients_service.list_patients(
            session, PageParams(limit=10), hospital_id=tenant.id
        )
        assert total == 1
        assert [row.id for row in rows] == [patient.id]


@pytest.mark.integration
async def test_a_backwards_date_range_is_refused(api: AsyncClient, receptionist: User) -> None:
    token = await login(api, receptionist)
    response = await api.get(
        PATIENTS,
        params={"registered_from": "2026-10-05", "registered_to": "2026-10-01"},
        headers=auth(token),
    )
    assert response.status_code == 422
