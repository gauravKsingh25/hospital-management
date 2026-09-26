"""Normalisation and duplicate matching for patient identity.

Everything here exists because the identifying pair is **name + mobile** and
both arrive typed by hand, at speed, at a busy counter. `SUNITA  devi` and
`Sunita Devi` are the same person; `+91 98765 43210`, `098765 43210` and
`9876543210` are the same phone. If normalisation is left to the caller it will
be done three different ways and the uniqueness guarantee quietly evaporates.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["is_valid_indian_mobile", "normalize_name", "normalize_phone"]

# Apostrophes join a word and are deleted, so `D'Souza` and `DSouza` match.
# Everything else that is not a letter or digit separates words and becomes a
# space, so `Devi,Sunita` and `Devi Sunita` do too.
# Spelled as escapes rather than literal glyphs: the whole point is that these
# four look alike, and a reader cannot tell them apart in source either.
#   U+0027 apostrophe · U+2019 right single quote · U+02BC modifier · U+0060 grave
_INTRA_WORD_MARKS = re.compile("[\u0027\u2019\u02bc\u0060]")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_NON_DIGITS = re.compile(r"\D")

# Honorifics and qualifiers reception types inconsistently. Stripping them means
# `Dr. Sunita Devi` and `Sunita Devi` are recognised as the same person rather
# than becoming two records with two histories.
_TITLES = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "master",
        "mstr",
        "dr",
        "prof",
        "shri",
        "shree",
        "smt",
        "sri",
        "kum",
        "baby",
        "bby",
        "md",
    }
)


def normalize_name(name: str) -> str:
    """Casefold, strip punctuation and honorifics, collapse whitespace.

    Unicode is NFKD-normalised first so a Devanagari or accented spelling has
    one canonical form rather than several that compare unequal.
    """
    text = unicodedata.normalize("NFKD", name).casefold()
    text = _INTRA_WORD_MARKS.sub("", text)
    text = _PUNCTUATION.sub(" ", text)
    tokens = [token for token in _WHITESPACE.split(text) if token]
    # Only drop a leading honorific — "Master" can be a surname, and a name
    # that is *only* an honorific is better kept than blanked.
    while len(tokens) > 1 and tokens[0] in _TITLES:
        tokens.pop(0)
    return " ".join(tokens)


def normalize_phone(phone: str) -> str:
    """Reduce an Indian mobile to its bare 10 digits.

    Accepts `+91 98765-43210`, `09876543210`, `919876543210`. Anything that is
    not a recognisable Indian mobile is returned digits-only rather than
    rejected — a foreign patient's number must not block registration
    (CLAUDE.md §7b: four fields, no gatekeeping).
    """
    digits = _NON_DIGITS.sub("", phone)

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    elif len(digits) == 13 and digits.startswith("091"):
        digits = digits[3:]

    return digits


def is_valid_indian_mobile(phone: str) -> bool:
    """Ten digits starting 6-9. Used to warn, never to block."""
    digits = normalize_phone(phone)
    return len(digits) == 10 and digits[0] in "6789"
