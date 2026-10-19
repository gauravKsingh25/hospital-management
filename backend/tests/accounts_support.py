"""Shared set-up for the accountant tests: a priced hospital and an admitted stay.

Built through the services, the way the counter and the admission desk build
it, so the stay carries the real automatic charges — the OPD consultation fee
from the visit it was admitted from, and whatever the ward accrues.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.billing import service as billing_service
from app.modules.billing.models import ChargeCategory, PayerType
from app.modules.billing.schemas import RateCardCreate, ServiceItemCreate, ServicePriceUpsert
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter
from app.modules.identity.models import User
from app.modules.ipd import service as ipd_service
from app.modules.ipd.models import Admission, BedClass
from app.modules.ipd.schemas import AdmitFromRequest, BedCreate, WardCreate
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister

D = Decimal


async def price_list(session: AsyncSession, hospital_id: uuid.UUID) -> None:
    """A cash card: an exempt consultation, a taxable dressing, a room-day."""
    card = await billing_service.create_rate_card(
        session,
        RateCardCreate(
            code="CASH", name="Cash counter", payer_type=PayerType.CASH, is_default=True
        ),
        hospital_id=hospital_id,
    )
    for code, name, category, price, exempt, rate in (
        ("OPD-CONSULT", "OPD consultation", ChargeCategory.CONSULTATION, "500.00", True, "0"),
        ("DRESSING", "Wound dressing", ChargeCategory.PHARMACY, "240.00", False, "12.00"),
        ("ROOM-GEN", "General ward bed-day", ChargeCategory.ROOM, "2000.00", True, "0"),
    ):
        item = await billing_service.create_service_item(
            session,
            ServiceItemCreate(
                code=code, name=name, category=category, is_gst_exempt=exempt, gst_rate=D(rate)
            ),
            hospital_id=hospital_id,
        )
        await billing_service.set_price(
            session, item, ServicePriceUpsert(rate_card_id=card.id, price=D(price))
        )
    await session.commit()


async def register(
    session: AsyncSession,
    hospital_id: uuid.UUID,
    name: str = "Ramesh Yadav",
    phone: str = "9812345670",
) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(full_name=name, phone=phone, gender=Gender.MALE, age_years=58),
        hospital_id=hospital_id,
    )
    await session.commit()
    return record


async def open_visit(
    session: AsyncSession, hospital_id: uuid.UUID, patient: Patient, actor: User
) -> Encounter:
    """An OPD visit reception has marked seen: the ₹500 fee is pending on it."""
    encounter = await clinical_service.open_encounter(
        session, hospital_id=hospital_id, patient_id=patient.id, actor=actor
    )
    await clinical_service.record_seen_by_staff(session, encounter, actor=actor)
    await session.commit()
    return encounter


@dataclass
class Stay:
    patient: Patient
    encounter: Encounter
    admission: Admission


async def admit(
    session: AsyncSession, hospital_id: uuid.UUID, encounter: Encounter, actor: User
) -> Admission:
    """Send the visit to the admission desk and admit it into a fresh bed."""
    suffix = uuid.uuid4().hex[:4].upper()
    ward = await ipd_service.create_ward(
        session,
        WardCreate(code=f"GW{suffix}", name="General Ward", bed_class=BedClass.GENERAL),
        hospital_id=hospital_id,
    )
    bed = await ipd_service.create_bed(
        session,
        BedCreate(ward_id=ward.id, code=f"GW{suffix}-01", tariff_item_code="ROOM-GEN"),
        hospital_id=hospital_id,
    )
    request = await ipd_service.request_admission(
        session, hospital_id=hospital_id, encounter_id=encounter.id
    )
    admission = await ipd_service.admit_from_request(
        session,
        request,
        AdmitFromRequest(bed_id=bed.id, provisional_diagnosis="Unstable angina"),
        actor=actor,
    )
    await session.commit()
    await db.set_tenant_context(session, hospital_id)
    return admission


async def admitted_stay(
    session: AsyncSession, hospital_id: uuid.UUID, actor: User, *, name: str = "Ramesh Yadav"
) -> Stay:
    """A patient admitted straight from a still-open OPD visit."""
    patient = await register(session, hospital_id, name=name)
    encounter = await open_visit(session, hospital_id, patient, actor)
    admission = await admit(session, hospital_id, encounter, actor)
    encounter = await clinical_service.get_encounter(
        session, admission.encounter_id, hospital_id=hospital_id
    )
    return Stay(patient=patient, encounter=encounter, admission=admission)
