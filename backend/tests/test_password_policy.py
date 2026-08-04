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
