"""Password hashing and JWT handling. No database required."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.exceptions import AuthenticationError, ValidationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    fingerprint,
    generate_opaque_token,
    hash_password,
    validate_password_strength,
    verify_password,
)


class TestPasswords:
    def test_hash_is_not_the_password(self) -> None:
        hashed = hash_password("correct-horse-battery-staple")
        assert "correct-horse" not in hashed
        assert hashed.startswith("$argon2id$")

    def test_same_password_hashes_differently_each_time(self) -> None:
        """A per-hash salt is what stops one rainbow table covering every account."""
        password = "correct-horse-battery-staple"
        assert hash_password(password) != hash_password(password)

    def test_verification_round_trips(self) -> None:
        hashed = hash_password("correct-horse-battery-staple")
        assert verify_password("correct-horse-battery-staple", hashed) is True
        assert verify_password("wrong-horse-battery-staple", hashed) is False

    def test_verification_of_garbage_returns_false_rather_than_raising(self) -> None:
        assert verify_password("anything", "not-a-hash") is False

    def test_short_passwords_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_password_strength("short")

    def test_common_passwords_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_password_strength("password1234")

    def test_a_long_passphrase_is_accepted_without_composition_rules(self) -> None:
        validate_password_strength("many small words strung together")


class TestOpaqueTokens:
    def test_tokens_are_unique_and_long(self) -> None:
        tokens = {generate_opaque_token() for _ in range(500)}
        assert len(tokens) == 500
        assert all(len(token) >= 60 for token in tokens)

    def test_fingerprint_is_stable_and_one_way(self) -> None:
        token = generate_opaque_token()
        assert fingerprint(token) == fingerprint(token)
        assert token not in fingerprint(token)


class TestAccessTokens:
    def test_round_trip_preserves_subject_and_tenant(self) -> None:
        user_id, hospital_id = uuid.uuid4(), uuid.uuid4()
        token, _ = create_access_token(user_id, hospital_id=hospital_id)

        claims = decode_token(token, expected_type="access")
        assert claims.subject == user_id
        assert claims.hospital_id == hospital_id
        assert claims.token_type == "access"

    def test_platform_admin_token_carries_no_tenant(self) -> None:
        token, _ = create_access_token(uuid.uuid4(), hospital_id=None)
        assert decode_token(token, expected_type="access").hospital_id is None

    def test_a_refresh_token_is_not_accepted_as_an_access_token(self) -> None:
        """Without the `typ` check a stolen refresh token would be a week-long session."""
        token, _ = create_refresh_token(uuid.uuid4(), hospital_id=None)
        with pytest.raises(AuthenticationError):
            decode_token(token, expected_type="access")

    def test_tampered_signature_is_rejected(self) -> None:
        token, _ = create_access_token(uuid.uuid4(), hospital_id=None)
        forged = jwt.encode(
            jwt.decode(token, options={"verify_signature": False}),
            "not-the-real-signing-key",
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError):
            decode_token(forged, expected_type="access")

    def test_expired_token_is_rejected(self) -> None:
        expired = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "access",
                "jti": str(uuid.uuid4()),
                "iss": settings.JWT_ISSUER,
                "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
                "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
                "hid": None,
            },
            settings.SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError) as excinfo:
            decode_token(expired, expected_type="access")
        assert excinfo.value.code == "token_expired"

    def test_token_from_another_issuer_is_rejected(self) -> None:
        foreign = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "access",
                "jti": str(uuid.uuid4()),
                "iss": "some-other-system",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            },
            settings.SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError):
            decode_token(foreign, expected_type="access")

    def test_unsigned_alg_none_token_is_rejected(self) -> None:
        """The classic JWT bypass: swap the algorithm to `none` and drop the signature."""
        unsigned = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "access",
                "jti": str(uuid.uuid4()),
                "iss": settings.JWT_ISSUER,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(AuthenticationError):
            decode_token(unsigned, expected_type="access")
