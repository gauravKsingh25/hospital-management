"""Time-based one-time passwords (RFC 6238), for two-step sign-in (SEC-05).

Pure and standard-library only: HMAC-SHA1 over a 30-second counter, six
digits — exactly what Google Authenticator, Microsoft Authenticator and every
other app compute, which is the whole point. Checked against the RFC's own
vectors in `tests/test_totp.py`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

__all__ = ["code_at", "new_secret", "provisioning_uri", "verify"]

STEP_SECONDS = 30
DIGITS = 6
# One window either side: a phone clock thirty seconds out, or a person who
# took a moment to type, still gets in. Wider only widens the guessing target.
DRIFT_STEPS = 1


def new_secret() -> str:
    """A 160-bit secret, base32 as authenticator apps expect it."""
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _step(at: datetime) -> int:
    return int(at.timestamp()) // STEP_SECONDS


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def code_at(secret: str, at: datetime | None = None) -> str:
    """The code an authenticator shows for `secret` at `at` (now by default)."""
    return _code(secret, _step(at or datetime.now(UTC)))


def verify(
    secret: str, code: str, *, at: datetime | None = None, after_step: int | None = None
) -> int | None:
    """The time step `code` matches, or None.

    Returning the step lets the caller spend it: a code, once used, must not
    sign anybody in again — not the same person twice, and not someone who
    read it over their shoulder. `after_step` refuses that step and anything
    earlier.
    """
    typed = "".join(ch for ch in code if not ch.isspace())
    if len(typed) != DIGITS or not typed.isdigit():
        return None
    now = _step(at or datetime.now(UTC))
    for step in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1):
        if after_step is not None and step <= after_step:
            continue
        if hmac.compare_digest(_code(secret, step), typed):
            return step
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The `otpauth://` link an authenticator app reads from a QR code."""
    label = f"{quote(issuer)}:{quote(account)}"
    query = urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS}, quote_via=quote)
    return f"otpauth://totp/{label}?{query}"
