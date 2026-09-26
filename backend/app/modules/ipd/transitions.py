"""Legal status changes for beds and admissions.

Same discipline as `clinical/state_machine.py`, `scheduling/transitions.py` and
`diagnostics/transitions.py`: the moves are a table, not a chain of conditionals.

The bug this prevents on the bed side is specific and expensive — a bed going
straight from `OCCUPIED` to `AVAILABLE`, skipping `CLEANING`, so the board says
free and the next patient is walked to an unmade bed. That is the exact failure
CLAUDE.md §7b names when it asks for a bed cleaning lifecycle "so bed
availability is never wrong", and the way to prevent it is to make the shortcut
unrepresentable rather than merely discouraged.
"""

from __future__ import annotations

from typing import Final

from app.core.exceptions import IllegalStateTransitionError
from app.modules.ipd.models import AdmissionStatus, BedStatus

__all__ = [
    "ADMISSION_TRANSITIONS",
    "BED_TRANSITIONS",
    "TERMINAL_ADMISSION_STATUSES",
    "assert_admission_transition",
    "assert_bed_transition",
]


BED_TRANSITIONS: Final[dict[BedStatus, frozenset[BedStatus]]] = {
    BedStatus.AVAILABLE: frozenset(
        {
            BedStatus.OCCUPIED,
            BedStatus.RESERVED,
            BedStatus.OUT_OF_SERVICE,
            # Housekeeping may decide an idle bed needs turning over again.
            BedStatus.CLEANING,
        }
    ),
    BedStatus.RESERVED: frozenset(
        {
            BedStatus.OCCUPIED,
            # The reservation lapsed or the case was cancelled.
            BedStatus.AVAILABLE,
            BedStatus.OUT_OF_SERVICE,
        }
    ),
    BedStatus.OCCUPIED: frozenset(
        {
            # The only ordinary way out. Note `AVAILABLE` is deliberately NOT
            # here: a bed a patient has just left is not a bed the next patient
            # can be sent to, and making that unrepresentable is the point of
            # this table.
            BedStatus.CLEANING,
            # A bed condemned while occupied — a broken rail, a contamination
            # incident. The patient is moved, and the bed does not go back into
            # circulation on the way.
            BedStatus.OUT_OF_SERVICE,
        }
    ),
    BedStatus.CLEANING: frozenset(
        {
            BedStatus.AVAILABLE,
            BedStatus.OUT_OF_SERVICE,
        }
    ),
    BedStatus.OUT_OF_SERVICE: frozenset(
        {
            # Back from maintenance still needs cleaning before it is offered.
            BedStatus.CLEANING,
            BedStatus.AVAILABLE,
        }
    ),
}


ADMISSION_TRANSITIONS: Final[dict[AdmissionStatus, frozenset[AdmissionStatus]]] = {
    AdmissionStatus.ADMITTED: frozenset(
        {
            AdmissionStatus.DISCHARGE_INITIATED,
            # A death or a self-discharge ends the stay without anybody writing
            # a discharge order first.
            AdmissionStatus.DISCHARGED,
            # Admitted in error — wrong patient, duplicate record. Distinct from
            # discharged: nothing happened, and the bed-day should not be billed.
            AdmissionStatus.CANCELLED,
        }
    ),
    AdmissionStatus.DISCHARGE_INITIATED: frozenset(
        {
            AdmissionStatus.DISCHARGED,
            # The discharge was called off — a patient who deteriorated between
            # the doctor's signature and the front door.
            AdmissionStatus.ADMITTED,
        }
    ),
    AdmissionStatus.DISCHARGED: frozenset(),
    AdmissionStatus.CANCELLED: frozenset(),
}

TERMINAL_ADMISSION_STATUSES: Final[frozenset[AdmissionStatus]] = frozenset(
    status for status, allowed in ADMISSION_TRANSITIONS.items() if not allowed
)


def _phrase(value: str) -> str:
    return value.lower().replace("_", " ")


def assert_bed_transition(current: BedStatus, target: BedStatus) -> None:
    _assert(current.value, target.value, BED_TRANSITIONS.get(current, frozenset()), "bed")


def assert_admission_transition(current: AdmissionStatus, target: AdmissionStatus) -> None:
    _assert(
        current.value, target.value, ADMISSION_TRANSITIONS.get(current, frozenset()), "admission"
    )


def _assert(current: str, target: str, allowed: frozenset[object], noun: str) -> None:
    """Shared refusal, phrased for the person reading the screen."""
    allowed_values = sorted(str(getattr(item, "value", item)) for item in allowed)

    if current == target:
        raise IllegalStateTransitionError(
            f"The {noun} is already {_phrase(current)}.",
            details={"from": current, "to": target},
        )
    if target not in allowed_values:
        # The occupied-to-available shortcut gets its own sentence, because the
        # generic one reads like a system quirk and this one is a real rule
        # somebody at a nursing station needs to understand immediately.
        if (
            noun == "bed"
            and current == BedStatus.OCCUPIED.value
            and target == BedStatus.AVAILABLE.value
        ):
            detail = (
                "A bed cannot go straight from occupied to available. "
                "Release the patient, then mark it cleaned."
            )
        else:
            detail = (
                f"A {noun} that is {_phrase(current)} cannot become {_phrase(target)}."
                if allowed_values
                else f"A {noun} that is {_phrase(current)} is closed and cannot change."
            )
        raise IllegalStateTransitionError(
            detail,
            details={"from": current, "to": target, "allowed": allowed_values},
        )
