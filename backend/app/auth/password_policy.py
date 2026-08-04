"""Password-strength policy: reject the most common passwords.

Length is enforced elsewhere (``MIN_PASSWORD_LENGTH`` in ``router.py``); this module adds a
blocklist so weak-but-long-enough passwords (``password``, ``12345678``, ``password123``)
are refused too — the NIST SP 800-63B recommendation, in place of character-composition rules.

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
