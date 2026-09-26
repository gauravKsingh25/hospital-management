"""The OPD → admission desk hand-off.

Reception marks a patient seen, sends them for admission; the admission desk
admits them from its own list, and the ordinary IPD flow takes over. Three
properties carry the weight, and each guards a real failure:

* **A patient waiting at the desk holds their OPD visit open.** Otherwise the
  doctor's "complete visit" — or the night's auto-close — ends the visit, and
  the desk is left trying to admit from a closed one.
* **An already-closed visit still admits.** The stay opens a new IPD visit
  rather than reopening a terminal one (CLAUDE.md §6).
* **Each side of the hand-off holds only its own half.** Reception can send
  but not work the desk; the desk can admit; an administrator can do both.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import ConflictError
from app.core.pagination import PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter, EncounterStatus, EncounterType
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service
from app.modules.ipd.models import AdmissionRequestStatus, AdmissionStatus, BedClass
from app.modules.ipd.schemas import AdmitFromRequest, BedCreate, WardCreate
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    return hospital


async def _user(session: AsyncSession, tenant: Hospital, role: str) -> User:
    user = await _make_user(session, role_code=role, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def reception_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.RECEPTIONIST)


@pytest.fixture
async def desk_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.ADMISSION_DESK)


@pytest.fixture
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    return await _user(session, tenant, Roles.DOCTOR)


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Mohammed Irfan", phone="9812399001", gender=Gender.MALE, age_years=45
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
async def bed_id(session: AsyncSession, tenant: Hospital) -> str:
    ward = await service.create_ward(
        session,
        WardCreate(code="GW1", name="General Ward", bed_class=BedClass.GENERAL),
        hospital_id=tenant.id,
    )
    bed = await service.create_bed(
        session,
        BedCreate(ward_id=ward.id, code="GW1-01", tariff_item_code="ROOM-GEN"),
        hospital_id=tenant.id,
    )
    await session.commit()
    return str(bed.id)


@pytest.fixture
async def seen_visit(
    session: AsyncSession, tenant: Hospital, patient: Patient, reception_user: User
) -> Encounter:
    """A visit reception has marked seen: in consultation, not yet closed."""
    encounter = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=reception_user
    )
    await clinical_service.record_seen_by_staff(session, encounter, actor=reception_user)
    await session.commit()
    return encounter


def _admit(bed_id: str) -> AdmitFromRequest:
    import uuid

    return AdmitFromRequest(bed_id=uuid.UUID(bed_id), provisional_diagnosis="Unstable angina")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class TestSendingAPatient:
    async def test_a_seen_patient_is_sent_to_the_desk(
        self, session: AsyncSession, tenant: Hospital, seen_visit: Encounter, reception_user: User
    ) -> None:
        request = await service.request_admission(
            session,
            hospital_id=tenant.id,
            encounter_id=seen_visit.id,
            note="Doctor advised ICU bed",
            actor=reception_user,
        )
        await session.commit()

        assert request.status is AdmissionRequestStatus.PENDING
        assert request.patient_id == seen_visit.patient_id
        assert request.requested_by_id == reception_user.id
        assert request.requested_by_name == reception_user.full_name

        waiting, total = await service.list_admission_requests(
            session,
            PageParams(limit=50, offset=0),
            hospital_id=tenant.id,
            statuses=[AdmissionRequestStatus.PENDING],
        )
        assert total == 1 and waiting[0].id == request.id

    async def test_a_patient_cannot_be_sent_twice(
        self, session: AsyncSession, tenant: Hospital, seen_visit: Encounter
    ) -> None:
        await service.request_admission(session, hospital_id=tenant.id, encounter_id=seen_visit.id)
        await session.commit()
        with pytest.raises(ConflictError) as excinfo:
            await service.request_admission(
                session, hospital_id=tenant.id, encounter_id=seen_visit.id
            )
        assert excinfo.value.code == "admission_already_requested"

    async def test_a_visit_that_ended_in_referral_cannot_be_sent(
        self, session: AsyncSession, tenant: Hospital, seen_visit: Encounter, doctor_user: User
    ) -> None:
        from app.modules.clinical.schemas import ReferralRecord

        await clinical_service.record_referral(
            session,
            seen_visit,
            ReferralRecord(referred_to_facility="AIIMS Delhi", referral_reason="Cardiac surgery"),
            actor=doctor_user,
        )
        await session.commit()
        with pytest.raises(ConflictError) as excinfo:
            await service.request_admission(
                session, hospital_id=tenant.id, encounter_id=seen_visit.id
            )
        assert excinfo.value.code == "encounter_not_admittable"


class TestTheDeskAdmits:
    async def test_admitting_starts_the_ipd_flow_on_the_same_visit(
        self,
        session: AsyncSession,
        tenant: Hospital,
        seen_visit: Encounter,
        desk_user: User,
        bed_id: str,
    ) -> None:
        request = await service.request_admission(
            session, hospital_id=tenant.id, encounter_id=seen_visit.id
        )
        admission = await service.admit_from_request(
            session, request, _admit(bed_id), actor=desk_user
        )
        await session.commit()

        assert admission.status is AdmissionStatus.ADMITTED
        # The chart carries straight on into the stay.
        assert admission.encounter_id == seen_visit.id
        await session.refresh(seen_visit)
        assert seen_visit.status is EncounterStatus.ADMITTED
        assert seen_visit.encounter_type is EncounterType.IPD

        await session.refresh(request)
        assert request.status is AdmissionRequestStatus.ADMITTED
        assert request.admission_id == admission.id
        assert request.handled_by_id == desk_user.id

    async def test_a_visit_the_doctor_already_closed_opens_a_new_ipd_visit(
        self,
        session: AsyncSession,
        tenant: Hospital,
        seen_visit: Encounter,
        doctor_user: User,
        desk_user: User,
        bed_id: str,
    ) -> None:
        """Terminal is terminal: the closed OPD visit is not reopened."""
        await clinical_service.complete_consultation(session, seen_visit, actor=doctor_user)
        await session.commit()
        assert seen_visit.status is EncounterStatus.COMPLETED

        request = await service.request_admission(
            session, hospital_id=tenant.id, encounter_id=seen_visit.id
        )
        admission = await service.admit_from_request(
            session, request, _admit(bed_id), actor=desk_user
        )
        await session.commit()

        assert admission.encounter_id != seen_visit.id
        stay = await clinical_service.get_encounter(
            session, admission.encounter_id, hospital_id=tenant.id
        )
        assert stay.status is EncounterStatus.ADMITTED
        assert stay.encounter_type is EncounterType.IPD
        await session.refresh(seen_visit)
        assert seen_visit.status is EncounterStatus.COMPLETED

    async def test_admitting_directly_also_clears_the_request(
        self,
        session: AsyncSession,
        tenant: Hospital,
        seen_visit: Encounter,
        doctor_user: User,
        bed_id: str,
    ) -> None:
        """A doctor admitting from the consultation screen must not leave the
        patient sitting on the desk's list after they are in a bed."""
        import uuid

        from app.modules.ipd.schemas import AdmissionCreate

        request = await service.request_admission(
            session, hospital_id=tenant.id, encounter_id=seen_visit.id
        )
        admission = await service.admit_patient(
            session,
            AdmissionCreate(encounter_id=seen_visit.id, bed_id=uuid.UUID(bed_id)),
            hospital_id=tenant.id,
            actor=doctor_user,
        )
        await session.commit()
        await session.refresh(request)
        assert request.status is AdmissionRequestStatus.ADMITTED
        assert request.admission_id == admission.id

    async def test_an_inpatient_cannot_be_sent_again(
        self,
        session: AsyncSession,
        tenant: Hospital,
        seen_visit: Encounter,
        desk_user: User,
        bed_id: str,
    ) -> None:
        request = await service.request_admission(
            session, hospital_id=tenant.id, encounter_id=seen_visit.id
        )
        await service.admit_from_request(session, request, _admit(bed_id), actor=desk_user)
        await session.commit()
        with pytest.raises(ConflictError):
            await service.request_admission(
                session, hospital_id=tenant.id, encounter_id=seen_visit.id
            )

    async def test_a_request_is_admitted_only_once(
        self,
        session: AsyncSession,
        tenant: Hospital,
        seen_visit: Encounter,
        desk_user: User,
        bed_id: str,
    ) -> None:
        request = await service.request_admission(
            session, hospital_id=tenant.id, encounter_id=seen_visit.id
        )
        await service.admit_from_request(session, request, _admit(bed_id), actor=desk_user)
        await session.commit()
        with pytest.raises(ConflictError) as excinfo:
            await service.admit_from_request(session, request, _admit(bed_id), actor=desk_user)
        assert excinfo.value.code == "admission_request_closed"


class TestTheVisitIsHeldOpen:
    async def test_the_doctor_completing_the_visit_leaves_it_admittable(
        self,
        session: AsyncSession,
        tenant: Hospital,
        seen_visit: Encounter,
        doctor_user: User,
    ) -> None:
        await service.request_admission(session, hospital_id=tenant.id, encounter_id=seen_visit.id)
        completed = await clinical_service.complete_consultation(
            session, seen_visit, actor=doctor_user
        )
        await session.commit()
        # Held for the desk, not closed.
        assert completed.status is EncounterStatus.PENDING_CLEARANCE

    async def test_the_night_auto_close_does_not_close_it(
        self, session: AsyncSession, tenant: Hospital, seen_visit: Encounter
    ) -> None:
        await service.request_admission(session, hospital_id=tenant.id, encounter_id=seen_visit.id)
        await session.commit()

        result = await clinical_service.close_stale_encounters(
            session, hospital_id=tenant.id, older_than=timedelta(0)
        )
        await session.commit()
        assert result["held_for_staff"] == 1
        await session.refresh(seen_visit)
        assert seen_visit.status is EncounterStatus.IN_CONSULTATION

    async def test_turning_the_request_away_lets_the_visit_close(
        self,
        session: AsyncSession,
        tenant: Hospital,
        seen_visit: Encounter,
        doctor_user: User,
        desk_user: User,
    ) -> None:
        request = await service.request_admission(
            session, hospital_id=tenant.id, encounter_id=seen_visit.id
        )
        await clinical_service.complete_consultation(session, seen_visit, actor=doctor_user)
        await session.commit()
        assert seen_visit.status is EncounterStatus.PENDING_CLEARANCE

        cancelled = await service.cancel_admission_request(
            session, request, reason="Family chose another hospital", actor=desk_user
        )
        await session.commit()

        assert cancelled.status is AdmissionRequestStatus.CANCELLED
        assert cancelled.cancellation_reason == "Family chose another hospital"
        await session.refresh(seen_visit)
        assert seen_visit.status is EncounterStatus.COMPLETED


# ---------------------------------------------------------------------------
# API and RBAC
# ---------------------------------------------------------------------------
REQUESTS = "/api/v1/ipd/admission-requests"
PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
QUEUE = "/api/v1/queue"
ENCOUNTERS = "/api/v1/encounters"


async def _seen_patient(api: AsyncClient, reception: str, admin: str, doctor: User) -> str:
    """Register, send to a doctor, mark seen — the real reception path.
    Returns the encounter id."""
    made = await api.post(DOCTORS, json={"user_id": str(doctor.id)}, headers=auth(admin))
    assert made.status_code == 201, made.text
    registered = await api.post(
        PATIENTS,
        json={
            "full_name": "Lakshmi Devi",
            "phone": "9812399002",
            "gender": "FEMALE",
            "age_years": 66,
        },
        headers=auth(reception),
    )
    assert registered.status_code == 201, registered.text
    opd = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": registered.json()["id"], "doctor_id": made.json()["id"]},
        headers=auth(reception),
    )
    assert opd.status_code == 201, opd.text
    seen = await api.post(
        f"{QUEUE}/{opd.json()['queue_entry']['id']}/seen", headers=auth(reception)
    )
    assert seen.status_code == 200, seen.text
    return str(opd.json()["encounter_id"])


class TestTheHandOffOverHttp:
    async def test_reception_sends_and_the_desk_admits(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        receptionist: User,
        hospital_admin: User,
        bed_id: str,
    ) -> None:
        doctor = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
        desk = await _make_user(session, role_code=Roles.ADMISSION_DESK, hospital_id=hospital.id)
        reception_token = await login(api, receptionist)
        admin_token = await login(api, hospital_admin)
        desk_token = await login(api, desk)

        encounter_id = await _seen_patient(api, reception_token, admin_token, doctor)

        sent = await api.post(
            REQUESTS,
            json={"encounter_id": encounter_id, "note": "Advised admission"},
            headers=auth(reception_token),
        )
        assert sent.status_code == 201, sent.text
        request_id = sent.json()["id"]
        assert sent.json()["patient_name"] == "Lakshmi Devi"

        # Reception cannot work the desk…
        refused = await api.post(
            f"{REQUESTS}/{request_id}/admit", json={"bed_id": bed_id}, headers=auth(reception_token)
        )
        assert refused.status_code == 403, refused.text

        # …the desk sees the patient waiting, and admits.
        waiting = await api.get(REQUESTS, params={"status": "PENDING"}, headers=auth(desk_token))
        assert waiting.status_code == 200, waiting.text
        assert [row["id"] for row in waiting.json()["items"]] == [request_id]

        admitted = await api.post(
            f"{REQUESTS}/{request_id}/admit",
            json={"bed_id": bed_id, "provisional_diagnosis": "Syncope"},
            headers=auth(desk_token),
        )
        assert admitted.status_code == 201, admitted.text
        assert admitted.json()["status"] == "ADMITTED"
        assert admitted.json()["encounter_id"] == encounter_id

        # And reception can see what became of the patient it sent.
        mine = await api.get(REQUESTS, headers=auth(reception_token))
        [row] = mine.json()["items"]
        assert row["status"] == "ADMITTED"
        assert row["admission_id"] == admitted.json()["id"]

        visit = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(desk_token))
        assert visit.json()["status"] == "ADMITTED"

    async def test_an_administrator_can_do_both_halves(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        receptionist: User,
        hospital_admin: User,
        bed_id: str,
    ) -> None:
        doctor = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
        reception_token = await login(api, receptionist)
        admin_token = await login(api, hospital_admin)
        encounter_id = await _seen_patient(api, reception_token, admin_token, doctor)

        sent = await api.post(
            REQUESTS, json={"encounter_id": encounter_id}, headers=auth(admin_token)
        )
        assert sent.status_code == 201, sent.text
        admitted = await api.post(
            f"{REQUESTS}/{sent.json()['id']}/admit",
            json={"bed_id": bed_id},
            headers=auth(admin_token),
        )
        assert admitted.status_code == 201, admitted.text

    async def test_a_role_without_the_permission_cannot_send_or_see(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        receptionist: User,
        hospital_admin: User,
    ) -> None:
        doctor = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
        cashier = await _make_user(session, role_code=Roles.CASHIER, hospital_id=hospital.id)
        reception_token = await login(api, receptionist)
        admin_token = await login(api, hospital_admin)
        cashier_token = await login(api, cashier)
        encounter_id = await _seen_patient(api, reception_token, admin_token, doctor)

        sent = await api.post(
            REQUESTS, json={"encounter_id": encounter_id}, headers=auth(cashier_token)
        )
        assert sent.status_code == 403, sent.text
        listed = await api.get(REQUESTS, headers=auth(cashier_token))
        assert listed.status_code == 403, listed.text

    async def test_turning_a_request_away_needs_a_reason(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        receptionist: User,
        hospital_admin: User,
    ) -> None:
        doctor = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
        desk = await _make_user(session, role_code=Roles.ADMISSION_DESK, hospital_id=hospital.id)
        reception_token = await login(api, receptionist)
        admin_token = await login(api, hospital_admin)
        desk_token = await login(api, desk)
        encounter_id = await _seen_patient(api, reception_token, admin_token, doctor)
        sent = await api.post(
            REQUESTS, json={"encounter_id": encounter_id}, headers=auth(reception_token)
        )
        request_id = sent.json()["id"]

        empty = await api.post(
            f"{REQUESTS}/{request_id}/cancel", json={"reason": ""}, headers=auth(desk_token)
        )
        assert empty.status_code == 422, empty.text

        cancelled = await api.post(
            f"{REQUESTS}/{request_id}/cancel",
            json={"reason": "Sent by mistake"},
            headers=auth(desk_token),
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "CANCELLED"
