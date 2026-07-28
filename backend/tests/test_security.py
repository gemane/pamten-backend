"""
Unit tests for password hashing/verification.

We moved off passlib (unmaintained; breaks with modern bcrypt) to using the
bcrypt library directly. These lock in the behaviour that matters:
  - round-trip hash/verify
  - wrong password is rejected
  - hashes in the previous passlib $2b$ format still verify (no forced logout /
    password reset for existing users)
  - >72-byte passwords don't raise (bcrypt >= 4.1 rejects them otherwise)
  - a malformed stored hash fails closed instead of raising
"""
from app.auth.security import hash_password, verify_password


def test_hash_verify_round_trip():
    h = hash_password("s3cret-password")
    assert h != "s3cret-password"          # not stored in plaintext
    assert h.startswith("$2b$")            # bcrypt format
    assert verify_password("s3cret-password", h) is True


def test_wrong_password_rejected():
    h = hash_password("s3cret-password")
    assert verify_password("wrong", h) is False


def test_verifies_legacy_passlib_format_hash():
    # A $2b$ hash exactly as the old passlib+bcrypt stack stored — must still
    # verify so existing accounts keep working after the switch.
    legacy = "$2b$12$UYRK2Lu5ZuzE6.Sk/mY9dueI6YYF.WMH3wUGVAWuQGLrz0kv5n0AC"
    assert verify_password("correct horse", legacy) is True
    assert verify_password("wrong horse", legacy) is False


def test_long_password_does_not_raise():
    # bcrypt only uses the first 72 bytes and raises above that unless truncated.
    long_pw = "a" * 200
    h = hash_password(long_pw)
    assert verify_password(long_pw, h) is True
    # First 72 bytes identical → same effective password (matches bcrypt semantics)
    assert verify_password("a" * 72, h) is True


def test_malformed_hash_fails_closed():
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False


# ── Purpose tokens (email verify / password reset links) ──────────────────────

from datetime import timedelta  # noqa: E402
import pytest  # noqa: E402
from app.auth.security import (  # noqa: E402
    create_purpose_token, verify_purpose_token, password_hash_fingerprint, TokenError,
)


def test_purpose_token_round_trip():
    tok = create_purpose_token("user-1", "verify_email", timedelta(hours=1))
    claims = verify_purpose_token(tok, "verify_email")
    assert claims["sub"] == "user-1" and claims["purpose"] == "verify_email"


def test_purpose_token_wrong_purpose_rejected():
    tok = create_purpose_token("user-1", "verify_email", timedelta(hours=1))
    with pytest.raises(TokenError):
        verify_purpose_token(tok, "pwd_reset")


def test_purpose_token_expired_rejected():
    tok = create_purpose_token("user-1", "pwd_reset", timedelta(seconds=-1))
    with pytest.raises(TokenError):
        verify_purpose_token(tok, "pwd_reset")


def test_purpose_token_tampered_signature_rejected():
    tok = create_purpose_token("user-1", "verify_email", timedelta(hours=1))
    with pytest.raises(TokenError):
        verify_purpose_token(tok + "x", "verify_email")


def test_password_hash_fingerprint_changes_with_the_hash():
    a = password_hash_fingerprint(hash_password("one"))
    b = password_hash_fingerprint(hash_password("two"))
    assert a != b and len(a) == 16          # 16-hex-char digest, hash-specific


# ── TOTP two-factor + recovery codes ──────────────────────────────────────────

import base64  # noqa: E402
import time as _time  # noqa: E402
from cryptography.hazmat.primitives.hashes import SHA1  # noqa: E402
from cryptography.hazmat.primitives.twofactor.totp import TOTP  # noqa: E402
from app.auth.security import (  # noqa: E402
    generate_totp_secret, verify_totp, totp_provisioning_uri,
    generate_recovery_codes, hash_recovery_code,
)


def _current_code(secret_b32: str) -> str:
    key = base64.b32decode(secret_b32)
    return TOTP(key, 6, SHA1(), 30).generate(int(_time.time())).decode()


def test_totp_round_trip_and_rejects_wrong_code():
    secret = generate_totp_secret()
    code = _current_code(secret)
    assert verify_totp(secret, code) is True
    wrong = f"{(int(code) + 1) % 1000000:06d}"     # a different value at the same instant
    assert verify_totp(secret, wrong) is False
    assert verify_totp(secret, "not-digits") is False


def test_provisioning_uri_is_scannable():
    secret = generate_totp_secret()
    uri = totp_provisioning_uri(secret, "user@x.com")
    assert uri.startswith("otpauth://totp/Pamten:")
    assert f"secret={secret}" in uri and "issuer=Pamten" in uri
    assert "digits=6" in uri and "period=30" in uri


def test_recovery_codes_are_unique_and_hash_is_format_insensitive():
    codes = generate_recovery_codes(10)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all("-" in c for c in codes)
    # matching ignores case and the dash separator
    assert hash_recovery_code("ABcdE-FGHij") == hash_recovery_code("abcdefghij")
    assert hash_recovery_code("aaaaa-bbbbb") != hash_recovery_code("aaaaa-bbbbc")
