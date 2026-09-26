"""The Encounter state machine, tested without a database.

CLAUDE.md §14 makes "no status is settable outside the state machine" an
invariant to guard actively. These tests guard the table itself — legality,
terminality, and the metadata a terminal outcome must carry — so a future edit
that quietly reopens a completed visit fails here rather than in a hospital.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import IllegalStateTransitionError
from app.modules.clinical.models import EncounterStatus
from app.modules.clinical.service import ACTIVE_STATUSES
from app.modules.clinical.state_machine import (
    ENCOUNTER_TRANSITIONS,
    REQUIRED_METADATA,
    TERMINAL_STATUSES,
    assert_transition,
    is_terminal,
)

S = EncounterStatus


def test_every_status_appears_in_the_table() -> None:
    """A status with no entry would be a silent dead end."""
    assert set(EncounterStatus) == set(ENCOUNTER_TRANSITIONS)


def test_the_table_only_targets_real_statuses() -> None:
    for allowed in ENCOUNTER_TRANSITIONS.values():
        assert allowed <= set(EncounterStatus)


def test_terminal_set_matches_claude_md() -> None:
    """CLAUDE.md §6 names exactly these as ends of the journey."""
    assert {
        S.COMPLETED,
        S.CANCELLED,
        S.NO_SHOW,
        S.REFERRED_OUT,
        S.LAMA,
        S.DECEASED,
    } == TERMINAL_STATUSES


def test_active_statuses_are_exactly_the_non_terminal_ones_bar_admitted() -> None:
    """`ACTIVE_STATUSES` drives the work lists and the auto-close sweep.

    It is stated separately from the transition table (deriving it would be
    circular), so this asserts the two agree. ADMITTED is deliberately absent:
    an inpatient on day five is not a stale OPD visit.
    """
    non_terminal = {status for status in EncounterStatus if not is_terminal(status)}
    assert set(ACTIVE_STATUSES) == non_terminal - {S.ADMITTED}


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_statuses_never_reopen(status: EncounterStatus) -> None:
    """A closed visit that can reopen is a closed visit that can be rewritten."""
    assert ENCOUNTER_TRANSITIONS[status] == frozenset()

    with pytest.raises(IllegalStateTransitionError) as error:
        assert_transition(status, S.IN_CONSULTATION)
    assert error.value.details["allowed"] == []


@pytest.mark.parametrize(
    "origin",
    [S.REGISTERED, S.IN_CONSULTATION, S.AWAITING_RESULTS, S.PENDING_CLEARANCE, S.ADMITTED],
)
@pytest.mark.parametrize("exit_status", [S.DECEASED, S.REFERRED_OUT, S.LAMA])
def test_unplanned_exits_reachable_from_every_active_state(
    origin: EncounterStatus, exit_status: EncounterStatus
) -> None:
    """CLAUDE.md §6: death and referral are enterable "from most active states".

    ADMITTED is included on purpose — most in-hospital deaths happen on a ward,
    not in a consulting room.
    """
    assert_transition(origin, exit_status)


def test_the_opd_happy_path() -> None:
    assert_transition(S.REGISTERED, S.IN_CONSULTATION)
    assert_transition(S.IN_CONSULTATION, S.COMPLETED)


def test_the_pending_clearance_path() -> None:
    assert_transition(S.IN_CONSULTATION, S.PENDING_CLEARANCE)
    assert_transition(S.PENDING_CLEARANCE, S.COMPLETED)


def test_results_send_the_patient_back_to_the_doctor() -> None:
    """The reason AWAITING_RESULTS is distinct from PENDING_CLEARANCE."""
    assert_transition(S.IN_CONSULTATION, S.AWAITING_RESULTS)
    assert_transition(S.AWAITING_RESULTS, S.IN_CONSULTATION)


def test_a_registered_patient_can_be_admitted_directly() -> None:
    """Casualty admits without an OPD consultation in between."""
    assert_transition(S.REGISTERED, S.ADMITTED)


def test_an_admitted_patient_cannot_go_back_to_the_waiting_room() -> None:
    with pytest.raises(IllegalStateTransitionError):
        assert_transition(S.ADMITTED, S.IN_CONSULTATION)


def test_transition_to_the_same_status_is_refused_clearly() -> None:
    with pytest.raises(IllegalStateTransitionError) as error:
        assert_transition(S.IN_CONSULTATION, S.IN_CONSULTATION)
    assert "already" in str(error.value)


def test_error_names_both_states_and_the_alternatives() -> None:
    """The person who hits this is a receptionist being told "no" by a screen."""
    with pytest.raises(IllegalStateTransitionError) as error:
        assert_transition(S.REGISTERED, S.COMPLETED)

    details = error.value.details
    assert details["from"] == "REGISTERED"
    assert details["to"] == "COMPLETED"
    assert "IN_CONSULTATION" in details["allowed"]
    assert "registered" in str(error.value) and "completed" in str(error.value)


def test_required_metadata_covers_every_outcome_that_needs_explaining() -> None:
    """CLAUDE.md §6 requires structured metadata on death, referral and LAMA."""
    assert set(REQUIRED_METADATA) == {S.DECEASED, S.REFERRED_OUT, S.LAMA}
    # The three facts a death record is worthless without.
    assert set(REQUIRED_METADATA[S.DECEASED]) == {
        "deceased_at",
        "death_certified_by_id",
        "cause_of_death",
    }
