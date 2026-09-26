"""Domain events published by `clinical`.

These are the module's second public surface, and the seam along which billing,
notifications and diagnostics stay decoupled from the core (CLAUDE.md §2):

* `billing` subscribes to `EncounterClosed` to run the settlement flow, and to
  `OrderPlaced` to capture a charge.
* `notifications` subscribes to `EncounterClosed` for follow-up reminders and to
  `PatientDeceased` to **suppress** them — the §14 invariant.
* `diagnostics` subscribes to `OrderPlaced` to open its own work item.

Kept thin on purpose: an event carries identifiers and the few facts a
subscriber cannot cheaply re-derive, never a copy of the record. A fat event is
a second schema that diverges silently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.core.events import DomainEvent

__all__ = [
    "EncounterAdmitted",
    "EncounterClosed",
    "EncounterOpened",
    "EncounterStatusChanged",
    "OrderCancelled",
    "OrderPlaced",
    "PatientDeceased",
    "PatientLeftAgainstAdvice",
    "PatientReferredOut",
    "VitalsRecorded",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class EncounterOpened(DomainEvent):
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    encounter_number: str
    encounter_type: str
    doctor_id: uuid.UUID | None
    appointment_id: uuid.UUID | None


@dataclass(frozen=True, kw_only=True, slots=True)
class EncounterStatusChanged(DomainEvent):
    """Published on every transition, without exception.

    Statuses travel as plain strings rather than the enum so a subscriber that
    has been extracted into its own service does not need to import this
    module's Python to understand the message.
    """

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    from_status: str
    to_status: str
    reason: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class EncounterAdmitted(DomainEvent):
    """The OPD visit became an inpatient stay. `ipd` (Phase 9) takes it from here."""

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    from_status: str
    # Carried so `scheduling` can close the OPD token without looking the
    # encounter back up. A patient who has gone to a ward is not still waiting
    # in the corridor, and the board has to stop saying they are.
    appointment_id: uuid.UUID | None = None
    # Whether a doctor actually started the consultation. The same definition
    # `reporting` uses for "seen", carried here so the queue and the dashboard
    # cannot disagree about how many patients were seen today.
    was_seen: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class EncounterClosed(DomainEvent):
    """A visit reached a terminal status, however it ended.

    `requires_settlement` is true for deceased and referred patients as well as
    completed ones: they usually still owe money, and CLAUDE.md §6 requires the
    settlement flow to run rather than the bill quietly disappearing with the
    patient.
    """

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    final_status: str
    requires_settlement: bool
    closed_automatically: bool
    # See `EncounterAdmitted.appointment_id`. Every terminal ending has to take
    # the token with it — including the three unplanned ones, which is where
    # this was found: a deceased patient stayed first in the OPD queue.
    appointment_id: uuid.UUID | None = None
    was_seen: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientDeceased(DomainEvent):
    """A death was recorded.

    Every notification channel must treat this as a hard stop for that patient
    (CLAUDE.md §14). The suppression itself does not rely on this event arriving
    — `patients.is_deceased` is set in the same transaction — but subscribers
    that cache anything need to hear about it.
    """

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    died_at: datetime
    cause_of_death: str
    certified_by_id: uuid.UUID


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientReferredOut(DomainEvent):
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    referred_to_facility: str
    referral_reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientLeftAgainstAdvice(DomainEvent):
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    lama_reason: str
    # Whether the patient signed the discharge-against-advice form. An unsigned
    # LAMA is the hospital's exposure, so it is reportable, not a footnote.
    form_signed: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class OrderPlaced(DomainEvent):
    """A doctor ordered something. `diagnostics` and `billing` both want this."""

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    order_id: uuid.UUID
    order_type: str
    item_code: str | None
    item_name: str
    priority: str


@dataclass(frozen=True, kw_only=True, slots=True)
class OrderCancelled(DomainEvent):
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    order_id: uuid.UUID
    order_type: str
    reason: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class VitalsRecorded(DomainEvent):
    """A nurse recorded observations.

    `is_abnormal` travels with it so an escalation rule can fire without
    re-reading the row or re-deriving the thresholds.
    """

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    vitals_id: uuid.UUID
    is_abnormal: bool
