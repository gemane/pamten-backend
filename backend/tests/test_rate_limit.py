"""Unit tests for the DB-backed rate limiter and its progressive lockout."""
import time
import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Helpers — build a fake run_sql that exposes the store for assertions
# ---------------------------------------------------------------------------

def _make_store_and_sql():
    """Return (store_dict, fake_run_sql) for patching app.auth.rate_limit.run_sql."""
    store: dict[str, dict] = {}

    def fake_run_sql(sql: str, params: dict | None = None, **_kw):
        params = params or {}
        k = params.get("k", "")
        u = sql.strip().upper()
        if u.startswith("SELECT"):
            return [dict(store[k])] if k in store else []
        if "UPSERT" in u or u.startswith("UPDATE"):
            store[k] = {
                "timestamps":    list(params.get("ts", [])),
                "lockout_until": float(params.get("lu", 0.0)),
                "lockout_count": int(params.get("lc", 0)),
            }
            return []
        if u.startswith("DELETE"):
            store.pop(k, None)
            return []
        return []

    return store, fake_run_sql


# ---------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------

def test_check_passes_below_limit(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    store["k"] = {"timestamps": [now - i for i in range(4)],
                  "lockout_until": 0.0, "lockout_count": 0}
    rl.check_rate_limit("k", limit=5, window=900)  # 4 attempts, limit 5 → OK


def test_check_raises_at_limit(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    store["k"] = {"timestamps": [now - i for i in range(5)],
                  "lockout_until": 0.0, "lockout_count": 0}
    with pytest.raises(HTTPException) as ei:
        rl.check_rate_limit("k", limit=5, window=900)
    assert ei.value.status_code == 429


def test_stale_timestamps_are_pruned(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    # 5 timestamps, all older than the window — should be ignored
    store["k"] = {"timestamps": [now - 1000 - i for i in range(5)],
                  "lockout_until": 0.0, "lockout_count": 0}
    rl.check_rate_limit("k", limit=5, window=900)  # all stale → OK


# ---------------------------------------------------------------------------
# Progressive lockout escalation
# ---------------------------------------------------------------------------

def test_first_exhaustion_sets_lockout_until(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    store["k"] = {"timestamps": [now - i for i in range(5)],
                  "lockout_until": 0.0, "lockout_count": 0}

    with pytest.raises(HTTPException):
        rl.check_rate_limit("k", limit=5, window=900)

    saved = store["k"]
    assert saved["lockout_count"] == 1
    # 1st lockout duration is 15 min (900 s)
    assert now + 900 - 2 <= saved["lockout_until"] <= now + 900 + 2


def test_second_exhaustion_escalates_to_one_hour(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    store["k"] = {"timestamps": [now - i for i in range(5)],
                  "lockout_until": 0.0, "lockout_count": 1}  # already hit once

    with pytest.raises(HTTPException):
        rl.check_rate_limit("k", limit=5, window=900)

    saved = store["k"]
    assert saved["lockout_count"] == 2
    # 2nd lockout duration is 1 hour (3600 s)
    assert now + 3600 - 2 <= saved["lockout_until"] <= now + 3600 + 2


def test_fourth_plus_exhaustion_caps_at_24_hours(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    store["k"] = {"timestamps": [now - i for i in range(5)],
                  "lockout_until": 0.0, "lockout_count": 10}  # many prior violations

    with pytest.raises(HTTPException):
        rl.check_rate_limit("k", limit=5, window=900)

    saved = store["k"]
    # Duration must be capped at 24 hours (86400 s) regardless of lockout_count
    assert now + 86400 - 2 <= saved["lockout_until"] <= now + 86400 + 2


def test_active_lockout_blocks_without_touching_window(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    # Lockout still active, window is empty (shouldn't matter)
    store["k"] = {"timestamps": [],
                  "lockout_until": now + 3600, "lockout_count": 2}

    with pytest.raises(HTTPException) as ei:
        rl.check_rate_limit("k", limit=5, window=900)
    assert ei.value.status_code == 429

    # lockout_count must NOT increase — we're inside an existing lockout
    assert store["k"]["lockout_count"] == 2


def test_expired_lockout_allows_fresh_window(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    # lockout_until in the past — expired; window also clear
    store["k"] = {"timestamps": [],
                  "lockout_until": now - 1, "lockout_count": 2}

    rl.check_rate_limit("k", limit=5, window=900)  # should not raise


# ---------------------------------------------------------------------------
# clear_attempts resets the lockout counter
# ---------------------------------------------------------------------------

def test_clear_attempts_deletes_record(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    store["k"] = {"timestamps": [now - i for i in range(5)],
                  "lockout_until": now + 3600, "lockout_count": 3}

    rl.clear_attempts("k")
    assert "k" not in store  # whole record gone → fresh start on next login


# ---------------------------------------------------------------------------
# record_attempt preserves lockout metadata
# ---------------------------------------------------------------------------

def test_record_attempt_preserves_lockout_fields(monkeypatch):
    import app.auth.rate_limit as rl
    store, sql = _make_store_and_sql()
    monkeypatch.setattr(rl, "run_sql", sql)

    now = time.time()
    store["k"] = {"timestamps": [now - 10],
                  "lockout_until": now + 500, "lockout_count": 2}

    rl.record_attempt("k", window=900)

    saved = store["k"]
    # A new timestamp was appended
    assert len(saved["timestamps"]) == 2
    # Lockout fields are unchanged
    assert saved["lockout_count"] == 2
    assert abs(saved["lockout_until"] - (now + 500)) < 2
