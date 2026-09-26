"""The clinical core: the Encounter and everything hung off it.

**The Encounter is the spine of the system** (CLAUDE.md §6). Every visit is one
Encounter with an explicit status, and that status is the single answer to "where
is this patient right now?" — which is what lets an OPD patient be tracked
without depending on them walking back to a reception desk.

Two rules this file exists to make structurally true:

* `Encounter.status` is **never assigned outside `state_machine.transition`**.
  Nothing here provides a setter, a default transition, or a convenience method
  that moves it. Every change writes an `EncounterEvent` alongside it, so the
  status and its history cannot disagree.
* The terminal outcomes — death, referral out, LAMA — carry their metadata as
  real columns, not as a free-text note. A death with no recorded time, no
  certifying doctor and no cause is not a record; it is a rumour.

Trade-off worth stating: the death / referral / LAMA fields live as nullable
columns on `Encounter` rather than in separate tables. They are mutually
exclusive (an encounter ends exactly once) and small, so a join for every closed
encounter would cost more than it saves. If death certificates are ever issued
from this system, the statutory MCCD Form 4 chain (immediate → antecedent →
underlying cause, plus informant details) outgrows a column and should become a
`death_records` table at that point — not before.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, DateTime, Index, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.core.models import AuditedTenantModel, TenantModel

__all__ = [
    "ClinicalNote",
    "Diagnosis",
    "DiagnosisCertainty",
    "DiagnosisType",
    "Encounter",
    "EncounterEvent",
    "EncounterSequence",
    "EncounterStatus",
    "EncounterType",
    "NoteTemplate",
    "NoteType",
    "Order",
    "OrderPriority",
    "OrderStatus",
    "OrderType",
    "TemplateType",
    "Vitals",
]


class EncounterStatus(enum.StrEnum):
    """The patient journey, exactly as CLAUDE.md §6 defines it.

    Order matters only for readability; legality lives in `state_machine.py`.
    """

    REGISTERED = "REGISTERED"
    IN_CONSULTATION = "IN_CONSULTATION"
    AWAITING_RESULTS = "AWAITING_RESULTS"
    PENDING_CLEARANCE = "PENDING_CLEARANCE"
    ADMITTED = "ADMITTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"

    # --- edge cases, first-class rather than afterthoughts (CLAUDE.md §6) ---
    REFERRED_OUT = "REFERRED_OUT"
    LAMA = "LAMA"
    DECEASED = "DECEASED"


class EncounterType(enum.StrEnum):
    OPD = "OPD"
    EMERGENCY = "EMERGENCY"
    IPD = "IPD"
    DAY_CARE = "DAY_CARE"


class NoteType(enum.StrEnum):
    """SOAP, plus the two notes staff rather than doctors write.

    Split into typed rows instead of one free-text blob so a discharge summary
    can be assembled later (`ipd`, Phase 9) without parsing prose, and so a
    doctor's template can target one section.
    """

    CHIEF_COMPLAINT = "CHIEF_COMPLAINT"
    HISTORY = "HISTORY"
    EXAMINATION = "EXAMINATION"
    ASSESSMENT = "ASSESSMENT"
    PLAN = "PLAN"
    ADVICE = "ADVICE"
    PROGRESS = "PROGRESS"
    NURSING = "NURSING"


class TemplateType(enum.StrEnum):
    """Reusable text a doctor drops into a note (CLAUDE.md §7b).

    Central to the low-doctor-effort goal: the fastest clinical note is one
    already written.
    """

    NOTE = "NOTE"
    DIAGNOSIS = "DIAGNOSIS"
    PRESCRIPTION = "PRESCRIPTION"
    ADVICE = "ADVICE"
    FOLLOW_UP = "FOLLOW_UP"
    ORDER_SET = "ORDER_SET"


class DiagnosisType(enum.StrEnum):
    PROVISIONAL = "PROVISIONAL"
    DIFFERENTIAL = "DIFFERENTIAL"
    FINAL = "FINAL"


class DiagnosisCertainty(enum.StrEnum):
    SUSPECTED = "SUSPECTED"
    PROBABLE = "PROBABLE"
    CONFIRMED = "CONFIRMED"
    RULED_OUT = "RULED_OUT"


class OrderType(enum.StrEnum):
    LAB = "LAB"
    RADIOLOGY = "RADIOLOGY"
    PHARMACY = "PHARMACY"
    PROCEDURE = "PROCEDURE"
    REFERRAL = "REFERRAL"


class OrderStatus(enum.StrEnum):
    REQUESTED = "REQUESTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class OrderPriority(enum.StrEnum):
    ROUTINE = "ROUTINE"
    URGENT = "URGENT"
    STAT = "STAT"


class Encounter(AuditedTenantModel, table=True):
    """One patient visit, end to end.

    Created at check-in and closed by a staff action — never by the patient
    happening to walk past a desk.
    """

    __tablename__ = "encounters"
    __table_args__ = (
        Index(
            "uq_encounters_number_live",
            "hospital_id",
            "encounter_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # At most one live encounter per appointment. A second check-in on the
        # same booking must reuse the chart, not open a parallel one — two open
        # encounters for one patient is how orders and charges go to the wrong
        # visit. Terminal states are excluded so a cancelled visit can be redone.
        Index(
            "uq_encounters_appointment_live",
            "appointment_id",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL AND appointment_id IS NOT NULL "
                "AND status NOT IN ('COMPLETED', 'CANCELLED', 'NO_SHOW', "
                "'REFERRED_OUT', 'LAMA', 'DECEASED')"
            ),
        ),
        # The work list: open encounters for one hospital, and one doctor's day.
        Index("ix_encounters_hospital_id_status", "hospital_id", "status"),
        Index("ix_encounters_doctor_id_status", "doctor_id", "status"),
        # The patient timeline (CLAUDE.md §7b) reads this.
        Index("ix_encounters_patient_id_started_at", "patient_id", "started_at"),
        # Backs the auto-close sweep, which scans non-terminal encounters by age.
        Index("ix_encounters_status_started_at", "status", "started_at"),
    )

    encounter_number: str = Field(
        max_length=32, index=True, description="Human-quotable reference, e.g. 'ENC-26-000123'."
    )

    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    # Nullable: an emergency walk-in is an encounter before anyone books
    # anything, and a direct admission may never have an OPD appointment.
    appointment_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="appointments.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    doctor_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="doctors.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )

    encounter_type: EncounterType = Field(default=EncounterType.OPD, index=True)

    # NEVER assigned outside `state_machine.transition`. See the module docstring.
    status: EncounterStatus = Field(default=EncounterStatus.REGISTERED, index=True)

    # In the patient's own words, captured by reception before the doctor sees
    # them — one of the few things that saves the doctor typing.
    chief_complaint: str | None = Field(default=None, max_length=500)
    triage_note: str | None = Field(default=None, max_length=500)

    started_at: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        description="When the patient was registered for this visit.",
    )
    consultation_started_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # Set by the doctor's "complete visit" action. Distinct from `closed_at`:
    # the doctor is finished long before the pharmacy and the cash counter are.
    consultation_completed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    closed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        description="When the encounter reached a terminal status.",
    )
    closed_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    # True when the auto-close sweep closed it rather than a person. Reporting
    # must be able to tell "the clinic closed this visit" from "nobody did, and
    # a background job tidied up" — the second is a process problem to fix.
    closed_automatically: bool = Field(default=False)

    follow_up_date: date | None = Field(default=None, sa_type=Date, index=True)
    follow_up_instructions: str | None = Field(default=None, max_length=500)

    # --- DECEASED (CLAUDE.md §6: datetime, certifying doctor, cause) --------
    deceased_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    death_certified_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )
    cause_of_death: str | None = Field(default=None, max_length=500)
    # Where death occurred: OPD, ward, casualty, brought-in-dead. Statutory on
    # the death register and genuinely different clinically.
    death_place: str | None = Field(default=None, max_length=60)

    # --- REFERRED_OUT -------------------------------------------------------
    referred_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    referred_to_facility: str | None = Field(default=None, max_length=200)
    referral_reason: str | None = Field(default=None, max_length=500)
    # Whether an ambulance carried them. Matters for the bill and for the
    # hospital's own transfer audit.
    referral_transport: str | None = Field(default=None, max_length=60)

    # --- LAMA / absconded ---------------------------------------------------
    lama_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    lama_recorded_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )
    lama_reason: str | None = Field(default=None, max_length=500)
    # A signed LAMA form is the hospital's legal protection. Recording that it
    # was refused is as important as recording that it was signed.
    lama_form_signed: bool = Field(default=False)

    # --- CANCELLED / NO_SHOW ------------------------------------------------
    cancellation_reason: str | None = Field(default=None, max_length=255)

    @property
    def is_closed(self) -> bool:
        """Terminal by data, not by a second flag that can drift from `status`."""
        return self.closed_at is not None


class EncounterEvent(AuditedTenantModel, table=True):
    """One row per state change — the audit trail and the patient timeline.

    Written by `state_machine.transition` and by nothing else. This is both the
    NABH/DPDP audit requirement (CLAUDE.md §8) and the feed the §7b patient
    timeline renders, which is why it is a real table rather than lines in the
    generic audit log: the timeline is a clinical read, not a compliance one,
    and it must survive the audit log being archived.
    """

    __tablename__ = "encounter_events"
    __table_args__ = (
        # The timeline read: one encounter, in order.
        Index("ix_encounter_events_encounter_id_occurred_at", "encounter_id", "occurred_at"),
    )

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    # Null `from_status` marks the opening event.
    from_status: EncounterStatus | None = Field(default=None, index=True)
    to_status: EncounterStatus = Field(index=True)

    # Null actor means the auto-close worker. Deliberately nullable rather than
    # pointing at a fake "system" user: inventing a user account that nobody can
    # log into makes the audit log lie about who acts in this hospital.
    actor_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    actor_email: str | None = Field(
        default=None, max_length=255, description="Snapshotted so the trail survives a rename."
    )

    reason: str | None = Field(default=None, max_length=500)
    # Structured detail for the transition — the death metadata, the referral
    # destination, the order that cleared. JSONB so a new transition needs no
    # migration.
    event_metadata: dict[str, Any] | None = Field(default=None, sa_type=JSONB)

    occurred_at: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )


class Vitals(AuditedTenantModel, table=True):
    """A set of observations, normally entered by a nurse (CLAUDE.md §7).

    One row per reading rather than columns on the encounter: vitals are taken
    repeatedly, and a trend is the clinically useful thing.
    """

    __tablename__ = "vitals"
    __table_args__ = (Index("ix_vitals_encounter_id_recorded_at", "encounter_id", "recorded_at"),)

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    recorded_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    recorded_at: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # Every measurement is optional: a nurse who has a blood pressure but no
    # thermometer to hand must be able to record what they actually have.
    # `Numeric` rather than float — a temperature of 98.60000000000001 on a
    # clinical record is not acceptable, and floats do that.
    temperature_c: Decimal | None = Field(default=None, sa_type=Numeric(4, 1))  # type: ignore[call-overload]
    pulse_bpm: int | None = Field(default=None, ge=0, le=400)
    respiratory_rate: int | None = Field(default=None, ge=0, le=150)
    systolic_bp: int | None = Field(default=None, ge=0, le=400)
    diastolic_bp: int | None = Field(default=None, ge=0, le=300)
    spo2_percent: int | None = Field(default=None, ge=0, le=100)
    height_cm: Decimal | None = Field(default=None, sa_type=Numeric(5, 1))  # type: ignore[call-overload]
    weight_kg: Decimal | None = Field(default=None, sa_type=Numeric(5, 2))  # type: ignore[call-overload]
    # Stored, not computed on read: the weight and height it was derived from
    # can be corrected later, and a BMI that silently changes under a clinician
    # who already acted on it is worse than one that is visibly stale.
    bmi: Decimal | None = Field(default=None, sa_type=Numeric(4, 1))  # type: ignore[call-overload]
    # 0-10 numeric rating scale — the standard NABH pain assessment.
    pain_score: int | None = Field(default=None, ge=0, le=10)
    blood_glucose_mgdl: int | None = Field(default=None, ge=0, le=2000)

    notes: str | None = Field(default=None, max_length=500)

    # Set by the service when a reading falls outside safe adult ranges, so the
    # doctor's screen can surface it without re-deriving the thresholds.
    is_abnormal: bool = Field(default=False, index=True)


class ClinicalNote(AuditedTenantModel, table=True):
    """A section of the clinical record, authored and e-signed by a clinician.

    Signing is a one-way door: a signed note is amended by adding another note,
    never by editing the original. That is what makes the record defensible.
    """

    __tablename__ = "clinical_notes"
    __table_args__ = (
        Index("ix_clinical_notes_encounter_id_note_type", "encounter_id", "note_type"),
    )

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    note_type: NoteType = Field(default=NoteType.PROGRESS, index=True)
    content: str = Field(min_length=1)

    authored_by_id: uuid.UUID = Field(foreign_key="users.id", ondelete="RESTRICT", index=True)
    author_name: str = Field(
        max_length=200, description="Snapshotted: the record must read correctly in ten years."
    )

    # e-signature (CLAUDE.md §7: the doctor signs, and that is the act).
    signed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    is_signed: bool = Field(default=False, index=True)

    # Which template it came from, if any. Feeds "which templates actually get
    # used", which is how the template library stays short enough to be useful.
    template_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="note_templates.id",
        ondelete="SET NULL",
        nullable=True,
    )
    # Set when this note amends an earlier signed one.
    amends_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="clinical_notes.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )


class NoteTemplate(AuditedTenantModel, table=True):
    """Reusable clinical text (CLAUDE.md §7b — doctor templates).

    `owner_id` NULL means the template is hospital-wide; set, it is that
    clinician's own. Both matter: a hospital standardises its discharge advice,
    and every consultant still has three phrases they type forty times a day.
    """

    __tablename__ = "note_templates"
    __table_args__ = (
        Index("ix_note_templates_hospital_id_template_type", "hospital_id", "template_type"),
        Index("ix_note_templates_owner_id_is_active", "owner_id", "is_active"),
    )

    owner_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="CASCADE", nullable=True
    )
    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="SET NULL",
        nullable=True,
    )

    template_type: TemplateType = Field(default=TemplateType.NOTE, index=True)
    note_type: NoteType | None = Field(
        default=None, description="Which note section this fills, when it fills one."
    )

    title: str = Field(max_length=120, index=True)
    body: str = Field(min_length=1)

    # Sorts the picker by what the clinician actually reaches for.
    usage_count: int = Field(default=0)
    is_active: bool = Field(default=True, index=True)


class Diagnosis(AuditedTenantModel, table=True):
    """A coded diagnosis on an encounter.

    `code` is nullable because a working diagnosis is recorded in the room, at
    speed, and an ICD-10 code is often assigned afterwards by records staff.
    Forcing a code up front would only teach doctors to pick a wrong one.
    """

    __tablename__ = "diagnoses"
    __table_args__ = (
        Index("ix_diagnoses_encounter_id_is_primary", "encounter_id", "is_primary"),
        # Morbidity reporting and the future ABDM/FHIR condition resource.
        Index("ix_diagnoses_hospital_id_code", "hospital_id", "code"),
    )

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    code: str | None = Field(default=None, max_length=16, index=True)
    code_system: str = Field(default="ICD-10", max_length=20)
    description: str = Field(max_length=300)

    diagnosis_type: DiagnosisType = Field(default=DiagnosisType.PROVISIONAL, index=True)
    certainty: DiagnosisCertainty = Field(default=DiagnosisCertainty.PROBABLE)
    # Exactly one primary per encounter is enforced in the service rather than
    # by a constraint: the correction path ("no, THAT one is primary") has to be
    # a single call, not a delete-then-insert the user can leave half done.
    is_primary: bool = Field(default=False, index=True)

    diagnosed_by_id: uuid.UUID = Field(foreign_key="users.id", ondelete="RESTRICT", index=True)
    diagnosed_at: datetime = Field(
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    notes: str | None = Field(default=None, max_length=500)


class Order(AuditedTenantModel, table=True):
    """A doctor's order — and the ledger that decides when a visit can close.

    This is the pending-item mechanism behind CLAUDE.md §6: an encounter reaches
    `COMPLETED` only when nothing is still open against it. `diagnostics`
    (Phase 6) and `billing` (Phase 7) own *fulfilment* — the sample, the result,
    the charge — and report back through the event bus. What lives here is the
    clinical act of ordering and the open/closed fact, because that is what the
    state machine has to reason about.

    `review_in_visit` is the distinction that decides `AWAITING_RESULTS` from
    `PENDING_CLEARANCE`, and it is a real clinical one: "get this done and come
    straight back to me" is a different instruction from "get this done before
    your next visit". One checkbox for the doctor, and reception stops guessing.
    """

    __tablename__ = "orders"
    __table_args__ = (
        # The closure check runs on every order change and every sweep pass.
        Index("ix_orders_encounter_id_status", "encounter_id", "status"),
        # The lab's and pharmacy's work lists.
        Index("ix_orders_hospital_id_order_type_status", "hospital_id", "order_type", "status"),
    )

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    order_type: OrderType = Field(index=True)
    status: OrderStatus = Field(default=OrderStatus.REQUESTED, index=True)
    priority: OrderPriority = Field(default=OrderPriority.ROUTINE, index=True)

    # Free-text item plus an optional catalogue code. The catalogue arrives with
    # `diagnostics` and `billing`; until then a doctor can still order "CBC" and
    # the lab still knows what to do.
    item_code: str | None = Field(default=None, max_length=32, index=True)
    item_name: str = Field(max_length=200)
    instructions: str | None = Field(default=None, max_length=500)

    # Whether the doctor will see the patient again in THIS visit once it is
    # back. Drives AWAITING_RESULTS vs PENDING_CLEARANCE.
    review_in_visit: bool = Field(default=False, index=True)
    # False for an order that must not hold the visit open (a follow-up test to
    # be done next month). Deliberately explicit so "why won't this close?" has
    # a data answer.
    blocks_closure: bool = Field(default=True, index=True)

    ordered_by_id: uuid.UUID = Field(foreign_key="users.id", ondelete="RESTRICT", index=True)
    ordered_at: datetime = Field(
        nullable=False,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    started_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    completed_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    cancelled_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    cancellation_reason: str | None = Field(default=None, max_length=255)

    # Filled in by `diagnostics` when it takes ownership of fulfilment, so the
    # link exists without `clinical` knowing that module's schema.
    fulfilment_ref: str | None = Field(
        default=None, max_length=64, description="Owning module's reference, once it has one."
    )


class EncounterSequence(TenantModel, table=True):
    """Per-hospital, per-year encounter number counter.

    Same shape as `UhidSequence` and `AppointmentSequence`, and for the same
    reason: a Postgres SEQUENCE is global to the database and cannot restart per
    tenant per year, and these numbers get read down a phone line.
    """

    __tablename__ = "encounter_sequences"
    __table_args__ = (
        Index("uq_encounter_sequences_hospital_year", "hospital_id", "year", unique=True),
    )

    year: int = Field(index=True)
    last_value: int = Field(default=0)
