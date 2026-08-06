"""Real-ArcadeDB tests for the refresh-token store.

``tests/test_refresh_tokens.py`` proves the logic against a fake ``run_sql``
that dispatches on parameter names — it would accept SQL ArcadeDB rejects. This
file runs the same operations against a real database, so the statements
themselves are covered: the UNIQUE index on token_hash, ``UPDATE ... WHERE``
matching by family and user, the numeric comparisons on the epoch columns, and
that a row written by one call is readable by the next.

That gap is not hypothetical here — mocked suites in this codebase have passed
repeatedly while the real Cypher/SQL was broken.
"""
import time

import pytest

import app.auth.refresh as rt
from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration


def _row(token_hash: str) -> dict | None:
    rows = run_sql(
        "SELECT user_id, family_id, expires_at, revoked_at, replaced_by "
        "FROM RefreshToken WHERE token_hash = :h", {"h": token_hash})
    return rows[0] if rows else None


# ── The statements round-trip ─────────────────────────────────────────────────

def test_issue_writes_a_readable_row(it_db):
    raw = rt.issue("user-1")
    row = _row(rt.hash_token(raw))
    assert row is not None
    assert row["user_id"] == "user-1"
    assert float(row["revoked_at"]) == 0.0


def test_the_schema_bootstrap_creates_the_type(it_db):
    """RefreshToken must be in db/schema.py — otherwise the first login on a
    fresh deployment writes into a type that does not exist."""
    assert run_sql("SELECT count(*) AS n FROM RefreshToken")[0]["n"] == 0


def test_rotate_round_trips(it_db):
    raw = rt.issue("user-1")
    user_id, new_raw = rt.rotate(raw)
    assert user_id == "user-1"
    assert float(_row(rt.hash_token(raw))["revoked_at"]) > 0
    assert _row(rt.hash_token(raw))["replaced_by"] == rt.hash_token(new_raw)
    assert float(_row(rt.hash_token(new_raw))["revoked_at"]) == 0.0


def test_successor_inherits_family_and_expiry(it_db):
    raw = rt.issue("user-1")
    before = _row(rt.hash_token(raw))
    _, new_raw = rt.rotate(raw)
    after = _row(rt.hash_token(new_raw))
    assert after["family_id"] == before["family_id"]
    assert float(after["expires_at"]) == pytest.approx(float(before["expires_at"]), abs=0.001)


def test_long_rotation_chain(it_db):
    raw = rt.issue("user-1")
    for _ in range(10):
        _, raw = rt.rotate(raw)
    assert rt.rotate(raw)[0] == "user-1"


# ── Rejection paths ───────────────────────────────────────────────────────────

def test_unknown_token_is_rejected(it_db):
    with pytest.raises(rt.RefreshError):
        rt.rotate("never-issued")


def test_expired_token_is_rejected(it_db):
    """The epoch comparison has to work in the database, not just in Python."""
    raw = rt.issue("user-1")
    run_sql("UPDATE RefreshToken SET expires_at = :e WHERE token_hash = :h",
            {"e": time.time() - 60, "h": rt.hash_token(raw)})
    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)


def test_replay_revokes_the_family_in_the_database(it_db):
    raw = rt.issue("user-1")
    _, live = rt.rotate(raw)

    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)

    assert float(_row(rt.hash_token(live))["revoked_at"]) > 0
    with pytest.raises(rt.RefreshError):
        rt.rotate(live)


def test_replay_leaves_a_second_session_alone(it_db):
    """The family predicate must actually discriminate — a WHERE that matched
    everything would pass the test above while logging the user out entirely."""
    other = rt.issue("user-1")
    raw = rt.issue("user-1")
    rt.rotate(raw)
    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)
    assert rt.rotate(other)[0] == "user-1"


# ── Revocation and erasure ────────────────────────────────────────────────────

def test_revoke_single_token(it_db):
    raw = rt.issue("user-1")
    rt.revoke(raw)
    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)


def test_revoke_all_for_user_spares_other_users(it_db):
    mine = rt.issue("user-1")
    theirs = rt.issue("user-2")
    rt.revoke_all_for_user("user-1")
    with pytest.raises(rt.RefreshError):
        rt.rotate(mine)
    assert rt.rotate(theirs)[0] == "user-2"


def test_delete_all_removes_the_rows(it_db):
    raw = rt.issue("user-1")
    rt.issue("user-2")
    rt.delete_all_for_user("user-1")
    assert _row(rt.hash_token(raw)) is None
    assert run_sql("SELECT count(*) AS n FROM RefreshToken")[0]["n"] == 1


# ── Purging ───────────────────────────────────────────────────────────────────

def test_issue_purges_expired_rows_for_that_user(it_db):
    stale = rt.issue("user-1")
    run_sql("UPDATE RefreshToken SET expires_at = :e WHERE token_hash = :h",
            {"e": time.time() - 60, "h": rt.hash_token(stale)})
    rt.issue("user-1")
    assert _row(rt.hash_token(stale)) is None


def test_purge_keeps_revoked_rows(it_db):
    """They are the replay tripwire; the DELETE predicate must not sweep them."""
    raw = rt.issue("user-1")
    rt.rotate(raw)
    rt.issue("user-1")
    assert _row(rt.hash_token(raw)) is not None


def test_purge_spares_other_users_expired_rows(it_db):
    stale = rt.issue("user-2")
    run_sql("UPDATE RefreshToken SET expires_at = :e WHERE token_hash = :h",
            {"e": time.time() - 60, "h": rt.hash_token(stale)})
    rt.issue("user-1")
    assert _row(rt.hash_token(stale)) is not None
