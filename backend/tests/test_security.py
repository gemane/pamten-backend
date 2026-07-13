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
