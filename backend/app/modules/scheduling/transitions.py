"""Legal status changes for appointments and queue entries.

CLAUDE.md §14 makes "no status is settable outside the state machine" an
invariant for the Encounter. The same discipline is applied here, one module
early, for two reasons: the bugs it prevents are identical (a cancelled
appointment silently reopening, a completed visit going back to waiting), and
`clinical` will implement the real Encounter machine against the same shape, so
the pattern is worth having settled before it matters most.

Transitions are data, not `if` statements. A table can be read, tested and
printed; a chain of conditionals scattered across service functions cannot.
"""

from __future__ import annotations

from typing import Final

from app.core.exceptions import IllegalStateTransitionError
from app.modules.scheduling.models import AppointmentStatus, QueueStatus

__all__ = [
    "APPOINTMENT_TRANSITIONS",
    "QUEUE_TRANSITIONS",
    "TERMINAL_APPOINTMENT_STATUSES",
    "TERMINAL_QUEUE_STATUSES",
    "assert_appointment_transition",
    "assert_queue_transition",
    "is_terminal_appointment_status",
]

# From -> the set of statuses it may legally become.
APPOINTMENT_TRANSITIONS: Final[dict[AppointmentStatus, frozenset[AppointmentStatus]]] = {
    AppointmentStatus.SCHEDULED: frozenset(
        {
            AppointmentStatus.CHECKED_IN,
            AppointmentStatus.CANCELLED,
            AppointmentStatus.NO_SHOW,
            AppointmentStatus.RESCHEDULED,
        }
    ),
    AppointmentStatus.CHECKED_IN: frozenset(
        {
            AppointmentStatus.IN_CONSULTATION,
            # A patient who checks in and then leaves before being seen is a
            # no-show in every way that matters to the clinic's day.
            AppointmentStatus.NO_SHOW,
            AppointmentStatus.CANCELLED,
        }
    ),
    AppointmentStatus.IN_CONSULTATION: frozenset(
        {
            AppointmentStatus.COMPLETED,
            # The doctor was interrupted and the patient left. Rare, real.
            AppointmentStatus.NO_SHOW,
        }
    ),
    # Everything below is terminal. Reopening a closed visit would let a
    # completed consultation be silently rewritten; a correction is a new
    # appointment with a reason, which leaves a trail.
    AppointmentStatus.COMPLETED: frozenset(),
    AppointmentStatus.CANCELLED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
    AppointmentStatus.RESCHEDULED: frozenset(),
}

TERMINAL_APPOINTMENT_STATUSES: Final[frozenset[AppointmentStatus]] = frozenset(
    status for status, allowed in APPOINTMENT_TRANSITIONS.items() if not allowed
)

QUEUE_TRANSITIONS: Final[dict[QueueStatus, frozenset[QueueStatus]]] = {
    QueueStatus.WAITING: frozenset(
        {
            QueueStatus.CALLED,
            # Reception can send a patient straight in when the doctor is free.
            QueueStatus.IN_CONSULTATION,
            QueueStatus.LEFT_WITHOUT_BEING_SEEN,
        }
    ),
    QueueStatus.CALLED: frozenset(
        {
            QueueStatus.IN_CONSULTATION,
            # Called and did not come forward. Recoverable: they go back in the
            # queue rather than being written off, because "not in the corridor
            # at that second" is not the same as "gone".
            QueueStatus.SKIPPED,
            QueueStatus.LEFT_WITHOUT_BEING_SEEN,
        }
    ),
    QueueStatus.SKIPPED: frozenset(
        {
            QueueStatus.WAITING,
            QueueStatus.CALLED,
            QueueStatus.LEFT_WITHOUT_BEING_SEEN,
        }
    ),
    QueueStatus.IN_CONSULTATION: frozenset({QueueStatus.COMPLETED}),
    QueueStatus.COMPLETED: frozenset(),
    QueueStatus.LEFT_WITHOUT_BEING_SEEN: frozenset(),
}

TERMINAL_QUEUE_STATUSES: Final[frozenset[QueueStatus]] = frozenset(
    status for status, allowed in QUEUE_TRANSITIONS.items() if not allowed
)


def is_terminal_appointment_status(status: AppointmentStatus) -> bool:
    return status in TERMINAL_APPOINTMENT_STATUSES


def assert_appointment_transition(current: AppointmentStatus, target: AppointmentStatus) -> None:
    """Raise unless `current -> target` is allowed.

    The error names both states, because the caller who hits this is usually a
    receptionist being told "no" by a screen and someone has to be able to
    explain why.
    """
    if target == current:
        raise IllegalStateTransitionError(
            f"The appointment is already {current.value.lower().replace('_', ' ')}.",
            details={"from": current.value, "to": target.value},
        )
    if target not in APPOINTMENT_TRANSITIONS.get(current, frozenset()):
        raise IllegalStateTransitionError(
            f"An appointment that is {current.value.lower().replace('_', ' ')} "
            f"cannot become {target.value.lower().replace('_', ' ')}.",
            details={
                "from": current.value,
                "to": target.value,
                "allowed": sorted(
                    status.value for status in APPOINTMENT_TRANSITIONS.get(current, frozenset())
                ),
            },
        )


def assert_queue_transition(current: QueueStatus, target: QueueStatus) -> None:
    if target == current:
        raise IllegalStateTransitionError(
            f"The token is already {current.value.lower().replace('_', ' ')}.",
            details={"from": current.value, "to": target.value},
        )
    if target not in QUEUE_TRANSITIONS.get(current, frozenset()):
        raise IllegalStateTransitionError(
            f"A token that is {current.value.lower().replace('_', ' ')} "
            f"cannot become {target.value.lower().replace('_', ' ')}.",
            details={
                "from": current.value,
                "to": target.value,
                "allowed": sorted(
                    status.value for status in QUEUE_TRANSITIONS.get(current, frozenset())
                ),
            },
        )
