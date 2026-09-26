"""Request/response DTOs for `clinical`.

The transition payloads (`DeathRecord`, `ReferralRecord`, `LamaRecord`) are
typed rather than a loose `metadata` dict on the wire. The state machine still
takes a dict — that is its documented interface — but a receptionist recording a
death should be told "cause_of_death is required" by a 422 with a field name,
not by a 500 three layers down.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.clinical.models import (
    DiagnosisCertainty,
    DiagnosisType,
    EncounterStatus,
    EncounterType,
    NoteType,
    OrderPriority,
    OrderStatus,
    OrderType,
    TemplateType,
)

__all__ = [
    "AdmitRequest",
    "CancelEncounterRequest",
    "CompleteConsultationRequest",
    "DeathRecord",
    "DiagnosisCreate",
    "DiagnosisRead",
    "EncounterChart",
    "EncounterEventRead",
    "EncounterOpen",
    "EncounterRead",
    "EncounterSummary",
    "EncounterUpdate",
    "LamaRecord",
    "NoteCreate",
    "NoteRead",
    "NoteTemplateCreate",
    "NoteTemplateRead",
    "NoteTemplateUpdate",
    "OrderCancelRequest",
    "OrderCreate",
    "OrderRead",
    "PendingItems",
    "ReferralRecord",
    "SafetyBanner",
    "VitalsCreate",
    "VitalsRead",
]


# ---------------------------------------------------------------------------
# Encounters
# ---------------------------------------------------------------------------
class EncounterOpen(BaseModel):
    """Open a visit directly — casualty, or a walk-in with no appointment.

    The OPD path does not use this: check-in and quick-OPD open the encounter
    automatically, because making reception do it as a second step is exactly
    the kind of forgotten click CLAUDE.md §7b exists to remove.
    """

    patient_id: uuid.UUID
    appointment_id: uuid.UUID | None = None
    doctor_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    encounter_type: EncounterType = EncounterType.OPD
    chief_complaint: Annotated[str | None, Field(max_length=500)] = None
    triage_note: Annotated[str | None, Field(max_length=500)] = None


class EncounterUpdate(BaseModel):
    doctor_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    chief_complaint: Annotated[str | None, Field(max_length=500)] = None
    triage_note: Annotated[str | None, Field(max_length=500)] = None
    follow_up_date: date | None = None
    follow_up_instructions: Annotated[str | None, Field(max_length=500)] = None


class EncounterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_number: str
    patient_id: uuid.UUID
    appointment_id: uuid.UUID | None
    doctor_id: uuid.UUID | None
    department_id: uuid.UUID | None
    encounter_type: EncounterType
    status: EncounterStatus
    chief_complaint: str | None
    triage_note: str | None

    started_at: datetime
    consultation_started_at: datetime | None
    consultation_completed_at: datetime | None
    closed_at: datetime | None
    closed_automatically: bool

    follow_up_date: date | None
    follow_up_instructions: str | None

    deceased_at: datetime | None
    death_certified_by_id: uuid.UUID | None
    # The certifying doctor by name, attached by the router. A death record
    # whose certifier is a UUID is not a death record anybody can read — this
    # is the field a records officer, a family, and eventually a registrar of
    # deaths all need, and it is the one place in the system where "we have the
    # id, look it up" is least acceptable.
    death_certified_by_name: str | None = None
    cause_of_death: str | None
    death_place: str | None

    referred_at: datetime | None
    referred_to_facility: str | None
    referral_reason: str | None

    lama_at: datetime | None
    lama_reason: str | None
    lama_form_signed: bool

    cancellation_reason: str | None
    created_at: datetime


class EncounterSummary(BaseModel):
    """The work-list row. Deliberately light — a busy OPD list is long."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_number: str
    patient_id: uuid.UUID
    doctor_id: uuid.UUID | None
    encounter_type: EncounterType
    status: EncounterStatus
    chief_complaint: str | None
    started_at: datetime
    closed_at: datetime | None


# --- transition payloads ---------------------------------------------------
class CompleteConsultationRequest(BaseModel):
    """The doctor's "I am finished" action (CLAUDE.md §6).

    Where the visit goes next is decided by what is still open against it, not
    by the doctor picking a status — that is the whole point of the state
    machine, and it is one fewer decision on the smallest surface in the system.
    """

    follow_up_date: date | None = None
    follow_up_instructions: Annotated[str | None, Field(max_length=500)] = None
    notes: Annotated[str | None, Field(max_length=500)] = None


class AdmitRequest(BaseModel):
    reason: Annotated[str | None, Field(max_length=500)] = None
    department_id: uuid.UUID | None = None


class CancelEncounterRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=255)]
    # True records that the patient never arrived, rather than that the visit
    # was called off. Different fact, different follow-up.
    no_show: bool = False


class DeathRecord(BaseModel):
    """CLAUDE.md §6: datetime, certifying doctor, cause — all three, always."""

    deceased_at: datetime
    death_certified_by_id: uuid.UUID
    cause_of_death: Annotated[str, Field(min_length=3, max_length=500)]
    death_place: Annotated[str | None, Field(max_length=60)] = None
    reason: Annotated[str | None, Field(max_length=500)] = None

    @model_validator(mode="after")
    def _not_in_future(self) -> Self:
        from app.core.models import utc_now

        moment = self.deceased_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=utc_now().tzinfo)
        if moment > utc_now():
            raise ValueError("A time of death cannot be in the future.")
        return self


class ReferralRecord(BaseModel):
    referred_to_facility: Annotated[str, Field(min_length=2, max_length=200)]
    referral_reason: Annotated[str, Field(min_length=3, max_length=500)]
    referral_transport: Annotated[str | None, Field(max_length=60)] = None
    referred_at: datetime | None = None


class LamaRecord(BaseModel):
    """Left against medical advice, or absconded.

    `lama_form_signed` is recorded either way: an unsigned LAMA is the
    hospital's legal exposure, and pretending otherwise helps nobody.
    """

    lama_reason: Annotated[str, Field(min_length=3, max_length=500)]
    lama_form_signed: bool = False
    lama_at: datetime | None = None


class EncounterEventRead(BaseModel):
    """One row of the patient timeline (CLAUDE.md §7b)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_id: uuid.UUID
    from_status: EncounterStatus | None
    to_status: EncounterStatus
    actor_id: uuid.UUID | None
    actor_email: str | None
    reason: str | None
    event_metadata: dict[str, Any] | None
    occurred_at: datetime


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------
class VitalsCreate(BaseModel):
    """Every field optional: a nurse records what they actually measured.

    Demanding a complete set would only teach staff to type zeros, and a
    fabricated respiratory rate is worse than a missing one.
    """

    temperature_c: Annotated[Decimal | None, Field(ge=25, le=45, decimal_places=1)] = None
    pulse_bpm: Annotated[int | None, Field(ge=0, le=400)] = None
    respiratory_rate: Annotated[int | None, Field(ge=0, le=150)] = None
    systolic_bp: Annotated[int | None, Field(ge=0, le=400)] = None
    diastolic_bp: Annotated[int | None, Field(ge=0, le=300)] = None
    spo2_percent: Annotated[int | None, Field(ge=0, le=100)] = None
    height_cm: Annotated[Decimal | None, Field(gt=0, le=280)] = None
    weight_kg: Annotated[Decimal | None, Field(gt=0, le=700)] = None
    pain_score: Annotated[int | None, Field(ge=0, le=10)] = None
    blood_glucose_mgdl: Annotated[int | None, Field(ge=0, le=2000)] = None
    notes: Annotated[str | None, Field(max_length=500)] = None
    recorded_at: datetime | None = None

    @model_validator(mode="after")
    def _check_bp(self) -> Self:
        both_present = self.systolic_bp is not None and self.diastolic_bp is not None
        if both_present and self.diastolic_bp >= self.systolic_bp:  # type: ignore[operator]
            raise ValueError("Diastolic pressure must be below systolic.")
        if (self.systolic_bp is None) != (self.diastolic_bp is None):
            raise ValueError("Record both blood pressure values, or neither.")
        return self


class VitalsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    recorded_by_id: uuid.UUID | None
    recorded_at: datetime
    temperature_c: Decimal | None
    pulse_bpm: int | None
    respiratory_rate: int | None
    systolic_bp: int | None
    diastolic_bp: int | None
    spo2_percent: int | None
    height_cm: Decimal | None
    weight_kg: Decimal | None
    bmi: Decimal | None
    pain_score: int | None
    blood_glucose_mgdl: int | None
    notes: str | None
    is_abnormal: bool


# ---------------------------------------------------------------------------
# Notes, templates and diagnoses
# ---------------------------------------------------------------------------
class NoteCreate(BaseModel):
    note_type: NoteType = NoteType.PROGRESS
    content: Annotated[str, Field(min_length=1)]
    template_id: uuid.UUID | None = None
    amends_id: uuid.UUID | None = None
    # Write and sign in one call — the common case, and one click instead of two
    # on the surface CLAUDE.md §7 wants smallest.
    sign: bool = True


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_id: uuid.UUID
    note_type: NoteType
    content: str
    authored_by_id: uuid.UUID
    author_name: str
    signed_at: datetime | None
    is_signed: bool
    amends_id: uuid.UUID | None
    created_at: datetime


class NoteTemplateCreate(BaseModel):
    template_type: TemplateType = TemplateType.NOTE
    note_type: NoteType | None = None
    title: Annotated[str, Field(min_length=1, max_length=120)]
    body: Annotated[str, Field(min_length=1)]
    department_id: uuid.UUID | None = None
    # True makes it available to the whole hospital rather than just its author.
    # Requires `template:manage`; a doctor's own shortcuts do not.
    shared: bool = False


class NoteTemplateUpdate(BaseModel):
    title: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    body: Annotated[str | None, Field(min_length=1)] = None
    note_type: NoteType | None = None
    is_active: bool | None = None


class NoteTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID | None
    department_id: uuid.UUID | None
    template_type: TemplateType
    note_type: NoteType | None
    title: str
    body: str
    usage_count: int
    is_active: bool


class DiagnosisCreate(BaseModel):
    description: Annotated[str, Field(min_length=1, max_length=300)]
    # Optional on purpose: a working diagnosis is recorded at speed in the room,
    # and records staff code it afterwards. Forcing a code up front only teaches
    # doctors to pick a wrong one.
    code: Annotated[str | None, Field(max_length=16)] = None
    code_system: Annotated[str, Field(max_length=20)] = "ICD-10"
    diagnosis_type: DiagnosisType = DiagnosisType.PROVISIONAL
    certainty: DiagnosisCertainty = DiagnosisCertainty.PROBABLE
    is_primary: bool = False
    notes: Annotated[str | None, Field(max_length=500)] = None


class DiagnosisRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_id: uuid.UUID
    code: str | None
    code_system: str
    description: str
    diagnosis_type: DiagnosisType
    certainty: DiagnosisCertainty
    is_primary: bool
    diagnosed_by_id: uuid.UUID
    diagnosed_at: datetime


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
class OrderCreate(BaseModel):
    order_type: OrderType
    item_name: Annotated[str, Field(min_length=1, max_length=200)]
    item_code: Annotated[str | None, Field(max_length=32)] = None
    priority: OrderPriority = OrderPriority.ROUTINE
    instructions: Annotated[str | None, Field(max_length=500)] = None
    # "Get this done and come straight back to me" (True) versus "get this done
    # before your next visit" (False). Decides AWAITING_RESULTS against
    # PENDING_CLEARANCE, so reception stops guessing.
    review_in_visit: bool = False
    blocks_closure: bool = True


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    order_type: OrderType
    status: OrderStatus
    priority: OrderPriority
    item_code: str | None
    item_name: str
    instructions: str | None
    review_in_visit: bool
    blocks_closure: bool
    ordered_by_id: uuid.UUID
    ordered_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None


class OrderCancelRequest(BaseModel):
    reason: Annotated[str, Field(min_length=3, max_length=255)]


class PendingItems(BaseModel):
    """Why a visit will not close yet — in words reception can act on."""

    total: int
    awaiting_review: int
    by_type: dict[str, int]
    descriptions: list[str]


# ---------------------------------------------------------------------------
# Composite reads
# ---------------------------------------------------------------------------
class SafetyBanner(BaseModel):
    """The persistent patient-safety header (CLAUDE.md §7b).

    Carried on the chart response rather than fetched separately, because a
    banner that arrives one request later than the screen is a banner that gets
    missed at exactly the wrong moment.
    """

    patient_id: uuid.UUID
    uhid: str
    full_name: str
    age_years: int | None
    gender: str
    blood_group: str
    is_deceased: bool
    alerts: list[str]
    has_critical_alert: bool


class EncounterChart(BaseModel):
    """Everything the consultation screen needs, in one round trip.

    Assembled server-side deliberately: the doctor's screen opening in one
    request rather than six is most of the difference between the §7b 60-second
    target and missing it.
    """

    encounter: EncounterRead
    banner: SafetyBanner
    vitals: list[VitalsRead]
    notes: list[NoteRead]
    diagnoses: list[DiagnosisRead]
    orders: list[OrderRead]
    pending: PendingItems
