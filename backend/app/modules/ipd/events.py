"""Domain events published by `ipd`.

Thin, like every other module's: identifiers and the few facts a subscriber
cannot cheaply re-derive.

`BedDayAccrued` is the one carrying real weight. `billing` subscribes to it
transactionally to put the room charge on the running bill, which is how
CLAUDE.md §7b's *"charges flow automatically … reception reviews the invoice,
never rebuilds it"* holds for an inpatient stay. It is emitted once per
admission per night by the worker sweep, and `accrual_key` makes that repeatable:
the same night re-emitted after a restart collapses onto the same charge instead
of billing the patient twice for one bed.

`notifications` already subscribes to `clinical.EncounterClosed`, which fires
when the stay ends — so a discharge produces its follow-up message without this
module knowing that messaging exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from app.core.events import DomainEvent

__all__ = [
    "BedDayAccrued",
    "BedReleased",
    "DischargeSummarySigned",
    "MedicationAdministered",
    "MedicationDoseMissed",
    "PatientAdmitted",
    "PatientDischarged",
    "PatientTransferred",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientAdmitted(DomainEvent):
    """A stay began. The bed is taken and the meter is running."""

    admission_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    admission_number: str
    bed_id: uuid.UUID
    ward_id: uuid.UUID
    bed_class: str
    attending_doctor_id: uuid.UUID | None


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientTransferred(DomainEvent):
    """Moved between beds — often between classes, which changes the tariff."""

    admission_id: uuid.UUID
    patient_id: uuid.UUID
    from_bed_id: uuid.UUID
    to_bed_id: uuid.UUID
    from_bed_class: str
    to_bed_class: str
    reason: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class BedDayAccrued(DomainEvent):
    """One night in one bed, ready to be charged.

    `accrual_key` is a deterministic UUID derived from (admission, date), so the
    charge `billing` captures is idempotent against a re-run: a sweep that
    restarts mid-pass, or runs twice after a deploy, cannot bill two nights for
    one. Same property `capture_charge` relies on everywhere else, arrived at
    without needing a row to hang it on.
    """

    admission_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    accrual_key: uuid.UUID
    service_date: date
    bed_class: str
    tariff_item_code: str | None
    nights: int
    description: str


@dataclass(frozen=True, kw_only=True, slots=True)
class BedReleased(DomainEvent):
    """A bed is free of its patient — but not yet available.

    Housekeeping picks this up. Deliberately not "available": the bed is in
    `CLEANING`, and a board that skips that rung is a board that sends the next
    patient to an unmade bed.
    """

    bed_id: uuid.UUID
    ward_id: uuid.UUID
    admission_id: uuid.UUID
    bed_code: str


@dataclass(frozen=True, kw_only=True, slots=True)
class MedicationAdministered(DomainEvent):
    """A dose was given. `billing` will want this once pharmacy lands."""

    administration_id: uuid.UUID
    admission_id: uuid.UUID
    patient_id: uuid.UUID
    encounter_id: uuid.UUID
    drug_name: str
    dose: str
    route: str
    administered_by_id: uuid.UUID | None


@dataclass(frozen=True, kw_only=True, slots=True)
class MedicationDoseMissed(DomainEvent):
    """A scheduled dose passed its window with nothing recorded.

    Its own event rather than a status nobody reads, because this is the thing a
    medication chart exists to surface. `auto` distinguishes the sweep noticing
    an absence from a nurse recording one — the first is a process failure worth
    escalating, the second is documented care.
    """

    administration_id: uuid.UUID
    admission_id: uuid.UUID
    patient_id: uuid.UUID
    drug_name: str
    due_at: datetime
    auto: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientDischarged(DomainEvent):
    """The stay ended, however it ended.

    `discharge_type` carries which — recovered, referred, LAMA, deceased — so a
    subscriber does not have to infer it from the encounter's terminal status.
    """

    admission_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    admission_number: str
    discharge_type: str
    length_of_stay_days: int


@dataclass(frozen=True, kw_only=True, slots=True)
class DischargeSummarySigned(DomainEvent):
    summary_id: uuid.UUID
    admission_id: uuid.UUID
    patient_id: uuid.UUID
    signed_by_id: uuid.UUID | None
