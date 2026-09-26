"""Password hashing, JWT issuance and verification (CLAUDE.md §12).

This module is pure: no database, no request context. It knows how to prove a
password matches and how to sign and read a token — nothing about users, roles
or tenants. That lives in `modules.identity.service`.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.exceptions import AuthenticationError, ValidationError

TokenType = Literal["access", "refresh"]

ACCESS: Final[TokenType] = "access"
REFRESH: Final[TokenType] = "refresh"

_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_KIB,
    parallelism=settings.ARGON2_PARALLELISM,
)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Argon2id hash. The salt is generated per call and embedded in the output."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verification that never raises on a bad password."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates a work-factor increase.

    Callers should transparently re-hash on the next successful login, so
    raising the cost parameters upgrades existing accounts over time.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str) -> None:
    """Length-first policy.

    Deliberately not a composition rule ("one uppercase, one symbol"): NIST
    SP 800-63B dropped those because they push people towards `Password1!`.
    Length is what actually costs an attacker.
    """
    minimum = settings.PASSWORD_MIN_LENGTH
    if len(password) < minimum:
        raise ValidationError(
            f"Password must be at least {minimum} characters long.",
            code="weak_password",
        )
    if password.lower() in _COMMON_PASSWORDS:
        raise ValidationError(
            "That password is too common. Choose something less guessable.",
            code="weak_password",
        )


# A token list only — real deployments should wire in a breach corpus check.
_COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "administrator",
        "hospital1234",
        "qwertyuiop12",
        "123456789012",
        "welcome12345",
    }
)


# ---------------------------------------------------------------------------
# Opaque tokens (refresh tokens, reset links)
# ---------------------------------------------------------------------------
def generate_opaque_token() -> str:
    """A high-entropy value handed to the client and never stored in the clear."""
    return secrets.token_urlsafe(48)


def fingerprint(token: str) -> str:
    """SHA-256 of a token, for storage and lookup.

    Refresh tokens are random 384-bit values, so a fast hash is appropriate
    here — there is nothing to brute-force. Argon2 is for human-chosen secrets.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The verified contents of an access or refresh token."""

    subject: uuid.UUID
    token_type: TokenType
    jti: uuid.UUID
    hospital_id: uuid.UUID | None
    issued_at: datetime
    expires_at: datetime


def _create_token(
    *,
    subject: uuid.UUID,
    token_type: TokenType,
    hospital_id: uuid.UUID | None,
    lifetime: timedelta,
    jti: uuid.UUID | None = None,
) -> tuple[str, TokenClaims]:
    now = datetime.now(UTC)
    expires_at = now + lifetime
    token_id = jti or uuid.uuid4()

    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "jti": str(token_id),
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        # Tenant is carried for observability and early rejection only. It is
        # never trusted for authorisation: the tenant used for queries is
        # re-read from the user row on every request.
        "hid": str(hospital_id) if hospital_id else None,
    }
    encoded = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    claims = TokenClaims(
        subject=subject,
        token_type=token_type,
        jti=token_id,
        hospital_id=hospital_id,
        issued_at=now,
        expires_at=expires_at,
    )
    return encoded, claims


def create_access_token(
    subject: uuid.UUID, *, hospital_id: uuid.UUID | None
) -> tuple[str, TokenClaims]:
    return _create_token(
        subject=subject,
        token_type=ACCESS,
        hospital_id=hospital_id,
        lifetime=timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES),
    )


def create_refresh_token(
    subject: uuid.UUID, *, hospital_id: uuid.UUID | None, jti: uuid.UUID | None = None
) -> tuple[str, TokenClaims]:
    return _create_token(
        subject=subject,
        token_type=REFRESH,
        hospital_id=hospital_id,
        lifetime=timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS),
        jti=jti,
    )


def decode_token(token: str, *, expected_type: TokenType) -> TokenClaims:
    """Verify signature, expiry, issuer and token type.

    Checking `typ` matters: without it a refresh token would be accepted as a
    bearer credential, handing an attacker a week-long session.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Your session has expired.", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid credentials.", code="invalid_token") from exc

    if payload.get("typ") != expected_type:
        raise AuthenticationError("Invalid credentials.", code="invalid_token")

    try:
        subject = uuid.UUID(payload["sub"])
        jti = uuid.UUID(payload["jti"])
        hospital_raw = payload.get("hid")
        hospital_id = uuid.UUID(hospital_raw) if hospital_raw else None
    except (KeyError, ValueError, TypeError) as exc:
        raise AuthenticationError("Invalid credentials.", code="invalid_token") from exc

    return TokenClaims(
        subject=subject,
        token_type=expected_type,
        jti=jti,
        hospital_id=hospital_id,
        issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )
