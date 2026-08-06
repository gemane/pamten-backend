"""Server-stored refresh tokens: long sessions without long-lived bearer tokens.

Access tokens are self-contained JWTs — nothing is consulted when one is
presented, so a stolen token is valid until it expires and there is no way to
call it back. That is why they now live 15 minutes instead of 12 hours. A
session outlives them through a refresh token, which is the opposite shape:

  - **opaque**, not a JWT — it carries no claims, it is just a random secret
    whose meaning lives in this table;
  - **stored hashed** (SHA-256), so a database leak does not hand over live
    sessions — the same reason password hashes exist. It needs no salt: the
    token is 32 random bytes, so there is nothing to brute-force or rainbow;
  - **revocable**, because every check hits the row;
  - **rotated on every use** — refreshing consumes the presented token and
    issues a successor.

Rotation is what makes theft detectable. A stolen token is only useful until
the real client refreshes (or vice versa); whoever refreshes second presents a
token that has already been consumed, which cannot happen in normal operation.
That is treated as a compromised session: the whole **family** — the chain of
successors descending from one login — is revoked, logging out both the thief
and the victim. The victim logs in again; the thief cannot.

Lifetime is **absolute**: a successor inherits its predecessor's expiry rather
than starting a fresh clock, so a session cannot be extended indefinitely by
staying active. It ends REFRESH_TOKEN_EXPIRE_DAYS after login.

Fail-closed
-----------
Unlike ``rate_limit`` (which fails *open* so a DB outage cannot lock everyone
out), every failure here denies the refresh. The cost is asymmetric: failing
open on a rate limiter allows extra guesses, while failing open on session
validation would accept revoked or forged tokens. A denied refresh degrades to
"log in again", which is safe.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid

from app.config import settings
from app.db.arcadedb import run_sql

log = logging.getLogger(__name__)

# 32 bytes of entropy, urlsafe-base64 encoded (43 chars). Well beyond guessing.
_TOKEN_BYTES = 32


class RefreshError(Exception):
    """The presented refresh token is unusable — unknown, expired, or revoked.

    Deliberately carries no detail about which: the caller turns every case into
    the same 401, so a probing client cannot tell a revoked token from a
    never-existed one.
    """


def hash_token(raw: str) -> str:
    """Digest stored in place of the token itself. See the module docstring for
    why an unsalted SHA-256 is the right choice for a high-entropy secret."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def _lifetime_seconds() -> int:
    return settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


def _insert(user_id: str, family_id: str, expires_at: float) -> str:
    """Create one refresh-token row and return the raw (unhashed) token.

    The raw value is returned to the caller and never stored — this is the only
    moment it exists outside the client's cookie.
    """
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    run_sql(
        "INSERT INTO RefreshToken SET id = :id, token_hash = :h, user_id = :u, "
        "family_id = :f, issued_at = :now, expires_at = :exp, "
        "revoked_at = 0.0, replaced_by = ''",
        {"id": str(uuid.uuid4()), "h": hash_token(raw), "u": user_id,
         "f": family_id, "now": _now(), "exp": expires_at},
    )
    return raw


def issue(user_id: str) -> str:
    """Start a new session (called on login). Returns the raw refresh token."""
    _purge_expired(user_id)
    return _insert(user_id, family_id=str(uuid.uuid4()),
                   expires_at=_now() + _lifetime_seconds())


def rotate(raw: str) -> tuple[str, str]:
    """Consume *raw* and issue its successor.

    Returns ``(user_id, new_raw_token)``. Raises :class:`RefreshError` if the
    token is unknown, expired, or already consumed — the last of which also
    revokes the whole family, since a replayed token means someone has a copy
    they should not have.
    """
    token_hash = hash_token(raw)
    try:
        rows = run_sql(
            "SELECT user_id, family_id, expires_at, revoked_at FROM RefreshToken "
            "WHERE token_hash = :h",
            {"h": token_hash},
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        log.warning("refresh lookup failed: %s", exc)
        raise RefreshError("Could not validate session") from exc

    if not rows:
        raise RefreshError("Unknown refresh token")

    row = rows[0]
    user_id = row.get("user_id") or ""
    family_id = row.get("family_id") or ""

    # Replay of an already-consumed token: the family is compromised. Revoking it
    # logs out the thief *and* the legitimate client, which is the intended
    # trade-off — we cannot tell which of the two is presenting this.
    if float(row.get("revoked_at") or 0):
        log.warning("refresh token replayed (user=%s, family=%s) — revoking family",
                    user_id, family_id)
        revoke_family(family_id)
        raise RefreshError("Refresh token already used")

    expires_at = float(row.get("expires_at") or 0)
    if expires_at <= _now():
        raise RefreshError("Refresh token expired")

    # Successor inherits the family's absolute expiry — rotation renews the
    # secret, not the session's lifetime.
    new_raw = _insert(user_id, family_id, expires_at)
    run_sql(
        "UPDATE RefreshToken SET revoked_at = :now, replaced_by = :new "
        "WHERE token_hash = :h",
        {"now": _now(), "new": hash_token(new_raw), "h": token_hash},
    )
    return user_id, new_raw


def revoke(raw: str) -> None:
    """Revoke a single token (logout). Silent if it is unknown — logging out
    with a stale cookie should still look like a successful logout."""
    try:
        run_sql(
            "UPDATE RefreshToken SET revoked_at = :now WHERE token_hash = :h "
            "AND revoked_at = 0.0",
            {"now": _now(), "h": hash_token(raw)},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh revoke failed: %s", exc)


def revoke_family(family_id: str) -> None:
    """Revoke every token descending from one login."""
    if not family_id:
        return
    try:
        run_sql(
            "UPDATE RefreshToken SET revoked_at = :now WHERE family_id = :f "
            "AND revoked_at = 0.0",
            {"now": _now(), "f": family_id},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh family revoke failed (family=%s): %s", family_id, exc)


def revoke_all_for_user(user_id: str) -> None:
    """End every session for an account.

    Called when the password changes, a reset completes, or the account is
    deleted — the events after which any session established with the old
    credentials should no longer be trusted. Note this ends the *current*
    session too; the caller is responsible for issuing a fresh one if the user
    should stay logged in.
    """
    if not user_id:
        return
    try:
        run_sql(
            "UPDATE RefreshToken SET revoked_at = :now WHERE user_id = :u "
            "AND revoked_at = 0.0",
            {"now": _now(), "u": user_id},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh revoke-all failed (user=%s): %s", user_id, exc)


def delete_all_for_user(user_id: str) -> None:
    """Erase an account's token rows outright (account deletion).

    Revoking would be enough to end the sessions, but the rows are account data
    and the deletion is an erasure — leaving them behind would keep a record of
    when someone logged in after their account was supposed to be gone.
    """
    if not user_id:
        return
    try:
        run_sql("DELETE FROM RefreshToken WHERE user_id = :u", {"u": user_id})
    except Exception as exc:  # noqa: BLE001 — never block the deletion
        log.warning("refresh delete-all failed (user=%s): %s", user_id, exc)


def _purge_expired(user_id: str) -> None:
    """Delete this user's expired rows on login.

    Opportunistic rather than a scheduled job: login is infrequent and already
    doing DB work, and scoping to one user keeps it cheap however large the
    table grows.

    Only *expired* rows go. Revoked-but-unexpired rows are kept on purpose —
    they are the tripwire in :func:`rotate`, and deleting them would turn a
    detectable replay into a token that merely looks unknown, so the family
    would never be revoked.
    """
    try:
        run_sql(
            "DELETE FROM RefreshToken WHERE user_id = :u AND expires_at <= :now",
            {"u": user_id, "now": _now()},
        )
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        log.warning("refresh purge failed (user=%s): %s", user_id, exc)
