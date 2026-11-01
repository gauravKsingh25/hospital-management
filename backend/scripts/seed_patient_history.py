"""Give the demo tenant a few returning patients with real history.

`seed_demo.py` creates staff, prices and wards; the end-to-end tests create
patients one visit at a time. Neither leaves anyone who has *been here before*,
so the patient record — whose whole point is "how often, and what for" — opens
on a single visit and looks empty. This adds three patients whose records read
like a mid-size OPD's regulars: a diabetic with hypertension on her fifth
visit, a man with recurring gastritis, a young woman treated for anaemia.

    python scripts/seed_patient_history.py            # after seed_demo.py

Idempotent: a patient already present (same name and mobile) is skipped.

Built through the clinical service layer, so every visit went through the
state machine and has its timeline. Two deliberate departures from a live
visit, both because this is *history*:

* **The event bus is cleared first.** A past visit replayed today would
  otherwise raise consultation charges onto the cash counter and lab requests
  onto the bench — old work showing up as today's queue. So these visits carry
  no charges or invoices.
* **Timestamps are back-dated afterwards** (visit, consultation, diagnoses,
  vitals, orders, timeline), because the services stamp "now".

**Development and demo data only** — never run against real patients.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core import database as db
from app.core.database import dispose_engine, session_scope
from app.core.events import event_bus
from app.core.pagination import PageParams
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import (
    Diagnosis,
    DiagnosisCertainty,
    DiagnosisType,
    EncounterEvent,
    EncounterType,
    OrderStatus,
    OrderType,
    Vitals,
)
from app.modules.clinical.schemas import DiagnosisCreate, OrderCreate, VitalsCreate
from app.modules.identity import service as identity_service
from app.modules.patients import service as patients_service
from app.modules.patients.models import AlertSeverity, AlertType, Gender
from app.modules.patients.schemas import AlertCreate, PatientRegister
from app.modules.scheduling import service as scheduling_service
from app.modules.tenancy import service as tenancy_service

DOCTOR_EMAIL = "doctor@demo.hospital"


@dataclass
class Dx:
    description: str
    code: str | None
    final: bool = True


@dataclass
class Visit:
    days_ago: int
    complaint: str
    diagnoses: list[Dx] = field(default_factory=list)
    vitals: dict[str, int | Decimal] = field(default_factory=dict)
    medicines: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    follow_up_in_days: int | None = None
    advice: str | None = None
    emergency: bool = False
    no_show: bool = False


@dataclass
class Regular:
    name: str
    phone: str
    gender: Gender
    age: int
    city: str
    visits: list[Visit]
    alert: tuple[AlertType, AlertSeverity, str, str | None] | None = None


DIABETES = Dx("Type 2 diabetes mellitus", "E11")
HYPERTENSION = Dx("Essential hypertension", "I10")

REGULARS = [
    Regular(
        name="Kamla Devi",
        phone="9810000101",
        gender=Gender.FEMALE,
        age=58,
        city="Varanasi",
        alert=(
            AlertType.DRUG_ALLERGY,
            AlertSeverity.CRITICAL,
            "Penicillin allergy",
            "Rash and wheeze, 2019",
        ),
        visits=[
            Visit(
                days_ago=420,
                complaint="Excessive thirst and frequent urination for two months",
                diagnoses=[DIABETES],
                vitals={
                    "systolic_bp": 148,
                    "diastolic_bp": 92,
                    "pulse_bpm": 84,
                    "blood_glucose_mgdl": 238,
                    "weight_kg": Decimal("71.5"),
                },
                medicines=["Metformin 500 mg twice daily"],
                tests=["HbA1c", "Fasting lipid profile"],
                follow_up_in_days=30,
                advice="Diet chart given; walk 30 minutes daily",
            ),
            Visit(
                days_ago=330,
                complaint="Diabetes review; occasional headache",
                diagnoses=[DIABETES, Dx("Essential hypertension", "I10", final=False)],
                vitals={
                    "systolic_bp": 152,
                    "diastolic_bp": 94,
                    "pulse_bpm": 80,
                    "blood_glucose_mgdl": 176,
                },
                medicines=["Metformin 500 mg twice daily", "Amlodipine 5 mg once daily"],
                follow_up_in_days=60,
            ),
            Visit(days_ago=240, complaint="Booked review", no_show=True),
            Visit(
                days_ago=185,
                complaint="Fever and productive cough for four days",
                emergency=True,
                diagnoses=[Dx("Community-acquired pneumonia", "J18.9")],
                vitals={
                    "temperature_c": Decimal("39.1"),
                    "pulse_bpm": 108,
                    "respiratory_rate": 24,
                    "spo2_percent": 92,
                    "systolic_bp": 138,
                    "diastolic_bp": 86,
                },
                medicines=["Azithromycin 500 mg once daily (5 days)", "Paracetamol 650 mg SOS"],
                tests=["Complete blood count", "Chest X-ray PA view"],
                follow_up_in_days=7,
                advice="Penicillins avoided — documented allergy",
            ),
            Visit(
                days_ago=95,
                complaint="BP and sugar review",
                diagnoses=[DIABETES, HYPERTENSION],
                vitals={
                    "systolic_bp": 136,
                    "diastolic_bp": 86,
                    "pulse_bpm": 78,
                    "blood_glucose_mgdl": 142,
                    "weight_kg": Decimal("68.0"),
                },
                medicines=["Metformin 500 mg twice daily", "Amlodipine 5 mg once daily"],
                tests=["HbA1c"],
                follow_up_in_days=90,
            ),
            Visit(
                days_ago=12,
                complaint="Routine three-monthly review",
                diagnoses=[DIABETES, HYPERTENSION],
                vitals={
                    "systolic_bp": 132,
                    "diastolic_bp": 84,
                    "pulse_bpm": 76,
                    "blood_glucose_mgdl": 128,
                    "weight_kg": Decimal("67.2"),
                },
                medicines=["Metformin 500 mg twice daily", "Amlodipine 5 mg once daily"],
                follow_up_in_days=30,
                advice="Continue medicines; repeat HbA1c before next visit",
            ),
        ],
    ),
    Regular(
        name="Ramesh Yadav",
        phone="9810000102",
        gender=Gender.MALE,
        age=42,
        city="Mirzapur",
        visits=[
            Visit(
                days_ago=700,
                complaint="Low back pain after lifting a load",
                diagnoses=[Dx("Strain of lumbar muscles", "S39.0")],
                vitals={"systolic_bp": 128, "diastolic_bp": 82, "pulse_bpm": 76, "pain_score": 6},
                medicines=["Aceclofenac 100 mg twice daily (5 days)"],
                advice="Avoid heavy lifting for two weeks",
            ),
            Visit(
                days_ago=260,
                complaint="Burning pain in the upper abdomen after meals",
                diagnoses=[Dx("Gastritis", "K29.7", final=False)],
                vitals={"systolic_bp": 126, "diastolic_bp": 80, "pulse_bpm": 82, "pain_score": 4},
                medicines=["Pantoprazole 40 mg before breakfast (4 weeks)"],
                follow_up_in_days=28,
            ),
            Visit(
                days_ago=34,
                complaint="Upper abdominal pain has come back",
                diagnoses=[Dx("Gastritis", "K29.7")],
                vitals={"systolic_bp": 130, "diastolic_bp": 84, "pulse_bpm": 80, "pain_score": 5},
                medicines=["Pantoprazole 40 mg before breakfast (8 weeks)"],
                tests=["H. pylori stool antigen"],
                follow_up_in_days=45,
                advice="Stop smoking; avoid NSAIDs",
            ),
        ],
    ),
    Regular(
        name="Fatima Begum",
        phone="9810000103",
        gender=Gender.FEMALE,
        age=29,
        city="Varanasi",
        visits=[
            Visit(
                days_ago=150,
                complaint="Tiredness and breathlessness on climbing stairs",
                diagnoses=[Dx("Iron deficiency anaemia", "D50.9")],
                vitals={
                    "systolic_bp": 108,
                    "diastolic_bp": 68,
                    "pulse_bpm": 98,
                    "spo2_percent": 98,
                    "weight_kg": Decimal("49.0"),
                },
                medicines=["Ferrous sulphate 200 mg once daily", "Folic acid 5 mg once daily"],
                tests=["Complete blood count", "Serum ferritin"],
                follow_up_in_days=60,
            ),
            Visit(
                days_ago=80,
                complaint="Anaemia follow-up; feeling better",
                diagnoses=[Dx("Iron deficiency anaemia", "D50.9")],
                vitals={
                    "systolic_bp": 112,
                    "diastolic_bp": 72,
                    "pulse_bpm": 84,
                    "weight_kg": Decimal("50.5"),
                },
                medicines=["Ferrous sulphate 200 mg once daily"],
                tests=["Complete blood count"],
            ),
        ],
    ),
]


async def _back_date(session: AsyncSession, encounter_id: uuid.UUID, when: datetime) -> None:
    """Move everything the services stamped "now" back to the visit's day."""
    diagnoses = await session.execute(
        select(Diagnosis).where(col(Diagnosis.encounter_id) == encounter_id)
    )
    for diagnosis in diagnoses.scalars():
        diagnosis.diagnosed_at = when
        session.add(diagnosis)
    vitals = await session.execute(select(Vitals).where(col(Vitals.encounter_id) == encounter_id))
    for reading in vitals.scalars():
        reading.recorded_at = when
        session.add(reading)
    events = await session.execute(
        select(EncounterEvent).where(col(EncounterEvent.encounter_id) == encounter_id)
    )
    for event in events.scalars():
        event.occurred_at = when
        session.add(event)


async def seed(code: str) -> int:
    # History, not today's work: no charges, no bench requests. See the docstring.
    event_bus.clear()

    async with session_scope() as session:
        async with db.system_context(session):
            hospital = await tenancy_service.get_hospital_by_code(session, code)
            doctor_user = await identity_service.get_user_by_email(session, DOCTOR_EMAIL)
        if hospital is None or doctor_user is None:
            print(f"Hospital {code} or {DOCTOR_EMAIL} not found — run seed_demo.py first.")
            return 1
        hospital_id = hospital.id
        await db.set_tenant_context(session, hospital_id)
        doctor = await scheduling_service.get_doctor_by_user(
            session, doctor_user.id, hospital_id=hospital_id
        )

    for regular in REGULARS:
        async with session_scope() as session:
            await db.set_tenant_context(session, hospital_id)
            existing, _ = await patients_service.search_patients(
                session, regular.phone, PageParams(limit=20), hospital_id=hospital_id
            )
            if any(p.full_name == regular.name for p in existing):
                print(f"  {regular.name}: already present, skipped.")
                continue

            patient, _ = await patients_service.register_patient(
                session,
                PatientRegister(
                    full_name=regular.name,
                    phone=regular.phone,
                    gender=regular.gender,
                    age_years=regular.age,
                    city=regular.city,
                    confirm_not_duplicate=True,
                ),
                hospital_id=hospital_id,
            )
            first_visit = max(visit.days_ago for visit in regular.visits)
            patient.registered_at = datetime.now(UTC) - timedelta(days=first_visit, hours=1)
            session.add(patient)
            if regular.alert is not None:
                alert_type, severity, label, detail = regular.alert
                await patients_service.record_alert(
                    session,
                    patient,
                    AlertCreate(
                        alert_type=alert_type, severity=severity, label=label, detail=detail
                    ),
                    recorded_by_id=doctor_user.id,
                )

            for visit in sorted(regular.visits, key=lambda v: -v.days_ago):
                when = datetime.now(UTC).replace(
                    hour=5, minute=0, second=0, microsecond=0
                ) - timedelta(days=visit.days_ago)
                encounter = await clinical_service.open_encounter(
                    session,
                    hospital_id=hospital_id,
                    patient_id=patient.id,
                    actor=doctor_user,
                    doctor_id=doctor.id if doctor else None,
                    encounter_type=EncounterType.EMERGENCY
                    if visit.emergency
                    else EncounterType.OPD,
                    chief_complaint=visit.complaint,
                )
                if visit.no_show:
                    await clinical_service.cancel_encounter(
                        session,
                        encounter,
                        reason="Did not arrive for the booked review",
                        no_show=True,
                    )
                else:
                    await clinical_service.start_consultation(session, encounter, actor=doctor_user)
                    if visit.vitals:
                        await clinical_service.record_vitals(
                            session, encounter, VitalsCreate(**visit.vitals), actor=doctor_user
                        )
                    for index, dx in enumerate(visit.diagnoses):
                        await clinical_service.record_diagnosis(
                            session,
                            encounter,
                            DiagnosisCreate(
                                description=dx.description,
                                code=dx.code,
                                diagnosis_type=DiagnosisType.FINAL
                                if dx.final
                                else DiagnosisType.PROVISIONAL,
                                certainty=DiagnosisCertainty.CONFIRMED
                                if dx.final
                                else DiagnosisCertainty.PROBABLE,
                                is_primary=index == 0,
                            ),
                            actor=doctor_user,
                        )
                    orders = [(OrderType.PHARMACY, item) for item in visit.medicines] + [
                        (OrderType.LAB if "X-ray" not in item else OrderType.RADIOLOGY, item)
                        for item in visit.tests
                    ]
                    for order_type, item in orders:
                        order = await clinical_service.place_order(
                            session,
                            encounter,
                            OrderCreate(
                                order_type=order_type, item_name=item, blocks_closure=False
                            ),
                            actor=doctor_user,
                        )
                        order.ordered_at = when
                        order.completed_at = when + timedelta(hours=1)
                        order.status = OrderStatus.COMPLETED
                        order.completed_by_id = doctor_user.id
                        session.add(order)
                    follow_up: date | None = (
                        (when + timedelta(days=visit.follow_up_in_days)).date()
                        if visit.follow_up_in_days
                        else None
                    )
                    await clinical_service.complete_consultation(
                        session,
                        encounter,
                        actor=doctor_user,
                        follow_up_date=follow_up,
                        follow_up_instructions=visit.advice,
                    )
                    encounter.consultation_started_at = when + timedelta(minutes=20)
                    encounter.consultation_completed_at = when + timedelta(minutes=35)
                encounter.started_at = when
                encounter.closed_at = when + timedelta(minutes=40)
                session.add(encounter)
                await session.flush()
                await _back_date(session, encounter.id, when + timedelta(minutes=25))

            print(f"  {regular.name}: {len(regular.visits)} visits added ({patient.uhid}).")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--hospital-code", default="DEMO")
    args = parser.parse_args()

    async def run() -> int:
        try:
            return await seed(args.hospital_code)
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
