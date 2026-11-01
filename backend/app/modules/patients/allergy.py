"""Does a drug clash with a recorded drug allergy? (SAFE-01)

Pure functions, no database — like `matching.py` beside it — so the rule can
be tested exhaustively and read in one place.

**Biased towards warning.** A false alarm costs the prescriber one typed
reason; a miss can cost the patient an anaphylactic reaction. So:

* the allergen is whatever the label names before its reaction ("Aspirin —
  anaphylaxis" names aspirin, not anaphylaxis), split on commas and slashes
  into one or more allergens;
* an allergen matches a drug word it equals or begins (`sulfa` begins
  `sulfasalazine`), once it is four letters or more;
* a class name brings its members (`penicillin` brings amoxicillin and
  piperacillin; `nsaid` brings ibuprofen and aspirin), and a generic brings
  the handful of Indian brands written on charts every day (`aspirin` brings
  Ecosprin).

The class and brand table is a safety net, not a formulary. It covers the
classes behind most recorded drug allergies; anything else is matched by name
only, which is why the medicine list's own name and code are checked as well
as whatever the doctor typed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["allergens", "allergy_matches"]

# Where the allergen ends and the reaction begins: "Aspirin — anaphylaxis",
# "Diclofenac: rash", "Morphine (itching)". A hyphen counts only with spaces
# round it, so "Co-trimoxazole" stays one drug.
# The dashes are written as escapes: \u2013 is an en dash, \u2014 an em dash.
_REACTION_START = re.compile("\\s[-\u2013\u2014]\\s|[\u2013\u2014:(]")
# Several allergens on one label: "Penicillin, sulfa", "Aspirin/NSAIDs".
_ALLERGEN_LIST = re.compile(r"[,/;&+]|\band\b")
_WORD = re.compile(r"[a-z]+")

# Words that appear on allergy labels but name no drug.
_NOT_AN_ALLERGEN = frozenset(
    {
        "allergy",
        "allergic",
        "allergies",
        "drug",
        "drugs",
        "medicine",
        "medicines",
        "group",
        "class",
        "family",
        "all",
        "any",
        "the",
        "to",
        "of",
        "unknown",
        "intolerance",
        "sensitivity",
        "history",
        "severe",
        "mild",
        "reaction",
        "rash",
        "itching",
        "hives",
        "urticaria",
        "anaphylaxis",
        "swelling",
        "angioedema",
        "derivatives",
        "containing",
        # Dosage forms: "Aspirin tablets" is an aspirin allergy, and must not
        # flag every tablet on the chart.
        "tab",
        "tabs",
        "tablet",
        "cap",
        "capsule",
        "syrup",
        "syp",
        "inj",
        "injection",
        "drop",
        "cream",
        "ointment",
        "gel",
        "oral",
        "iv",
    }
)

_PENICILLINS = (
    "penicillin",
    "benzylpenicillin",
    "phenoxymethylpenicillin",
    "benzathine",
    "amoxicillin",
    "amoxycillin",
    "amoxiclav",
    "augmentin",
    "ampicillin",
    "cloxacillin",
    "flucloxacillin",
    "dicloxacillin",
    "piperacillin",
    "ticarcillin",
)
_CEPHALOSPORINS = (
    "cefalexin",
    "cephalexin",
    "cefadroxil",
    "cefazolin",
    "cefuroxime",
    "cefixime",
    "cefpodoxime",
    "cefdinir",
    "cefotaxime",
    "ceftriaxone",
    "ceftazidime",
    "cefoperazone",
    "cefepime",
)
_SULFONAMIDES = (
    "sulfamethoxazole",
    "sulphamethoxazole",
    "cotrimoxazole",
    "sulfasalazine",
    "sulphasalazine",
    "sulfadiazine",
    "septran",
    "bactrim",
)
_NSAIDS = (
    "aspirin",
    "ibuprofen",
    "diclofenac",
    "aceclofenac",
    "naproxen",
    "ketorolac",
    "mefenamic",
    "indomethacin",
    "piroxicam",
    "ketoprofen",
    "nimesulide",
    "etoricoxib",
    "celecoxib",
)
_QUINOLONES = (
    "ciprofloxacin",
    "levofloxacin",
    "ofloxacin",
    "norfloxacin",
    "moxifloxacin",
    "gatifloxacin",
)
_MACROLIDES = ("azithromycin", "clarithromycin", "erythromycin", "roxithromycin")
_OPIOIDS = ("morphine", "codeine", "tramadol", "fentanyl", "pethidine", "tapentadol", "oxycodone")

# Allergen word -> the drug words it also covers.
_COVERS: dict[str, tuple[str, ...]] = {
    "penicillin": _PENICILLINS,
    "betalactam": _PENICILLINS + _CEPHALOSPORINS,
    "cephalosporin": _CEPHALOSPORINS,
    "sulfa": _SULFONAMIDES,
    "sulpha": _SULFONAMIDES,
    "sulfonamide": _SULFONAMIDES,
    "sulphonamide": _SULFONAMIDES,
    "nsaid": _NSAIDS,
    "quinolone": _QUINOLONES,
    "fluoroquinolone": _QUINOLONES,
    "macrolide": _MACROLIDES,
    "opioid": _OPIOIDS,
    "opiate": _OPIOIDS,
    # Generics -> brands written on Indian charts and prescriptions.
    "aspirin": ("ecosprin", "disprin", "acetylsalicylic"),
    "paracetamol": ("acetaminophen", "dolo", "crocin", "calpol"),
    "acetaminophen": ("paracetamol", "dolo", "crocin", "calpol"),
    "ibuprofen": ("brufen", "combiflam"),
    "diclofenac": ("voveran",),
    "cotrimoxazole": ("septran", "bactrim"),
    "amoxicillin": ("amoxiclav", "augmentin"),
}

# Below this length a word is matched exactly, never as the start of another:
# "dolo" must not catch "dolonex"-style coincidences any more than it has to.
_MIN_STEM = 4


def _singular(word: str) -> str:
    """`penicillins` -> `penicillin`, `nsaids` -> `nsaid`."""
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def _words(text: str) -> list[str]:
    # Hyphens and spaces inside a drug's own name join it up, so
    # "Co-trimoxazole" is `cotrimoxazole` — while each part is kept too, so
    # "Piperacillin-Tazobactam" still offers `piperacillin`.
    lowered = text.casefold()
    joined = re.sub(r"(?<=[a-z])[-\s](?=[a-z])", "", lowered)
    return _WORD.findall(lowered) + _WORD.findall(joined)


def allergens(label: str) -> set[str]:
    """The drug words an allergy label covers, class members and brands included."""
    named = _REACTION_START.split(label, maxsplit=1)[0]
    found: set[str] = set()
    for part in _ALLERGEN_LIST.split(named.casefold()):
        for raw in _WORD.findall(part):
            word = _singular(raw)
            if len(word) < 3 or word in _NOT_AN_ALLERGEN:
                continue
            found.add(word)
            found.update(_COVERS.get(word, ()))
    # One more round for what a class brought in: `nsaid` brings `aspirin`,
    # and `aspirin` brings Ecosprin.
    for word in list(found):
        found.update(_COVERS.get(word, ()))
    return found


def allergy_matches(label: str, drug_names: Iterable[str]) -> bool:
    """True when any of `drug_names` is, or belongs to, the allergen on `label`."""
    terms = allergens(label)
    if not terms:
        return False
    for name in drug_names:
        for word in _words(name):
            for term in terms:
                if word == term or (len(term) >= _MIN_STEM and word.startswith(term)):
                    return True
    return False
