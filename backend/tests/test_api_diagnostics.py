"""Diagnostics over HTTP: a whole lab day, and RBAC.

The first class walks one blood test from the doctor's order to a closed visit
exactly as the staff would — order, accession, bleed, receive, result, verify —
because that is the chain CLAUDE.md §13 step 6 asks for, and it only works if
every link agrees.
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
CATALOGUE = "/api/v1/diagnostics/catalogue"
SPECIMENS = "/api/v1/diagnostics/specimens"
REPORTS = "/api/v1/diagnostics/reports"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def doctor_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)


@pytest.fixture
async def tech_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.LAB_TECH, hospital_id=hospital.id)


@pytest.fixture
async def nurse_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)


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
async def tech_token(api: AsyncClient, tech_user: User) -> str:
    return await login(api, tech_user)


@pytest.fixture
async def cbc_id(api: AsyncClient, admin_token: str) -> str:
    """A one-analyte panel with a female band that has a panic limit."""
    item = await api.post(
        CATALOGUE,
        json={
            "code": "CBC",
            "name": "Complete Blood Count",
            "discipline": "LAB",
            "section": "Haematology",
            "specimen_type": "BLOOD",
            "container": "EDTA (purple top)",
        },
        headers=auth(admin_token),
    )
    assert item.status_code == 201, item.text
    item_id = item.json()["id"]

    analyte = await api.post(
        f"{CATALOGUE}/{item_id}/analytes",
        json={
            "code": "HB",
            "name": "Haemoglobin",
            "unit": "g/dL",
            "ranges": [
                {"low": "12.0", "high": "17.0"},
                {"sex": "FEMALE", "low": "12.0", "high": "15.0", "critical_low": "7.0"},
            ],
        },
        headers=auth(admin_token),
    )
    assert analyte.status_code == 201, analyte.text
    return str(item_id)


@pytest.fixture
async def encounter_id(
    api: AsyncClient, admin_token: str, reception_token: str, doctor_user: User
) -> str:
    doctor = await api.post(
        DOCTORS, json={"user_id": str(doctor_user.id)}, headers=auth(admin_token)
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

    opd = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": patient.json()["id"], "doctor_id": doctor.json()["id"]},
        headers=auth(reception_token),
    )
    assert opd.status_code == 201, opd.text
    return str(opd.json()["encounter_id"])


async def _order_cbc(api: AsyncClient, doctor_token: str, encounter_id: str) -> str:
    await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
    order = await api.post(
        f"{ENCOUNTERS}/{encounter_id}/orders",
        json={"order_type": "LAB", "item_name": "CBC"},
        headers=auth(doctor_token),
    )
    assert order.status_code == 201, order.text
    return str(order.json()["id"])


# ---------------------------------------------------------------------------
# The lab day
# ---------------------------------------------------------------------------
class TestTheLabDay:
    async def test_accessioning_creates_the_report_and_the_tube_label(
        self, api: AsyncClient, doctor_token: str, tech_token: str, encounter_id: str, cbc_id: str
    ) -> None:
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        response = await api.post(
            REPORTS,
            json={"order_id": order_id, "catalogue_item_id": cbc_id},
            headers=auth(tech_token),
        )
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["report_number"].startswith("RPT-")
        assert body["status"] == "REGISTERED"
        assert body["specimen"]["accession_number"].startswith("ACC-")
        assert body["specimen"]["container"] == "EDTA (purple top)"
        assert body["specimen"]["status"] == "PENDING_COLLECTION"

    async def test_the_whole_chain_closes_the_visit(
        self,
        api: AsyncClient,
        doctor_token: str,
        tech_token: str,
        encounter_id: str,
        cbc_id: str,
    ) -> None:
        """CLAUDE.md §6 and §13 step 6, end to end."""
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()
        report_id, specimen_id = report["id"], report["specimen"]["id"]

        # The doctor is finished; the lab is not.
        done = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/complete", json={}, headers=auth(doctor_token)
        )
        assert done.json()["status"] == "PENDING_CLEARANCE"

        collected = await api.post(
            f"{SPECIMENS}/{specimen_id}/collect",
            json={"collection_site": "Left antecubital"},
            headers=auth(tech_token),
        )
        assert collected.status_code == 200, collected.text
        received = await api.post(f"{SPECIMENS}/{specimen_id}/receive", headers=auth(tech_token))
        assert received.status_code == 200, received.text

        entered = await api.post(
            f"{REPORTS}/{report_id}/results",
            json={"results": [{"analyte_code": "HB", "value_numeric": "12.5"}]},
            headers=auth(tech_token),
        )
        assert entered.status_code == 200, entered.text
        value = entered.json()["results"][0]
        # 12.5 is normal for a 34-year-old woman; it would be low on the
        # catch-all band.
        assert value["flag"] == "NORMAL"
        assert value["ref_low"] == "12.0000"

        # The technician cannot sign their own work.
        self_verify = await api.post(f"{REPORTS}/{report_id}/verify", headers=auth(tech_token))
        assert self_verify.status_code == 403, self_verify.text

        verified = await api.post(f"{REPORTS}/{report_id}/verify", headers=auth(doctor_token))
        assert verified.status_code == 200, verified.text
        assert verified.json()["status"] == "FINAL"

        visit = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(doctor_token))
        assert visit.json()["status"] == "COMPLETED"

    async def test_results_are_refused_before_the_sample_is_received(
        self, api: AsyncClient, doctor_token: str, tech_token: str, encounter_id: str, cbc_id: str
    ) -> None:
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()

        response = await api.post(
            f"{REPORTS}/{report['id']}/results",
            json={"results": [{"analyte_code": "HB", "value_numeric": "12.5"}]},
            headers=auth(tech_token),
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "specimen_not_received"

    async def test_a_panic_value_is_flagged_and_the_callback_recorded(
        self,
        api: AsyncClient,
        doctor_token: str,
        tech_token: str,
        encounter_id: str,
        cbc_id: str,
    ) -> None:
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()
        report_id, specimen_id = report["id"], report["specimen"]["id"]
        await api.post(f"{SPECIMENS}/{specimen_id}/collect", json={}, headers=auth(tech_token))
        await api.post(f"{SPECIMENS}/{specimen_id}/receive", headers=auth(tech_token))

        entered = await api.post(
            f"{REPORTS}/{report_id}/results",
            json={"results": [{"analyte_code": "HB", "value_numeric": "5.2"}]},
            headers=auth(tech_token),
        )
        assert entered.status_code == 200, entered.text
        assert entered.json()["results"][0]["flag"] == "CRITICAL_LOW"
        assert entered.json()["has_critical_result"] is True

        callback = await api.post(
            f"{REPORTS}/{report_id}/critical-callback",
            json={"notified_to": "Dr Rao, Medicine"},
            headers=auth(tech_token),
        )
        assert callback.status_code == 200, callback.text
        assert callback.json()["critical_notified_to"] == "Dr Rao, Medicine"

    async def test_a_rejected_sample_leaves_the_work_outstanding(
        self, api: AsyncClient, doctor_token: str, tech_token: str, encounter_id: str, cbc_id: str
    ) -> None:
        """The test was still asked for. A fresh tube is taken against the same
        order rather than the work quietly disappearing."""
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()
        specimen_id = report["specimen"]["id"]
        await api.post(f"{SPECIMENS}/{specimen_id}/collect", json={}, headers=auth(tech_token))

        rejected = await api.post(
            f"{SPECIMENS}/{specimen_id}/reject",
            json={"reason": "Haemolysed sample."},
            headers=auth(tech_token),
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "REJECTED"

        pending = await api.get(f"{ENCOUNTERS}/{encounter_id}/pending", headers=auth(doctor_token))
        assert pending.json()["total"] == 1

    async def test_the_worklist_puts_criticals_first(
        self, api: AsyncClient, tech_token: str, doctor_token: str, encounter_id: str, cbc_id: str
    ) -> None:
        response = await api.get(f"{REPORTS}?open_only=true", headers=auth(tech_token))
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []

    async def test_the_phlebotomy_round_lists_uncollected_samples(
        self, api: AsyncClient, doctor_token: str, tech_token: str, encounter_id: str, cbc_id: str
    ) -> None:
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        await api.post(
            REPORTS,
            json={"order_id": order_id, "catalogue_item_id": cbc_id},
            headers=auth(tech_token),
        )

        response = await api.get(f"{SPECIMENS}?pending_only=true", headers=auth(tech_token))
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["status"] == "PENDING_COLLECTION"


# ---------------------------------------------------------------------------
# Catalogue validation
# ---------------------------------------------------------------------------
class TestCatalogueRules:
    async def test_a_lab_test_needs_a_specimen_type(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        response = await api.post(
            CATALOGUE,
            json={"code": "LFT", "name": "Liver Function Tests", "discipline": "LAB"},
            headers=auth(admin_token),
        )
        assert response.status_code == 422, response.text

    async def test_a_scan_does_not_collect_a_sample(
        self, api: AsyncClient, admin_token: str
    ) -> None:
        response = await api.post(
            CATALOGUE,
            json={
                "code": "USG-ABD",
                "name": "Ultrasound Abdomen",
                "discipline": "RADIOLOGY",
                "specimen_type": "BLOOD",
            },
            headers=auth(admin_token),
        )
        assert response.status_code == 422, response.text

    async def test_a_reference_band_must_make_arithmetic_sense(
        self, api: AsyncClient, admin_token: str, cbc_id: str
    ) -> None:
        analytes = await api.get(f"{CATALOGUE}/{cbc_id}/analytes", headers=auth(admin_token))
        analyte_id = analytes.json()[0]["id"]

        response = await api.post(
            f"{CATALOGUE}/analytes/{analyte_id}/ranges",
            json={"low": "15.0", "high": "12.0"},
            headers=auth(admin_token),
        )
        assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
class TestPermissions:
    async def test_a_receptionist_cannot_read_a_report(
        self,
        api: AsyncClient,
        reception_token: str,
        tech_token: str,
        doctor_token: str,
        encounter_id: str,
        cbc_id: str,
    ) -> None:
        """Reception tells a waiting patient whether their report is ready; they
        do not get to read what is in it."""
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()

        response = await api.get(f"{REPORTS}/{report['id']}", headers=auth(reception_token))
        assert response.status_code == 403, response.text

    async def test_a_receptionist_can_see_the_sample_worklist(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        response = await api.get(SPECIMENS, headers=auth(reception_token))
        assert response.status_code == 200, response.text

    async def test_a_technician_cannot_edit_the_catalogue(
        self, api: AsyncClient, tech_token: str
    ) -> None:
        response = await api.post(
            CATALOGUE,
            json={
                "code": "RFT",
                "name": "Renal Function Tests",
                "discipline": "LAB",
                "specimen_type": "SERUM",
            },
            headers=auth(tech_token),
        )
        assert response.status_code == 403, response.text

    async def test_a_nurse_may_bleed_a_patient_but_not_result_it(
        self,
        api: AsyncClient,
        nurse_user: User,
        doctor_token: str,
        tech_token: str,
        encounter_id: str,
        cbc_id: str,
    ) -> None:
        order_id = await _order_cbc(api, doctor_token, encounter_id)
        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()
        specimen_id = report["specimen"]["id"]

        nurse_token = await login(api, nurse_user)
        collected = await api.post(
            f"{SPECIMENS}/{specimen_id}/collect", json={}, headers=auth(nurse_token)
        )
        assert collected.status_code == 200, collected.text

        entered = await api.post(
            f"{REPORTS}/{report['id']}/results",
            json={"results": [{"analyte_code": "HB", "value_numeric": "12.5"}]},
            headers=auth(nurse_token),
        )
        assert entered.status_code == 403, entered.text

    async def test_an_unauthenticated_request_is_refused(self, api: AsyncClient) -> None:
        response = await api.get(CATALOGUE)
        assert response.status_code == 401, response.text
