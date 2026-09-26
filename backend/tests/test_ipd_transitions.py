"""The bed and admission state tables.

No database: these are pure table lookups, and the point of them being a table
is that they can be tested exhaustively rather than by example. The test that
matters most is `test_an_occupied_bed_cannot_become_available_directly` — that
single forbidden edge is the whole reason the bed lifecycle has a `CLEANING`
rung, and CLAUDE.md §7b asks for it by name.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import IllegalStateTransitionError
from app.modules.ipd.models import AdmissionStatus, BedStatus
from app.modules.ipd.transitions import (
    ADMISSION_TRANSITIONS,
    BED_TRANSITIONS,
    TERMINAL_ADMISSION_STATUSES,
    assert_admission_transition,
    assert_bed_transition,
)


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------
def test_every_bed_status_appears_in_the_table() -> None:
    """A status missing from the table is a bed that can never leave it."""
    assert set(BED_TRANSITIONS) == set(BedStatus)


def test_every_admission_status_appears_in_the_table() -> None:
    assert set(ADMISSION_TRANSITIONS) == set(AdmissionStatus)


def test_every_bed_status_is_reachable() -> None:
    """No orphan states. A status nothing leads to is dead code with a name."""
    reachable = {target for targets in BED_TRANSITIONS.values() for target in targets}
    # AVAILABLE is where a bed starts, so nothing needs to lead to it — though
    # something does.
    assert set(BedStatus) - reachable - {BedStatus.AVAILABLE} == set()


def test_no_bed_status_is_a_dead_end() -> None:
    """A bed is never finished with. Every status must have a way out.

    Not true of admissions — a discharged stay is closed forever — but a bed
    that could reach a terminal state would be a bed permanently lost from the
    hospital's capacity by a single click.
    """
    for status, allowed in BED_TRANSITIONS.items():
        assert allowed, f"{status} is a dead end"


def test_admission_terminal_statuses_are_exactly_discharged_and_cancelled() -> None:
    expected = frozenset({AdmissionStatus.DISCHARGED, AdmissionStatus.CANCELLED})
    assert expected == TERMINAL_ADMISSION_STATUSES


# ---------------------------------------------------------------------------
# The edge this module exists to forbid
# ---------------------------------------------------------------------------
def test_an_occupied_bed_cannot_become_available_directly() -> None:
    """The whole reason `CLEANING` exists (CLAUDE.md §7b).

    Skipping it is how a bed board comes to say "free" about a bed that is
    unmade, and the next patient is walked to it at 2am. The error message is
    asserted too: the person reading it is at a nursing station and needs to
    know what to do, not that a state machine refused.
    """
    with pytest.raises(IllegalStateTransitionError) as exc:
        assert_bed_transition(BedStatus.OCCUPIED, BedStatus.AVAILABLE)

    assert "occupied to available" in str(exc.value)
    assert "mark it cleaned" in str(exc.value)


def test_the_release_then_clean_path_is_legal() -> None:
    assert_bed_transition(BedStatus.OCCUPIED, BedStatus.CLEANING)
    assert_bed_transition(BedStatus.CLEANING, BedStatus.AVAILABLE)
    assert_bed_transition(BedStatus.AVAILABLE, BedStatus.OCCUPIED)


def test_a_bed_back_from_maintenance_still_needs_cleaning_offered() -> None:
    """Both routes are legal — but cleaning is available, which is the point."""
    assert BedStatus.CLEANING in BED_TRANSITIONS[BedStatus.OUT_OF_SERVICE]


def test_a_reserved_bed_can_be_filled_or_released() -> None:
    assert_bed_transition(BedStatus.RESERVED, BedStatus.OCCUPIED)
    assert_bed_transition(BedStatus.RESERVED, BedStatus.AVAILABLE)


def test_an_occupied_bed_can_be_condemned_without_passing_through_cleaning() -> None:
    """A broken rail or a contamination incident does not wait for housekeeping."""
    assert_bed_transition(BedStatus.OCCUPIED, BedStatus.OUT_OF_SERVICE)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_setting_a_bed_to_its_current_status_is_refused_with_a_plain_sentence() -> None:
    with pytest.raises(IllegalStateTransitionError) as exc:
        assert_bed_transition(BedStatus.OCCUPIED, BedStatus.OCCUPIED)
    assert "already occupied" in str(exc.value)


def test_a_discharged_admission_cannot_reopen() -> None:
    """A correction is a new admission with a reason, not a silent rewrite."""
    for target in AdmissionStatus:
        if target is AdmissionStatus.DISCHARGED:
            continue
        with pytest.raises(IllegalStateTransitionError):
            assert_admission_transition(AdmissionStatus.DISCHARGED, target)


def test_a_cancelled_admission_cannot_reopen() -> None:
    with pytest.raises(IllegalStateTransitionError) as exc:
        assert_admission_transition(AdmissionStatus.CANCELLED, AdmissionStatus.ADMITTED)
    assert "closed" in str(exc.value)


def test_an_initiated_discharge_can_be_called_off() -> None:
    """A patient who deteriorates between the signature and the front door."""
    assert_admission_transition(AdmissionStatus.DISCHARGE_INITIATED, AdmissionStatus.ADMITTED)


def test_a_death_can_end_a_stay_without_a_discharge_order_first() -> None:
    assert_admission_transition(AdmissionStatus.ADMITTED, AdmissionStatus.DISCHARGED)


def test_refusals_name_what_would_have_been_allowed() -> None:
    """The details carry the legal moves, so a client can offer them."""
    with pytest.raises(IllegalStateTransitionError) as exc:
        assert_bed_transition(BedStatus.CLEANING, BedStatus.OCCUPIED)
    assert exc.value.details["allowed"] == ["AVAILABLE", "OUT_OF_SERVICE"]
