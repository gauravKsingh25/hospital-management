"""Domain events published by `diagnostics`.

`notifications` (Phase 8) subscribes to `ReportReady` for the "your report is
ready" message CLAUDE.md §13 step 6 asks for, and to `CriticalResultFlagged`
for the escalation that cannot wait for anyone to refresh a screen. `billing`
subscribes to `ReportReady` to capture the investigation charge.

What does *not* travel by event: closing the clinical order. That is a direct
call into `clinical.service`, because a swallowed handler there would leave a
visit permanently open with its result already filed — see `service.verify_report`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.events import DomainEvent

__all__ = [
    "CriticalResultFlagged",
    "ReportAmended",
    "ReportReady",
    "SpecimenCollected",
    "SpecimenRejected",
]


@dataclass(frozen=True, kw_only=True, slots=True)
class SpecimenCollected(DomainEvent):
    specimen_id: uuid.UUID
    order_id: uuid.UUID
    patient_id: uuid.UUID
    accession_number: str
    specimen_type: str


@dataclass(frozen=True, kw_only=True, slots=True)
class SpecimenRejected(DomainEvent):
    """The patient has to be stuck again. Reception and the ward both need to know."""

    specimen_id: uuid.UUID
    order_id: uuid.UUID
    patient_id: uuid.UUID
    accession_number: str
    reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ReportReady(DomainEvent):
    """A verified report a clinician may act on (CLAUDE.md §13 step 6).

    Published only on `FINAL`. A preliminary result is deliberately not
    announced this way: "your report is ready" about a number that may still
    change is worse than saying nothing.
    """

    report_id: uuid.UUID
    order_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    report_number: str
    test_name: str
    discipline: str
    has_critical_result: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class CriticalResultFlagged(DomainEvent):
    """A value outside the panic limits.

    Fired as soon as the value is entered, before verification — a potassium of
    7.2 does not wait for a signature. NABH requires the resulting callback to
    be documented, which is what `DiagnosticReport.critical_notified_*` records.
    """

    report_id: uuid.UUID
    order_id: uuid.UUID
    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    test_name: str
    # e.g. ["Potassium 7.2 mmol/L (CRITICAL_HIGH)"] — enough for an alert to be
    # actionable without a second query.
    critical_values: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True, slots=True)
class ReportAmended(DomainEvent):
    """A verified report was superseded.

    Carried separately from `ReportReady` because the clinical consequence is
    different: somebody may already have acted on the old numbers.
    """

    report_id: uuid.UUID
    superseded_report_id: uuid.UUID
    order_id: uuid.UUID
    patient_id: uuid.UUID
    reason: str
