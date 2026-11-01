"""Does this drug clash with that recorded allergy? (SAFE-01)

The matcher is deliberately biased towards warning. A false alarm costs the
doctor one typed reason; a miss can cost the patient an anaphylactic reaction.
These cases pin both directions: what must be caught, and the everyday noise
that must not be, because a warning that fires on everything is a warning
that gets overridden without being read.
"""

from __future__ import annotations

import pytest

from app.modules.patients.allergy import allergy_matches


@pytest.mark.parametrize(
    ("label", "drug"),
    [
        # The drug itself, however it is written.
        ("Aspirin — anaphylaxis", "Aspirin 300 mg"),
        ("aspirin", "ASPIRIN"),
        ("Penicillin", "Penicillin G"),
        ("Codeine", "Paracetamol + Codeine"),
        # A common Indian brand of the same drug.
        ("Aspirin", "Ecosprin 75"),
        ("Paracetamol", "Dolo 650"),
        # Its class.
        ("Penicillin", "Amoxicillin 500 mg"),
        ("Penicillins", "Piperacillin-Tazobactam 4.5 g"),
        ("Sulfa drugs", "Sulfamethoxazole + Trimethoprim"),
        ("Sulpha", "Co-trimoxazole DS"),
        ("NSAIDs", "Ibuprofen 400 mg"),
        ("NSAID allergy", "Diclofenac 75 mg IM"),
        ("NSAIDs", "Aspirin 75 mg"),
        ("Cephalosporins", "Ceftriaxone 1 g"),
        ("Fluoroquinolones", "Levofloxacin 500 mg"),
        # A stem that starts the drug's name.
        ("Sulfa", "Sulfasalazine"),
        # Separators other than a dash.
        ("Diclofenac: rash", "Diclofenac gel"),
        ("Morphine (itching)", "Morphine 2 mg IV"),
    ],
)
def test_a_clash_is_caught(label: str, drug: str) -> None:
    assert allergy_matches(label, [drug])


@pytest.mark.parametrize(
    ("label", "drug"),
    [
        ("Aspirin — anaphylaxis", "Paracetamol 650 mg"),
        ("Penicillin", "Ceftriaxone 1 g"),
        ("Penicillin", "Azithromycin 500 mg"),
        # The reaction is not the allergen: "rash" must not match anything.
        ("Latex — rash", "Rashmi's custom syrup"),
        ("Sulfa drugs", "Magnesium sulphate"),
        # Filler words in the label are not allergens.
        ("Allergy to iodine", "Pantoprazole 40 mg"),
        ("Drug allergy — unknown", "Ondansetron 4 mg"),
        # A dosage form on the label is not an allergen either.
        ("Aspirin tablets", "Tablet Paracetamol 650"),
        ("Penicillin injection", "Inj Pantoprazole 40 mg"),
    ],
)
def test_ordinary_drugs_are_not_flagged(label: str, drug: str) -> None:
    assert not allergy_matches(label, [drug])


def test_any_of_the_names_given_can_match() -> None:
    """The typed drug name, the medicine-list name and its code are all read:
    a doctor who types "Tab X" against the item "Aspirin 75 mg" is still caught."""
    assert allergy_matches("Aspirin", ["Tab X", "Aspirin 75 mg", "ASP75"])


def test_an_empty_label_matches_nothing() -> None:
    assert not allergy_matches("  —  ", ["Aspirin"])
