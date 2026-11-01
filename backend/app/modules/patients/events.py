"""Domain events published by `patients`.

Kept thin, as everywhere: identifiers and the few facts a subscriber cannot
cheaply re-derive.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.events import DomainEvent

__all__ = ["PatientBulkReadDetected"]


@dataclass(frozen=True, kw_only=True, slots=True)
class PatientBulkReadDetected(DomainEvent):
    """One staff member has read an unusual number of patient records (SEC-01).

    Published once per person per hour, the first time their count of distinct
    patients crosses `PATIENT_BULK_READ_ALERT`. Not proof of anything — a
    records officer reconciling files reads hundreds legitimately — so it asks
    an administrator to look, rather than locking anybody out.
    """

    user_id: uuid.UUID
    user_name: str
    distinct_patients: int
