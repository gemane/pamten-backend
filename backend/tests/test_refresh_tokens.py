"""Unit tests for the server-stored refresh tokens (app/auth/refresh.py).

These run the real module against a fake ``run_sql`` that keeps rows in a dict,
so rotation, replay detection and revocation are exercised for real while the
SQL itself is stubbed. The SQL is covered separately by
``tests/integration/test_refresh_tokens_it.py`` against a real ArcadeDB —
neither layer is sufficient alone, and this codebase has been burned before by
mocks that happily accepted Cypher the database rejects.
"""
import time

import pytest

import app.auth.refresh as rt


@pytest.fixture
def store(refresh_rows):
    """The in-memory row table from conftest's autouse ``refresh_rows`` fixture,
    which patches ``run_sql`` so the real store logic runs against it."""
    return refresh_rows


# ── Issue ─────────────────────────────────────────────────────────────────────

def test_issue_stores_only_the_hash(store):
    """A database leak must not hand over live sessions."""
    raw = rt.issue("user-1")
    assert len(store) == 1
    row = next(iter(store.values()))
    assert row["token_hash"] == rt.hash_token(raw)
    assert raw not in str(row)          # the raw secret appears nowhere


def test_issue_returns_unique_tokens(store):
    assert rt.issue("user-1") != rt.issue("user-1")


def test_issue_sets_absolute_expiry(store, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "REFRESH_TOKEN_EXPIRE_DAYS", 30)
    before = time.time()
    rt.issue("user-1")
    row = next(iter(store.values()))
    assert row["expires_at"] == pytest.approx(before + 30 * 86400, abs=5)


# ── Rotation ──────────────────────────────────────────────────────────────────

def test_rotate_returns_user_and_new_token(store):
    raw = rt.issue("user-1")
    user_id, new_raw = rt.rotate(raw)
    assert user_id == "user-1"
    assert new_raw != raw


def test_rotate_consumes_the_presented_token(store):
    raw = rt.issue("user-1")
    rt.rotate(raw)
    assert store[rt.hash_token(raw)]["revoked_at"] > 0


def test_rotate_links_predecessor_to_successor(store):
    raw = rt.issue("user-1")
    _, new_raw = rt.rotate(raw)
    assert store[rt.hash_token(raw)]["replaced_by"] == rt.hash_token(new_raw)


def test_successor_stays_in_the_same_family(store):
    raw = rt.issue("user-1")
    _, new_raw = rt.rotate(raw)
    assert (store[rt.hash_token(new_raw)]["family_id"]
            == store[rt.hash_token(raw)]["family_id"])


def test_rotation_does_not_extend_the_session(store):
    """Absolute lifetime: staying active must not grant an endless session."""
    raw = rt.issue("user-1")
    original_expiry = store[rt.hash_token(raw)]["expires_at"]
    _, new_raw = rt.rotate(raw)
    assert store[rt.hash_token(new_raw)]["expires_at"] == original_expiry


def test_rotate_chains(store):
    """Several refreshes in a row keep working."""
    raw = rt.issue("user-1")
    for _ in range(5):
        _, raw = rt.rotate(raw)
    assert rt.rotate(raw)[0] == "user-1"


def test_rotate_rejects_unknown_token(store):
    with pytest.raises(rt.RefreshError):
        rt.rotate("never-issued")


def test_rotate_rejects_expired_token(store):
    raw = rt.issue("user-1")
    store[rt.hash_token(raw)]["expires_at"] = time.time() - 1
    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)


def test_rotate_fails_closed_when_the_store_is_unreachable(monkeypatch):
    """A DB outage must deny the refresh, never wave it through."""
    def boom(*_a, **_kw):
        raise RuntimeError("arcadedb unreachable")
    monkeypatch.setattr(rt, "run_sql", boom)
    with pytest.raises(rt.RefreshError):
        rt.rotate("anything")


# ── Replay detection ──────────────────────────────────────────────────────────

def test_replaying_a_consumed_token_is_rejected(store):
    raw = rt.issue("user-1")
    rt.rotate(raw)
    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)


def test_replay_revokes_the_whole_family(store):
    """The stolen token and the victim's live one both die — we cannot tell
    which party is presenting the replay, so the session is burned."""
    raw = rt.issue("user-1")
    _, live = rt.rotate(raw)
    assert store[rt.hash_token(live)]["revoked_at"] == 0.0   # still good

    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)                                       # thief replays

    assert store[rt.hash_token(live)]["revoked_at"] > 0      # victim logged out
    with pytest.raises(rt.RefreshError):
        rt.rotate(live)


def test_replay_does_not_touch_other_sessions(store):
    """Burning one compromised family must not sign the user out everywhere —
    a second browser is a separate family."""
    other = rt.issue("user-1")
    raw = rt.issue("user-1")
    rt.rotate(raw)
    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)
    assert rt.rotate(other)[0] == "user-1"


# ── Revocation ────────────────────────────────────────────────────────────────

def test_revoke_ends_the_session(store):
    raw = rt.issue("user-1")
    rt.revoke(raw)
    with pytest.raises(rt.RefreshError):
        rt.rotate(raw)


def test_revoke_is_silent_for_an_unknown_token(store):
    rt.revoke("not-a-real-token")        # logout with a stale cookie still works


def test_revoke_all_ends_every_session_for_the_user(store):
    a, b = rt.issue("user-1"), rt.issue("user-1")
    other_user = rt.issue("user-2")
    rt.revoke_all_for_user("user-1")
    for tok in (a, b):
        with pytest.raises(rt.RefreshError):
            rt.rotate(tok)
    assert rt.rotate(other_user)[0] == "user-2"      # untouched


def test_delete_all_erases_rows_rather_than_revoking(store):
    """Account deletion is an erasure — no leftover login history."""
    rt.issue("user-1")
    rt.issue("user-2")
    rt.delete_all_for_user("user-1")
    assert [r["user_id"] for r in store.values()] == ["user-2"]


def test_revoke_all_tolerates_a_dead_store(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("down")
    monkeypatch.setattr(rt, "run_sql", boom)
    rt.revoke_all_for_user("user-1")     # must not raise into the caller


# ── Purging ───────────────────────────────────────────────────────────────────

def test_issue_purges_the_users_expired_rows(store):
    stale = rt.issue("user-1")
    store[rt.hash_token(stale)]["expires_at"] = time.time() - 1
    rt.issue("user-1")
    assert rt.hash_token(stale) not in store


def test_purge_keeps_revoked_rows_so_replays_stay_detectable(store):
    """Revoked-but-unexpired rows are the replay tripwire; purging them would
    downgrade a detectable theft to a token that merely looks unknown."""
    raw = rt.issue("user-1")
    rt.rotate(raw)                       # raw is now revoked, not expired
    rt.issue("user-1")                   # triggers the purge
    assert rt.hash_token(raw) in store


def test_purge_leaves_other_users_alone(store):
    stale = rt.issue("user-2")
    store[rt.hash_token(stale)]["expires_at"] = time.time() - 1
    rt.issue("user-1")
    assert rt.hash_token(stale) in store
