"""TOTP (RFC 6238), the code an authenticator app shows (SEC-05).

Checked against the RFC's own published vectors, so the codes this produces
are the codes Google Authenticator, Microsoft Authenticator and every other
app produce for the same secret.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from app.core import totp

# RFC 6238 Appendix B: the SHA-1 seed is the ASCII string "12345678901234567890".
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def test_the_rfc_vectors() -> None:
    # The RFC prints 8-digit codes; an authenticator shows the last 6.
    for moment, eight_digits in (
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
    ):
        assert totp.code_at(RFC_SECRET, _at(moment)) == eight_digits[-6:]


def test_a_code_from_the_previous_window_is_accepted() -> None:
    """A phone's clock drifts, and a person takes a few seconds to type."""
    previous = totp.code_at(RFC_SECRET, _at(1111111109 - 30))
    assert totp.verify(RFC_SECRET, previous, at=_at(1111111109)) is not None


def test_a_code_from_two_windows_ago_is_refused() -> None:
    stale = totp.code_at(RFC_SECRET, _at(1111111109 - 90))
    assert totp.verify(RFC_SECRET, stale, at=_at(1111111109)) is None


def test_verify_says_which_step_matched_so_it_cannot_be_reused() -> None:
    code = totp.code_at(RFC_SECRET, _at(1111111109))
    step = totp.verify(RFC_SECRET, code, at=_at(1111111109))
    assert step == 1111111109 // 30
    # Once used, that step and anything before it is spent.
    assert totp.verify(RFC_SECRET, code, at=_at(1111111109), after_step=step) is None


def test_typing_noise_is_forgiven_but_wrong_digits_are_not() -> None:
    code = totp.code_at(RFC_SECRET, _at(59))
    spaced = f"{code[:3]} {code[3:]}"
    assert totp.verify(RFC_SECRET, spaced, at=_at(59)) is not None
    assert totp.verify(RFC_SECRET, "000000", at=_at(59)) is None
    assert totp.verify(RFC_SECRET, "abc", at=_at(59)) is None


def test_a_new_secret_is_long_enough_and_base32() -> None:
    secret = totp.new_secret()
    assert len(base64.b32decode(secret)) == 20


def test_the_enrolment_link_carries_the_issuer_and_account() -> None:
    uri = totp.provisioning_uri(
        RFC_SECRET, account="dr.rao@demo.hospital", issuer="Sunrise Hospital"
    )
    assert uri.startswith("otpauth://totp/Sunrise%20Hospital:dr.rao%40demo.hospital?")
    assert f"secret={RFC_SECRET}" in uri
    assert "issuer=Sunrise%20Hospital" in uri
