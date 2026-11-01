"""What the admission screen needs, in the one admission response.

The stay screen shows the safety banner, the attending doctor by name and
where the discharge summary stands, without a second request — a ward screen
that fills in the allergy a second later is a screen where it gets missed.
Its "running bill" is the stay's own: what was charged from the admission on,
not the OPD consultation before it, which is its own bill.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.billing import service as billing_service
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.ipd.schemas import AdmissionUpdate
from app.modules.scheduling import service as scheduling_service
from app.modules.scheduling.schemas import DoctorCreate
from app.modules.tenancy.models import Hospital
from tests import accounts_support as support
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

ADMISSIONS = "/api/v1/ipd/admissions"


@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    await support.price_list(session, hospital.id)
    return hospital


@pytest.fixture
async def admin(session: AsyncSession, tenant: Hospital) -> User:
    user = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


async def test_the_stays_cost_starts_at_the_admission(
    session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    """The ₹500 consultation before the admission is its own OPD bill."""
    stay = await support.admitted_stay(session, tenant.id, admin)

    breakdown = await billing_service.cost_breakdown(
        session, stay.encounter.id, hospital_id=tenant.id
    )

    assert [line.description for line in breakdown.lines] == []
    assert breakdown.total == support.D("0.00")


async def test_the_admission_carries_banner_doctor_and_summary_state(
    api: AsyncClient, session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    stay = await support.admitted_stay(session, tenant.id, admin)
    doctor_user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    doctor = await scheduling_service.create_doctor(
        session,
        DoctorCreate(user_id=doctor_user.id),
        hospital_id=tenant.id,
        display_name="Dr. Meera Rao",
    )
    await ipd_service.update_admission(
        session, stay.admission, AdmissionUpdate(attending_doctor_id=doctor.id)
    )
    await session.commit()
    token = await login(api, admin)
    await api.post(
        f"/api/v1/patients/{stay.patient.id}/alerts",
        json={"alert_type": "DRUG_ALLERGY", "label": "Penicillin", "severity": "CRITICAL"},
        headers=auth(token),
    )

    response = await api.get(f"{ADMISSIONS}/{stay.admission.id}", headers=auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["safety_banner"]["has_critical_alert"] is True
    assert any("Penicillin" in alert for alert in body["safety_banner"]["alerts"])
    assert body["discharge_summary_status"] is None
    assert body["attending_doctor_name"] == "Dr. Meera Rao"


async def test_a_discharged_stay_still_names_its_last_bed(
    api: AsyncClient, session: AsyncSession, tenant: Hospital, admin: User
) -> None:
    """The printed summary says where the patient was after the bed is freed."""
    stay = await support.admitted_stay(session, tenant.id, admin)
    await session.commit()
    token = await login(api, admin)

    discharged = await api.post(
        f"{ADMISSIONS}/{stay.admission.id}/discharge",
        json={"discharge_type": "RECOVERED"},
        headers=auth(token),
    )

    assert discharged.status_code == 200, discharged.text
    body = (await api.get(f"{ADMISSIONS}/{stay.admission.id}", headers=auth(token))).json()
    assert body["bed"] is None
    assert body["last_ward_bed"].startswith("General Ward, GW")
