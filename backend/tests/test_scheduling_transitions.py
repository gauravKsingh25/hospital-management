"""The appointment and queue transition tables.

Pure logic, no database. These rules are what stop a cancelled appointment
quietly reopening or a completed consultation going back to the waiting room,
so they are pinned exhaustively rather than by example.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import IllegalStateTransitionError
from app.modules.scheduling.models import AppointmentStatus, QueueStatus
from app.modules.scheduling.transitions import (
    APPOINTMENT_TRANSITIONS,
    QUEUE_TRANSITIONS,
    TERMINAL_APPOINTMENT_STATUSES,
    assert_appointment_transition,
    assert_queue_transition,
)


class TestAppointmentTransitions:
    def test_every_status_has_a_rule(self) -> None:
        """A status missing from the table would be silently un-transitionable."""
        assert set(APPOINTMENT_TRANSITIONS) == set(AppointmentStatus)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (AppointmentStatus.SCHEDULED, AppointmentStatus.CHECKED_IN),
            (AppointmentStatus.SCHEDULED, AppointmentStatus.CANCELLED),
            (AppointmentStatus.SCHEDULED, AppointmentStatus.NO_SHOW),
            (AppointmentStatus.CHECKED_IN, AppointmentStatus.IN_CONSULTATION),
            (AppointmentStatus.CHECKED_IN, AppointmentStatus.NO_SHOW),
            (AppointmentStatus.IN_CONSULTATION, AppointmentStatus.COMPLETED),
        ],
    )
    def test_the_normal_paths_are_allowed(
        self, current: AppointmentStatus, target: AppointmentStatus
    ) -> None:
        assert_appointment_transition(current, target)

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_APPOINTMENT_STATUSES))
    def test_terminal_statuses_never_reopen(self, terminal: AppointmentStatus) -> None:
        """A closed visit must not be rewritable; a correction is a new record."""
        for target in AppointmentStatus:
            if target is terminal:
                continue
            with pytest.raises(IllegalStateTransitionError):
                assert_appointment_transition(terminal, target)

    def test_a_visit_cannot_skip_check_in(self) -> None:
        with pytest.raises(IllegalStateTransitionError):
            assert_appointment_transition(
                AppointmentStatus.SCHEDULED, AppointmentStatus.IN_CONSULTATION
            )

    def test_a_visit_cannot_complete_without_being_seen(self) -> None:
        with pytest.raises(IllegalStateTransitionError):
            assert_appointment_transition(AppointmentStatus.CHECKED_IN, AppointmentStatus.COMPLETED)

    def test_repeating_the_current_status_is_refused_clearly(self) -> None:
        with pytest.raises(IllegalStateTransitionError, match="already"):
            assert_appointment_transition(AppointmentStatus.CANCELLED, AppointmentStatus.CANCELLED)

    def test_the_error_says_what_would_have_been_allowed(self) -> None:
        """Reception is being told 'no' by a screen; someone has to explain it."""
        with pytest.raises(IllegalStateTransitionError) as excinfo:
            assert_appointment_transition(AppointmentStatus.SCHEDULED, AppointmentStatus.COMPLETED)
        assert excinfo.value.details["allowed"]
        assert "CHECKED_IN" in excinfo.value.details["allowed"]


class TestQueueTransitions:
    def test_every_status_has_a_rule(self) -> None:
        assert set(QUEUE_TRANSITIONS) == set(QueueStatus)

    def test_a_skipped_patient_can_come_back(self) -> None:
        """Not in the corridor at that second is not the same as gone home."""
        assert_queue_transition(QueueStatus.SKIPPED, QueueStatus.WAITING)
        assert_queue_transition(QueueStatus.SKIPPED, QueueStatus.CALLED)

    def test_reception_can_send_a_patient_straight_in(self) -> None:
        assert_queue_transition(QueueStatus.WAITING, QueueStatus.IN_CONSULTATION)

    def test_a_completed_token_is_final(self) -> None:
        for target in QueueStatus:
            if target is QueueStatus.COMPLETED:
                continue
            with pytest.raises(IllegalStateTransitionError):
                assert_queue_transition(QueueStatus.COMPLETED, target)

    def test_a_patient_who_left_cannot_be_consulted(self) -> None:
        with pytest.raises(IllegalStateTransitionError):
            assert_queue_transition(
                QueueStatus.LEFT_WITHOUT_BEING_SEEN, QueueStatus.IN_CONSULTATION
            )
