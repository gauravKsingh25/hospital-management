"""Name and phone normalisation.

Pure functions, no database — but the identity guarantee for the whole system
rests on them, because `name + mobile` is what makes a patient unique.
"""

from __future__ import annotations

import pytest

from app.modules.patients.matching import (
    is_valid_indian_mobile,
    normalize_name,
    normalize_phone,
)


class TestNameNormalisation:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("Sunita Devi", "sunita devi"),
            ("  SUNITA   DEVI  ", "sunita devi"),
            ("sunita devi", "sunita devi"),
            ("Sunita  Devi", "sunita devi"),
        ],
    )
    def test_spacing_and_case_do_not_create_new_people(self, written: str, expected: str) -> None:
        assert normalize_name(written) == expected

    @pytest.mark.parametrize(
        "written",
        ["Dr. Sunita Devi", "Smt Sunita Devi", "Mrs. Sunita Devi", "Shri Sunita Devi"],
    )
    def test_honorifics_are_stripped(self, written: str) -> None:
        """`Dr. Sunita Devi` and `Sunita Devi` are one person, one history."""
        assert normalize_name(written) == "sunita devi"

    def test_punctuation_is_ignored(self) -> None:
        assert normalize_name("D'Souza, Maria") == normalize_name("DSouza Maria")

    def test_different_people_stay_different(self) -> None:
        assert normalize_name("Sunita Devi") != normalize_name("Sunil Devi")

    def test_a_name_that_is_only_an_honorific_is_kept(self) -> None:
        """Blanking it would lose the only identifier we were given."""
        assert normalize_name("Baby") == "baby"

    def test_devanagari_is_handled(self) -> None:
        assert normalize_name("सुनीता देवी") == normalize_name("सुनीता  देवी")


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        "written",
        [
            "9876543210",
            "+91 9876543210",
            "+91-98765-43210",
            "09876543210",
            "919876543210",
            "98765 43210",
            "+91 (98765) 43210",
        ],
    )
    def test_every_way_a_receptionist_might_type_it(self, written: str) -> None:
        assert normalize_phone(written) == "9876543210"

    def test_different_numbers_stay_different(self) -> None:
        assert normalize_phone("9876543210") != normalize_phone("9876543211")

    def test_a_foreign_number_is_kept_rather_than_rejected(self) -> None:
        """Four fields and no gatekeeping: an unusual number must not block the queue."""
        assert normalize_phone("+1 415 555 0123") == "14155550123"

    @pytest.mark.parametrize("number", ["9876543210", "6123456789", "7000000000"])
    def test_valid_indian_mobiles(self, number: str) -> None:
        assert is_valid_indian_mobile(number) is True

    @pytest.mark.parametrize("number", ["1234567890", "98765", "5876543210"])
    def test_invalid_indian_mobiles(self, number: str) -> None:
        assert is_valid_indian_mobile(number) is False
