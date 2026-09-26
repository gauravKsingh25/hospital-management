"""Wards and beds, the admission, the medication chart, and the discharge summary.

CLAUDE.md §13 step 9: *"admission, bed/ward management, nursing notes, MAR,
rounds, discharge summary auto-compile."* Two of those six are deliberately
**not** tables here, and the reason is the module boundary rather than laziness:

* **Nursing notes** are `clinical.ClinicalNote` with `NoteType.NURSING`, which
  already exists. A parallel `ipd_nursing_notes` table would mean a patient's
  record lived in two places and a discharge summary had to read both. The note
  belongs to the encounter, and an admission *is* an encounter.
* **Rounds** are `ClinicalNote` with `NoteType.PROGRESS`. A ward round produces a
  progress note; what `ipd` adds is the *worklist* — which admitted patients this
  consultant has not written one for today — and a worklist is a query, not a
  table.

What is genuinely new is everything below: physical capacity, who is in which
bed and when, what drugs are due and whether they were actually given, and the
document that has to exist before a patient can leave.

---------------------------------------------------------------------------
The two decisions worth arguing about
---------------------------------------------------------------------------

**Bed occupancy is a separate table from the bed.** `beds.status` says whether a
bed can be filled right now; `bed_assignments` says who was in it, from when to
when. Denormalising the second into the first would make "which bed was this
patient in on day three" unanswerable, and that question is asked in earnest
twice: when infection control traces a contact, and when a patient disputes a
room charge.

**The medication chart materialises doses in advance.** A `MedicationOrder` of
"1g TDS" generates `MedicationAdministration` rows at the due times, *before*
anyone gives anything. This is the whole point of a MAR: the clinically
significant event is a dose that was **not** given, and a table that only records
administrations cannot represent one. A missed 6am antibiotic has to be visible
at 8am without anybody remembering to look for an absence.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Column, Date, DateTime, Index, text
from sqlmodel import Field

from app.core.models import AuditedTenantModel, TenantModel

__all__ = [
    "Admission",
    "AdmissionRequest",
    "AdmissionRequestStatus",
    "AdmissionSequence",
    "AdmissionStatus",
    "Bed",
    "BedAssignment",
    "BedClass",
    "BedStatus",
    "DischargeSummary",
    "DischargeType",
    "DoseStatus",
    "DrugSchedule",
    "MedicationAdministration",
    "MedicationOrder",
    "MedicationRoute",
    "SummaryStatus",
    "Ward",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class BedClass(enum.StrEnum):
    """What the patient is paying for, and what the ward can offer.

    Drives the room tariff, so it is an enum rather than free text: a rate card
    keyed on a string somebody typed is a rate card with three spellings of
    "Semi Private" in it.
    """

    GENERAL = "GENERAL"
    SEMI_PRIVATE = "SEMI_PRIVATE"
    PRIVATE = "PRIVATE"
    DELUXE = "DELUXE"
    ICU = "ICU"
    HDU = "HDU"
    NICU = "NICU"
    EMERGENCY = "EMERGENCY"
    DAY_CARE = "DAY_CARE"


class BedStatus(enum.StrEnum):
    """The bed lifecycle CLAUDE.md §7b asks for, so availability is never wrong.

    `CLEANING` is the rung people leave out, and leaving it out is what makes a
    bed board lie: the moment a patient is discharged the bed looks free, so
    admissions sends someone to it, and they arrive to find it unmade. Modelling
    housekeeping as a state the bed passes through means "available" means
    available.
    """

    AVAILABLE = "AVAILABLE"
    OCCUPIED = "OCCUPIED"
    # Discharged, not yet turned around.
    CLEANING = "CLEANING"
    # Held for a named patient — a theatre case coming back, a planned
    # admission. Distinct from OCCUPIED because nobody is in it yet.
    RESERVED = "RESERVED"
    # Broken, or closed for infection control.
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


class AdmissionStatus(enum.StrEnum):
    ADMITTED = "ADMITTED"
    # The doctor has written the discharge; the patient is settling the bill and
    # waiting for medicines. The bed is still theirs, which is why this is not
    # simply "discharged" — a bed board that frees the bed at the doctor's
    # signature is a bed board that double-books it.
    DISCHARGE_INITIATED = "DISCHARGE_INITIATED"
    DISCHARGED = "DISCHARGED"
    CANCELLED = "CANCELLED"


class AdmissionRequestStatus(enum.StrEnum):
    """Where a patient sent to the admission desk has got to.

    Two ways out of PENDING and no way back: an admission made in error is
    `Admission.cancel`, not a reopened request, and a patient turned away and
    later sent again is a second request with its own author and time.
    """

    PENDING = "PENDING"
    ADMITTED = "ADMITTED"
    CANCELLED = "CANCELLED"


class DischargeType(enum.StrEnum):
    """How the stay ended. Every one of these is a different form.

    The edge cases are first-class here for the same reason CLAUDE.md §6 makes
    them first-class on the Encounter: they are not rare, and treating them as
    variations of "discharged" loses the reason.
    """

    RECOVERED = "RECOVERED"
    # Discharged to another facility, with a reason and a destination.
    REFERRED = "REFERRED"
    # Left against medical advice / absconded.
    LAMA = "LAMA"
    DECEASED = "DECEASED"
    # Sent home to return — chemotherapy cycles, dialysis.
    TRANSFERRED_OUT = "TRANSFERRED_OUT"


class MedicationRoute(enum.StrEnum):
    ORAL = "ORAL"
    IV = "IV"
    IM = "IM"
    SC = "SC"
    TOPICAL = "TOPICAL"
    INHALED = "INHALED"
    RECTAL = "RECTAL"
    OPHTHALMIC = "OPHTHALMIC"
    NASOGASTRIC = "NASOGASTRIC"
    OTHER = "OTHER"


class DrugSchedule(enum.StrEnum):
    """Drugs and Cosmetics Rules schedules (CLAUDE.md §9).

    H and H1 need a prescription on file; X needs one retained for two years and
    a stricter dispensing record. Carried on the order so the pharmacy module can
    validate hard rather than by convention.
    """

    NONE = "NONE"
    H = "H"
    H1 = "H1"
    X = "X"
    NARCOTIC = "NARCOTIC"


class DoseStatus(enum.StrEnum):
    """One slot on the medication chart.

    `DUE` is the state a materialised dose starts in. Everything else is a nurse
    saying what happened, and three of them are ways of saying "not given" that
    a hospital must be able to tell apart: the patient refused, the ward ran out,
    or a doctor held it.
    """

    DUE = "DUE"
    GIVEN = "GIVEN"
    # Nobody recorded anything and the window has passed. Set by the sweep, not
    # by a person — which is exactly what makes it worth auditing.
    MISSED = "MISSED"
    REFUSED = "REFUSED"
    # Deliberately withheld: nil by mouth, pre-operative, a clinical decision.
    HELD = "HELD"
    # The order was stopped or the patient left before this slot came round.
    CANCELLED = "CANCELLED"


class SummaryStatus(enum.StrEnum):
    # Assembled by the system from what is already recorded. Not yet a document.
    DRAFT = "DRAFT"
    # Reviewed and signed by a doctor. Frozen from here.
    FINAL = "FINAL"
    AMENDED = "AMENDED"


# ---------------------------------------------------------------------------
# Physical capacity
# ---------------------------------------------------------------------------
class Ward(AuditedTenantModel, table=True):
    """A named nursing unit: "Male Medical", "ICU-1", "Maternity"."""

    __tablename__ = "wards"
    __table_args__ = (
        Index(
            "uq_wards_code_live",
            "hospital_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    code: str = Field(max_length=16, index=True, description="Short code staff say out loud.")
    name: str = Field(max_length=120, index=True)
    floor: str | None = Field(default=None, max_length=20)
    bed_class: BedClass = Field(
        default=BedClass.GENERAL,
        index=True,
        description="The ward's default; a bed may differ.",
    )

    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )

    # NULL means mixed. Enforced on admission rather than merely displayed:
    # putting a man in a female ward is a complaint, sometimes a serious one.
    gender_policy: str | None = Field(
        default=None, max_length=10, description="MALE, FEMALE, or NULL for mixed."
    )
    phone_extension: str | None = Field(default=None, max_length=16)
    is_active: bool = Field(default=True, index=True)


class Bed(AuditedTenantModel, table=True):
    """One bed. The unit a patient actually occupies and is billed for."""

    __tablename__ = "beds"
    __table_args__ = (
        Index(
            "uq_beds_code_live",
            "hospital_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The bed board: "what is free in this ward, right now".
        Index("ix_beds_ward_id_status", "ward_id", "status"),
        Index("ix_beds_hospital_id_bed_class_status", "hospital_id", "bed_class", "status"),
    )

    ward_id: uuid.UUID = Field(foreign_key="wards.id", ondelete="RESTRICT", index=True)

    code: str = Field(max_length=16, index=True, description="e.g. 'ICU-04'. Printed on the board.")
    label: str | None = Field(default=None, max_length=60, description="e.g. 'Window side'.")
    bed_class: BedClass = Field(default=BedClass.GENERAL, index=True)

    # Never assigned directly — every change goes through `transitions.py`.
    status: BedStatus = Field(default=BedStatus.AVAILABLE, index=True)

    # Set while status is RESERVED, so a held bed says who it is being held for
    # rather than merely being unavailable for no visible reason.
    reserved_for_patient_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="patients.id",
        ondelete="SET NULL",
        nullable=True,
    )
    out_of_service_reason: str | None = Field(default=None, max_length=255)

    # Stamped on release. Housekeeping's worklist is ordered by it, so the bed
    # empty longest is cleaned first.
    released_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # Maps the bed onto a priced service (`billing.service_items.code`), so the
    # room tariff is a rate-card lookup like everything else rather than a number
    # living in two places.
    tariff_item_code: str | None = Field(default=None, max_length=32, index=True)

    is_active: bool = Field(default=True, index=True)

    @property
    def is_occupiable(self) -> bool:
        return self.is_active and self.status in (BedStatus.AVAILABLE, BedStatus.RESERVED)


# ---------------------------------------------------------------------------
# The stay
# ---------------------------------------------------------------------------
class Admission(AuditedTenantModel, table=True):
    """An inpatient stay, hanging off the existing `clinical.Encounter`.

    There is deliberately no second patient-journey record: the Encounter is the
    spine (CLAUDE.md §6) and an admission is what happens to it between
    `ADMITTED` and `COMPLETED`. That is why `encounter_id` is unique among live
    rows — a visit cannot be admitted twice, and a bug that tried would otherwise
    produce two bills for one stay.
    """

    __tablename__ = "admissions"
    __table_args__ = (
        Index(
            "uq_admissions_number_live",
            "hospital_id",
            "admission_number",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_admissions_encounter_live",
            "encounter_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status <> 'CANCELLED'"),
        ),
        # The ward census, and the bed-day accrual sweep.
        Index("ix_admissions_hospital_id_status", "hospital_id", "status"),
        Index("ix_admissions_patient_id_admitted_at", "patient_id", "admitted_at"),
    )

    admission_number: str = Field(max_length=32, index=True, description="e.g. 'IPD-2627-000001'.")

    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    # The consultant carrying the patient. `clinical.Encounter.doctor_id` is who
    # saw them in OPD, which is often somebody else.
    attending_doctor_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="doctors.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    department_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="departments.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )

    status: AdmissionStatus = Field(default=AdmissionStatus.ADMITTED, index=True)

    admitted_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        index=True,
    )
    # What the patient was admitted *for*, in the admitting doctor's words. The
    # coded diagnosis lives in `clinical.Diagnosis` and often arrives later.
    provisional_diagnosis: str | None = Field(default=None, max_length=500)
    admission_notes: str | None = Field(default=None, max_length=2000)
    expected_stay_days: int | None = Field(default=None, ge=0)

    # Who is staying with the patient. In an Indian hospital this is who is
    # actually spoken to, and who signs consent when the patient cannot.
    attendant_name: str | None = Field(default=None, max_length=200)
    attendant_phone: str | None = Field(default=None, max_length=15)
    attendant_relation: str | None = Field(default=None, max_length=50)

    # --- discharge --------------------------------------------------------
    discharge_initiated_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    discharged_at: datetime | None = Field(
        default=None,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    discharge_type: DischargeType | None = Field(default=None, index=True)
    discharged_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    cancellation_reason: str | None = Field(default=None, max_length=255)

    # --- billing ----------------------------------------------------------
    # How many bed-days have been charged so far. Kept on the admission rather
    # than counted from charges each time because the accrual sweep runs per
    # tenant across every open stay, and a per-row COUNT over `charges` on a
    # 300-bed hospital every night is a query nobody needs.
    bed_days_charged: int = Field(default=0)

    @property
    def is_open(self) -> bool:
        return self.status in (AdmissionStatus.ADMITTED, AdmissionStatus.DISCHARGE_INITIATED)


class AdmissionRequest(AuditedTenantModel, table=True):
    """A patient sent from the OPD to the admission desk.

    The hand-off between two counters: reception knows the doctor has advised
    admission; the admission desk does the bed, the paperwork and the deposit.
    Without a record of the hand-off the patient is carried across the building
    by word of mouth, and the desk learns about them when they arrive.

    Deliberately *not* an `Admission` in a pending state. An admission owns a
    bed, a number and a bill; a request owns none of those, and a census that
    had to filter out "admissions that are not admissions yet" would be wrong
    the first time somebody forgot the filter.

    `encounter_id` is the visit the patient was seen on. If that visit has
    closed by the time the desk admits them, the admission opens a new IPD
    visit and this row still points at the one that sent them.
    """

    __tablename__ = "admission_requests"
    __table_args__ = (
        # One open request per patient. Two counters sending the same person
        # would give the desk two rows for one bed.
        Index(
            "uq_admission_requests_patient_pending",
            "hospital_id",
            "patient_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status = 'PENDING'"),
        ),
        # The desk's list: pending, oldest first.
        Index(
            "ix_admission_requests_hospital_id_status_requested_at",
            "hospital_id",
            "status",
            "requested_at",
        ),
    )

    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)
    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    # Who saw them in OPD — the desk's usual default for the attending doctor.
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
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
    # Snapshotted so the desk's list needs no join into scheduling or identity,
    # and so it still reads correctly after a rename.
    doctor_name: str | None = Field(default=None, max_length=200)

    status: AdmissionRequestStatus = Field(default=AdmissionRequestStatus.PENDING, index=True)
    note: str | None = Field(
        default=None, max_length=500, description="What the desk needs to know, e.g. 'ICU bed'."
    )

    requested_at: datetime = Field(
        nullable=False,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    requested_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    requested_by_name: str | None = Field(default=None, max_length=200)

    # --- how it ended ------------------------------------------------------
    handled_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    handled_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    admission_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="admissions.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    cancellation_reason: str | None = Field(default=None, max_length=255)


class BedAssignment(AuditedTenantModel, table=True):
    """Who was in which bed, from when to when.

    Its own table, not a column on the admission, because a stay moves: casualty
    to ICU to a ward to a side room. The history is what answers "which bed was
    this patient in on day three", which infection control asks in earnest and a
    patient disputing a private-room charge asks in anger.

    Also what the room tariff is computed from — the ICU night and the general
    ward night are different money, and a single `bed_id` on the admission would
    silently bill the whole stay at whatever bed they happened to end in.
    """

    __tablename__ = "bed_assignments"
    __table_args__ = (
        # One live occupant per bed. The partial index is the actual guarantee
        # against a double-booked bed; the status check on `beds` is the
        # friendly error that normally gets there first.
        Index(
            "uq_bed_assignments_bed_live",
            "bed_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND released_at IS NULL"),
        ),
        # And one live bed per admission: a patient is in one place at a time.
        Index(
            "uq_bed_assignments_admission_live",
            "admission_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND released_at IS NULL"),
        ),
        Index("ix_bed_assignments_admission_id_assigned_at", "admission_id", "assigned_at"),
    )

    admission_id: uuid.UUID = Field(foreign_key="admissions.id", ondelete="RESTRICT", index=True)
    bed_id: uuid.UUID = Field(foreign_key="beds.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    # Snapshotted from the bed at assignment: a bed reclassified next year must
    # not retrospectively change what this patient was charged for.
    bed_class: BedClass = Field(default=BedClass.GENERAL, index=True)
    tariff_item_code: str | None = Field(default=None, max_length=32)

    assigned_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        index=True,
    )
    released_at: datetime | None = Field(
        default=None,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # Why they moved. "Stepped down from ICU" and "moved for infection control"
    # are different facts and both get asked about.
    transfer_reason: str | None = Field(default=None, max_length=255)
    assigned_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )

    @property
    def is_current(self) -> bool:
        return self.released_at is None


# ---------------------------------------------------------------------------
# The medication chart
# ---------------------------------------------------------------------------
class MedicationOrder(AuditedTenantModel, table=True):
    """A drug prescribed for an inpatient, with a schedule.

    Separate from `clinical.Order` on purpose. A clinical order is a one-off
    request that gets fulfilled once — a blood test, a scan. A medication order
    is a *standing instruction* that produces many administrations over days, and
    forcing it into the same row would mean either one order per dose (which
    loses "the patient is on amoxicillin") or one fulfilment for a course (which
    loses every individual dose).
    """

    __tablename__ = "medication_orders"
    __table_args__ = (
        Index("ix_medication_orders_admission_id_is_active", "admission_id", "is_active"),
        Index("ix_medication_orders_hospital_id_is_active", "hospital_id", "is_active"),
    )

    admission_id: uuid.UUID = Field(foreign_key="admissions.id", ondelete="RESTRICT", index=True)
    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    drug_name: str = Field(max_length=200, index=True)
    # Free text alongside the name: "500 mg", "1 g", "10 units". Not numeric,
    # because dosing is written in units the pharmacy and the nurse share and
    # normalising it here would lose "as per sliding scale".
    dose: str = Field(max_length=60)
    route: MedicationRoute = Field(default=MedicationRoute.ORAL)
    # Human-readable: "BD", "TDS", "QID", "STAT", "SOS". The machine-readable
    # part is `times_per_day` + `dose_times`, which is what materialises slots.
    frequency: str = Field(max_length=40)
    times_per_day: int = Field(default=1, ge=0, le=24)
    # Explicit clock times, e.g. ["08:00", "14:00", "20:00"]. Empty for STAT and
    # PRN. Stored rather than derived so a ward that gives its 8am round at 7am
    # can say so.
    dose_times: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # As-needed. Generates no scheduled slots — a PRN dose is recorded when it
    # is given, and a PRN that was never needed is not a missed dose.
    is_prn: bool = Field(default=False, index=True)
    prn_indication: str | None = Field(
        default=None, max_length=200, description="e.g. 'for temperature above 38'."
    )

    drug_schedule: DrugSchedule = Field(
        default=DrugSchedule.NONE,
        index=True,
        description="Drugs and Cosmetics Rules schedule (CLAUDE.md §9).",
    )

    starts_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        index=True,
    )
    # NULL for "until stopped". A course with an end date stops generating slots
    # on its own, which is one fewer thing for a busy ward to remember.
    ends_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    prescribed_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True, index=True
    )
    instructions: str | None = Field(default=None, max_length=500)

    is_active: bool = Field(default=True, index=True)
    stopped_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    stopped_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL", nullable=True
    )
    stop_reason: str | None = Field(default=None, max_length=255)

    @property
    def generates_slots(self) -> bool:
        return self.is_active and not self.is_prn and self.times_per_day > 0


class MedicationAdministration(AuditedTenantModel, table=True):
    """One slot on the chart: a dose that is due, and what became of it.

    Materialised **before** it is given. That is the design decision that makes
    this a medication administration record rather than a log of things that
    happened: the clinically significant event is a dose that was *not* given,
    and an absence cannot be a row in a table that only records administrations.
    A missed 6am antibiotic has to be visible at 8am without anyone thinking to
    look for it.
    """

    __tablename__ = "medication_administrations"
    __table_args__ = (
        # Idempotency for the materialiser: re-running it for the same day must
        # not double the chart. Partial so a cancelled slot can be regenerated
        # if the order is restarted.
        Index(
            "uq_medication_administrations_slot",
            "medication_order_id",
            "due_at",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status <> 'CANCELLED'"),
        ),
        # The nurse's screen: "what is due on this ward in this hour".
        Index("ix_medication_administrations_hospital_id_due_at", "hospital_id", "due_at"),
        Index("ix_medication_administrations_admission_id_due_at", "admission_id", "due_at"),
        Index("ix_medication_administrations_status_due_at", "status", "due_at"),
    )

    medication_order_id: uuid.UUID = Field(
        foreign_key="medication_orders.id", ondelete="CASCADE", index=True
    )
    admission_id: uuid.UUID = Field(foreign_key="admissions.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    # Snapshotted from the order, for the same reason a result snapshots its
    # reference range: a drug renamed or re-dosed next week must not rewrite what
    # this patient was given today.
    drug_name: str = Field(max_length=200)
    dose: str = Field(max_length=60)
    route: MedicationRoute = Field(default=MedicationRoute.ORAL)

    due_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
        index=True,
    )
    status: DoseStatus = Field(default=DoseStatus.DUE, index=True)

    administered_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    administered_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True, index=True
    )
    # Snapshotted so a chart printed in five years still names who gave the dose.
    administered_by_name: str | None = Field(default=None, max_length=200)

    # Required whenever the dose was not given. A blank reason on a missed
    # antibiotic is exactly the gap an incident review cannot close.
    reason: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=500)

    # True when the sweep set MISSED rather than a person recording an outcome.
    # Worth distinguishing: "the nurse said it was refused" and "nobody wrote
    # anything and the window closed" are different problems.
    auto_missed: bool = Field(default=False, index=True)

    @property
    def is_outstanding(self) -> bool:
        return self.status is DoseStatus.DUE


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
class DischargeSummary(AuditedTenantModel, table=True):
    """The discharge summary, assembled by the system and signed by a doctor.

    CLAUDE.md §7 in one table. The doctor's surface is *review and sign*, not
    *write*: every section below is compiled from rows the hospital already has —
    diagnoses from `clinical`, investigations from `diagnostics`, drugs from the
    chart above — and the doctor edits what is wrong rather than typing what is
    already known. A summary that takes forty minutes is a summary written at
    home a week later, or not at all.

    Sections are columns rather than one blob because they are separately
    editable, separately printable, and separately auditable; `compiled` keeps
    the raw structured payload the compiler produced, so it is possible to see
    what the system offered before a human changed it.
    """

    __tablename__ = "discharge_summaries"
    __table_args__ = (
        Index(
            "uq_discharge_summaries_admission_live",
            "admission_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status <> 'AMENDED'"),
        ),
        Index("ix_discharge_summaries_hospital_id_status", "hospital_id", "status"),
    )

    admission_id: uuid.UUID = Field(foreign_key="admissions.id", ondelete="RESTRICT", index=True)
    encounter_id: uuid.UUID = Field(foreign_key="encounters.id", ondelete="RESTRICT", index=True)
    patient_id: uuid.UUID = Field(foreign_key="patients.id", ondelete="RESTRICT", index=True)

    status: SummaryStatus = Field(default=SummaryStatus.DRAFT, index=True)

    # --- the sections -----------------------------------------------------
    presenting_complaint: str | None = Field(default=None)
    diagnoses: str | None = Field(default=None, description="Final diagnoses, one per line.")
    history: str | None = Field(default=None)
    examination: str | None = Field(default=None)
    course_in_hospital: str | None = Field(
        default=None, description="Compiled from progress and nursing notes."
    )
    investigations: str | None = Field(default=None, description="Compiled from diagnostics.")
    procedures: str | None = Field(default=None)
    treatment_given: str | None = Field(default=None, description="Compiled from the chart.")
    condition_at_discharge: str | None = Field(default=None, max_length=1000)
    discharge_medications: str | None = Field(default=None)
    follow_up_instructions: str | None = Field(default=None)
    diet_and_activity: str | None = Field(default=None, max_length=1000)
    # When to come back, and when to come back *urgently*. The second one is the
    # part that keeps a patient out of casualty at 3am, or gets them there in time.
    warning_signs: str | None = Field(default=None, max_length=1000)

    follow_up_date: date | None = Field(default=None, sa_type=Date, index=True)

    # What the compiler produced, before anyone edited it.
    compiled: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    compiled_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )

    # --- signature --------------------------------------------------------
    signed_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True
    )
    signed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # Snapshotted so a summary reprinted years later still names its signatory
    # and carries the registration number that makes it valid.
    signed_by_name: str | None = Field(default=None, max_length=200)
    signatory_registration_number: str | None = Field(default=None, max_length=64)

    amends_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="discharge_summaries.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    amendment_reason: str | None = Field(default=None, max_length=500)

    @property
    def is_signed(self) -> bool:
        return self.status in (SummaryStatus.FINAL, SummaryStatus.AMENDED)


class AdmissionSequence(TenantModel, table=True):
    """Per-hospital, per-financial-year counter for admission numbers.

    Same shape and reasoning as the UHID, token, encounter, accession and invoice
    counters: a Postgres SEQUENCE is global to the database and cannot restart
    per tenant per year, and this number is written on a wristband and read down
    a phone line.
    """

    __tablename__ = "admission_sequences"
    __table_args__ = (
        Index("uq_admission_sequences_hospital_year", "hospital_id", "year", unique=True),
    )

    year: int = Field(index=True, description="Financial year, April-March, as billing uses.")
    last_value: int = Field(default=0)


# Bed-day pricing falls back to this when a bed names no tariff item and no rate
# card covers it. Zero rather than a guess: `billing.capture_charge` records the
# act at zero and flags `needs_pricing`, which puts it on a worklist instead of
# inventing a number that ends up on a patient's bill.
DEFAULT_BED_RATE: Decimal = Decimal("0")
