"""Reference-range selection and flagging, tested without a database.

These two functions decide whether a number gets a highlight, a phone call, or
nothing at all. They are pure, so they can be tested exhaustively — which is
what you want for the code that decides a potassium of 7.2 is an emergency.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.core.exceptions import IllegalStateTransitionError
from app.modules.diagnostics.models import ReferenceRange, ReportStatus, ResultFlag, SpecimenStatus
from app.modules.diagnostics.service import flag_for, select_reference_range
from app.modules.diagnostics.transitions import (
    REPORT_TRANSITIONS,
    SPECIMEN_TRANSITIONS,
    TERMINAL_REPORT_STATUSES,
    assert_report_transition,
    assert_specimen_transition,
)


def _band(**kwargs: object) -> ReferenceRange:
    """An unsaved band. These functions are pure, so no database is involved."""
    return ReferenceRange(hospital_id=uuid.uuid4(), analyte_id=uuid.uuid4(), **kwargs)  # type: ignore[arg-type]


D = Decimal


# ---------------------------------------------------------------------------
# Choosing the band
# ---------------------------------------------------------------------------
class TestRangeSelection:
    def test_a_sex_specific_band_beats_the_catch_all(self) -> None:
        """A haemoglobin of 12 is normal in a woman and anaemic in a man.

        Getting this wrong mis-flags roughly half the hospital.
        """
        catch_all = _band(low=D("12.0"), high=D("17.0"))
        female = _band(sex="FEMALE", low=D("12.0"), high=D("15.0"))
        male = _band(sex="MALE", low=D("13.0"), high=D("17.0"))

        chosen = select_reference_range([catch_all, female, male], sex="FEMALE", age_years=34)
        assert chosen is female

    def test_an_age_band_is_used_when_it_fits(self) -> None:
        catch_all = _band(low=D("12.0"), high=D("17.0"))
        child = _band(age_min_years=0, age_max_years=12, low=D("11.0"), high=D("14.0"))

        assert select_reference_range([catch_all, child], sex="MALE", age_years=8) is child
        assert select_reference_range([catch_all, child], sex="MALE", age_years=40) is catch_all

    def test_sex_outranks_age(self) -> None:
        """Both fit; the more specific dimension wins."""
        by_age = _band(age_min_years=18, age_max_years=99, low=D("12.0"), high=D("17.0"))
        by_sex = _band(sex="FEMALE", low=D("12.0"), high=D("15.0"))

        assert select_reference_range([by_age, by_sex], sex="FEMALE", age_years=34) is by_sex

    def test_a_band_for_another_sex_is_never_chosen(self) -> None:
        male_only = _band(sex="MALE", low=D("13.0"), high=D("17.0"))
        assert select_reference_range([male_only], sex="FEMALE", age_years=34) is None

    def test_an_unknown_age_falls_back_rather_than_guessing(self) -> None:
        """Registration takes four fields; a birth date is not one of them.

        A patient whose age we never recorded must still get a range, which is
        exactly why the catch-all band has to exist.
        """
        catch_all = _band(low=D("12.0"), high=D("17.0"))
        child = _band(age_min_years=0, age_max_years=12, low=D("11.0"), high=D("14.0"))

        assert select_reference_range([catch_all, child], sex="MALE", age_years=None) is catch_all

    def test_no_bands_means_no_range(self) -> None:
        assert select_reference_range([], sex="MALE", age_years=30) is None


# ---------------------------------------------------------------------------
# Flagging a value
# ---------------------------------------------------------------------------
class TestFlagging:
    def test_a_value_inside_the_band_is_normal(self) -> None:
        band = _band(low=D("12.0"), high=D("15.0"))
        assert flag_for(D("13.4"), band) == (ResultFlag.NORMAL, False)

    def test_the_boundaries_are_inclusive(self) -> None:
        band = _band(low=D("12.0"), high=D("15.0"))
        assert flag_for(D("12.0"), band)[0] is ResultFlag.NORMAL
        assert flag_for(D("15.0"), band)[0] is ResultFlag.NORMAL

    def test_outside_the_band_is_low_or_high(self) -> None:
        band = _band(low=D("12.0"), high=D("15.0"))
        assert flag_for(D("11.9"), band) == (ResultFlag.LOW, False)
        assert flag_for(D("15.1"), band) == (ResultFlag.HIGH, False)

    def test_a_panic_value_outranks_merely_abnormal(self) -> None:
        """A potassium of 7.2 is both "above normal" and "ring the ward now",
        and only the second one matters."""
        band = _band(low=D("3.5"), high=D("5.1"), critical_low=D("2.5"), critical_high=D("6.5"))

        assert flag_for(D("7.2"), band) == (ResultFlag.CRITICAL_HIGH, True)
        assert flag_for(D("2.1"), band) == (ResultFlag.CRITICAL_LOW, True)
        # Still abnormal, but not an emergency.
        assert flag_for(D("5.8"), band) == (ResultFlag.HIGH, False)

    def test_the_panic_limits_are_inclusive(self) -> None:
        band = _band(low=D("3.5"), high=D("5.1"), critical_low=D("2.5"), critical_high=D("6.5"))
        assert flag_for(D("6.5"), band)[1] is True
        assert flag_for(D("2.5"), band)[1] is True

    def test_a_one_sided_band_only_flags_its_side(self) -> None:
        """Plenty of analytes have an upper limit and no meaningful lower one."""
        band = _band(high=D("40.0"))
        assert flag_for(D("0.1"), band)[0] is ResultFlag.NORMAL
        assert flag_for(D("55.0"), band)[0] is ResultFlag.HIGH

    def test_no_band_means_no_opinion(self) -> None:
        """Silence beats a made-up normal."""
        assert flag_for(D("999"), None) == (ResultFlag.NORMAL, False)


# ---------------------------------------------------------------------------
# The two state machines
# ---------------------------------------------------------------------------
class TestTransitions:
    def test_every_status_appears_in_both_tables(self) -> None:
        assert set(ReportStatus) == set(REPORT_TRANSITIONS)
        assert set(SpecimenStatus) == set(SPECIMEN_TRANSITIONS)

    def test_a_verified_report_never_goes_backwards(self) -> None:
        """The only way to change a signed report is to supersede it."""
        assert REPORT_TRANSITIONS[ReportStatus.FINAL] == frozenset({ReportStatus.AMENDED})

        for target in (ReportStatus.IN_PROGRESS, ReportStatus.PRELIMINARY, ReportStatus.CANCELLED):
            with pytest.raises(IllegalStateTransitionError):
                assert_report_transition(ReportStatus.FINAL, target)

    def test_terminal_report_statuses(self) -> None:
        assert {ReportStatus.AMENDED, ReportStatus.CANCELLED} == TERMINAL_REPORT_STATUSES

    def test_the_normal_lab_path(self) -> None:
        assert_report_transition(ReportStatus.REGISTERED, ReportStatus.IN_PROGRESS)
        assert_report_transition(ReportStatus.IN_PROGRESS, ReportStatus.PRELIMINARY)
        assert_report_transition(ReportStatus.PRELIMINARY, ReportStatus.FINAL)

    def test_a_report_may_go_straight_to_final(self) -> None:
        """Most results are entered and signed in one sitting."""
        assert_report_transition(ReportStatus.IN_PROGRESS, ReportStatus.FINAL)

    def test_the_normal_specimen_path(self) -> None:
        assert_specimen_transition(SpecimenStatus.PENDING_COLLECTION, SpecimenStatus.COLLECTED)
        assert_specimen_transition(SpecimenStatus.COLLECTED, SpecimenStatus.RECEIVED)

    def test_a_sample_can_be_rejected_at_the_bench(self) -> None:
        """Haemolysis is often only found after the sample is booked in."""
        assert_specimen_transition(SpecimenStatus.RECEIVED, SpecimenStatus.REJECTED)

    def test_an_uncollected_sample_cannot_be_received(self) -> None:
        with pytest.raises(IllegalStateTransitionError) as error:
            assert_specimen_transition(SpecimenStatus.PENDING_COLLECTION, SpecimenStatus.RECEIVED)
        assert error.value.details["allowed"] == ["CANCELLED", "COLLECTED"]

    def test_a_rejected_sample_is_final(self) -> None:
        with pytest.raises(IllegalStateTransitionError):
            assert_specimen_transition(SpecimenStatus.REJECTED, SpecimenStatus.RECEIVED)

    def test_the_refusal_names_both_states(self) -> None:
        with pytest.raises(IllegalStateTransitionError) as error:
            assert_report_transition(ReportStatus.REGISTERED, ReportStatus.FINAL)
        assert "registered" in str(error.value)
        assert error.value.details["from"] == "REGISTERED"
