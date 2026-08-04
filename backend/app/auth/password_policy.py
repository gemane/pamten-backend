"""Password-strength policy for user-chosen passwords.

``password_policy_error`` is the single definition of the rules, shared by every caller
that accepts a password — ``/auth/register``, ``/auth/reset-password``,
``/auth/change-password`` and ``manage.py set-password``. Callers turn the returned
message into whatever their surface needs (an HTTP 400 detail, a line on stderr), so the
rules can never drift apart between the API and the CLI.

Alongside length limits it applies a blocklist, so weak-but-long-enough passwords
(``password``, ``12345678``, ``password123``) are refused too — the NIST SP 800-63B
recommendation, in place of character-composition rules.

The blocklist is the xato-net "10 million passwords" top-10,000 list, bundled from SecLists
(https://github.com/danielmiessler/SecLists, MIT licence) as ``common_passwords.txt`` — one
entry per line, already lowercased. Loaded once into a frozenset at import.
"""
from pathlib import Path

_LIST_FILE = Path(__file__).with_name("common_passwords.txt")


def _load() -> frozenset[str]:
    try:
        return frozenset(
            line.strip() for line in _LIST_FILE.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    except OSError:  # pragma: no cover - the file ships with the package
        return frozenset()


_COMMON: frozenset[str] = _load()


def is_common_password(password: str) -> bool:
    """True if ``password`` is on the common-password blocklist (case-insensitive)."""
    return password.strip().lower() in _COMMON


MIN_PASSWORD_LENGTH = 8
# bcrypt silently truncates input at 72 UTF-8 bytes, making the tail of a longer
# password meaningless for authentication. We reject at the boundary rather than
# truncate silently, so users are never misled about what their password is.
MAX_PASSWORD_BYTES = 72


def password_policy_error(password: str) -> str | None:
    """Return a user-facing message if ``password`` violates the policy, else ``None``.

    Checks run in order — length first, so a too-short password gets the actionable
    message rather than a blocklist hit it would also have triggered.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"Password must be at most {MAX_PASSWORD_BYTES} characters"
    if is_common_password(password):
        return "This password is too common — please choose a less common one."
    return None
