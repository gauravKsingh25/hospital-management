"""Legal status changes for specimens and diagnostic reports.

Same discipline as `clinical/state_machine.py` and `scheduling/transitions.py`:
the moves are a table, not a chain of conditionals. The bug this prevents here
is specific and nasty — a verified report slipping back to "in progress" and
being quietly rewritten after a clinician has already acted on it.
"""

from __future__ import annotations

from typing import Final

from app.core.exceptions import IllegalStateTransitionError
from app.modules.diagnostics.models import ReportStatus, SpecimenStatus

__all__ = [
    "REPORT_TRANSITIONS",
    "SPECIMEN_TRANSITIONS",
    "TERMINAL_REPORT_STATUSES",
    "TERMINAL_SPECIMEN_STATUSES",
    "assert_report_transition",
    "assert_specimen_transition",
]


SPECIMEN_TRANSITIONS: Final[dict[SpecimenStatus, frozenset[SpecimenStatus]]] = {
    SpecimenStatus.PENDING_COLLECTION: frozenset(
        {
            SpecimenStatus.COLLECTED,
            SpecimenStatus.CANCELLED,
        }
    ),
    SpecimenStatus.COLLECTED: frozenset(
        {
            SpecimenStatus.RECEIVED,
            # Broken in transit, wrong tube, unlabelled. The patient gets stuck
            # again, so this is a recorded event with a reason, not a deletion.
            SpecimenStatus.REJECTED,
            SpecimenStatus.CANCELLED,
        }
    ),
    SpecimenStatus.RECEIVED: frozenset(
        {
            # Haemolysis and clotting are often only found at the bench, after
            # the sample has been booked in.
            SpecimenStatus.REJECTED,
        }
    ),
    SpecimenStatus.REJECTED: frozenset(),
    SpecimenStatus.CANCELLED: frozenset(),
}

TERMINAL_SPECIMEN_STATUSES: Final[frozenset[SpecimenStatus]] = frozenset(
    status for status, allowed in SPECIMEN_TRANSITIONS.items() if not allowed
)


REPORT_TRANSITIONS: Final[dict[ReportStatus, frozenset[ReportStatus]]] = {
    ReportStatus.REGISTERED: frozenset(
        {
            ReportStatus.IN_PROGRESS,
            ReportStatus.CANCELLED,
        }
    ),
    ReportStatus.IN_PROGRESS: frozenset(
        {
            # Released before verification, clearly labelled provisional: a
            # clinician waiting on a potassium should see it now rather than
            # nothing at all until someone is free to sign.
            ReportStatus.PRELIMINARY,
            ReportStatus.FINAL,
            ReportStatus.CANCELLED,
        }
    ),
    ReportStatus.PRELIMINARY: frozenset(
        {
            ReportStatus.FINAL,
            ReportStatus.CANCELLED,
        }
    ),
    # A verified report never goes backwards. The only way to change it is to
    # supersede it, which leaves both versions and a reason.
    ReportStatus.FINAL: frozenset({ReportStatus.AMENDED}),
    ReportStatus.AMENDED: frozenset(),
    ReportStatus.CANCELLED: frozenset(),
}

TERMINAL_REPORT_STATUSES: Final[frozenset[ReportStatus]] = frozenset(
    status for status, allowed in REPORT_TRANSITIONS.items() if not allowed
)


def _phrase(value: str) -> str:
    return value.lower().replace("_", " ")


def assert_specimen_transition(current: SpecimenStatus, target: SpecimenStatus) -> None:
    _assert(current.value, target.value, SPECIMEN_TRANSITIONS.get(current, frozenset()), "sample")


def assert_report_transition(current: ReportStatus, target: ReportStatus) -> None:
    _assert(current.value, target.value, REPORT_TRANSITIONS.get(current, frozenset()), "report")


def _assert(current: str, target: str, allowed: frozenset[object], noun: str) -> None:
    """Shared refusal, phrased for the person reading the screen."""
    allowed_values = sorted(str(getattr(item, "value", item)) for item in allowed)

    if current == target:
        raise IllegalStateTransitionError(
            f"The {noun} is already {_phrase(current)}.",
            details={"from": current, "to": target},
        )
    if target not in allowed_values:
        detail = (
            f"A {noun} that is {_phrase(current)} cannot become {_phrase(target)}."
            if allowed_values
            else f"A {noun} that is {_phrase(current)} is closed and cannot change."
        )
        raise IllegalStateTransitionError(
            detail,
            details={"from": current, "to": target, "allowed": allowed_values},
        )
