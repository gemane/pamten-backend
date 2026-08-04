"""Unit tests for the common-password blocklist."""
from app.auth.password_policy import is_common_password


def test_flags_common_passwords():
    for pw in ("password", "12345678", "password123", "qwerty123", "letmein"):
        assert is_common_password(pw) is True


def test_case_insensitive():
    assert is_common_password("Password") is True
    assert is_common_password("PASSWORD123") is True


def test_ignores_surrounding_whitespace():
    assert is_common_password("  password123  ") is True


def test_allows_a_strong_password():
    assert is_common_password("Zt9mQ2vLp4rK") is False
    assert is_common_password("correct-horse-battery-staple-42") is False


def test_blocklist_is_loaded():
    # The bundled list should be present and non-trivial.
    from app.auth import password_policy
    assert len(password_policy._COMMON) > 5000


# ── password_policy_error: the single rule set shared by the API and the CLI ──

from app.auth.password_policy import (  # noqa: E402
    password_policy_error, MIN_PASSWORD_LENGTH, MAX_PASSWORD_BYTES,
)


def test_accepts_a_strong_password():
    assert password_policy_error("Zt9mQ2vLp4rK") is None


def test_rejects_too_short():
    msg = password_policy_error("a" * (MIN_PASSWORD_LENGTH - 1))
    assert msg and str(MIN_PASSWORD_LENGTH) in msg


def test_accepts_exactly_the_minimum_length():
    assert password_policy_error("Zt9mQ2vL") is None  # 8 chars, not on the blocklist


def test_rejects_over_the_bcrypt_byte_limit():
    msg = password_policy_error("a" * (MAX_PASSWORD_BYTES + 1))
    assert msg and str(MAX_PASSWORD_BYTES) in msg


def test_counts_bytes_not_characters_for_the_upper_bound():
    # bcrypt truncates at 72 BYTES; "é" is 2 bytes in UTF-8, so 40 of them exceed
    # the limit despite being only 40 characters.
    assert password_policy_error("é" * 40) is not None
    assert password_policy_error("é" * 36) is None  # 72 bytes exactly — allowed


def test_rejects_a_common_password():
    msg = password_policy_error("password123")
    assert msg and "common" in msg.lower()


def test_length_is_reported_before_the_blocklist():
    # "letmein" is both too short and on the blocklist; the actionable message wins.
    msg = password_policy_error("letmein")
    assert msg and str(MIN_PASSWORD_LENGTH) in msg
