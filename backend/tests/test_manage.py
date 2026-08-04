"""
Tests for the manage.py wipe-source command (there is no whole-DB wipe — a fresh
start is a database drop). Covers the three safety guards that keep a delete from
ever running against the wrong database, and that it delegates to
maintenance.wipe_source once the guards pass.
"""
import types

import pytest


def _args(**kw):
    kw.setdefault("confirm_database", "test")  # matches ARCADEDB_DATABASE in conftest
    kw.setdefault("source", "UK PSC")
    return types.SimpleNamespace(**kw)


def _arm(monkeypatch):
    """Enable the dedicated wipe guard (Guard 1)."""
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_WIPE", "true")


def test_backfill_search_updates_entity_and_person(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda q, *a, **k: calls.append(q))

    import manage
    manage.cmd_backfill_search(_args(batch=20000))

    assert any("UPDATE Entity SET search_text" in c and "WHERE search_text IS NULL LIMIT 20000" in c
               for c in calls)
    assert any("UPDATE Person SET search_text" in c and "WHERE search_text IS NULL LIMIT 20000" in c
               for c in calls)
    # name is null-guarded so a null name can't leave search_text NULL and loop forever
    assert any("ifnull(name, '')" in c for c in calls)


def _stub_wipe(monkeypatch):
    """Record calls to maintenance.wipe_source without touching the DB."""
    calls: list[dict] = []
    def fake(source, batch=10000, id_prefixes=None, **kw):
        calls.append({"source": source, "batch": batch, "id_prefixes": id_prefixes})
        return {"edges": {"OWNS": 3}, "nodes": {"Entity": 2, "Person": 5}, "reindexed": 9}
    monkeypatch.setattr("app.scraper.maintenance.wipe_source", fake)
    return calls


def test_wipe_source_delegates_after_guards(monkeypatch):
    _arm(monkeypatch)
    calls = _stub_wipe(monkeypatch)

    import manage
    manage.cmd_wipe_source(_args(yes=True, source="UK PSC", batch=500, id_prefix="chpsc:,gb-coh:"))

    assert calls == [{"source": "UK PSC", "batch": 500, "id_prefixes": ["chpsc:", "gb-coh:"]}]


def test_wipe_source_refuses_without_the_dedicated_flag(monkeypatch):
    # Guard 1: DEBUG must NOT be enough — only ALLOW_DESTRUCTIVE_WIPE arms it.
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_WIPE", raising=False)
    monkeypatch.setenv("DEBUG", "true")
    calls = _stub_wipe(monkeypatch)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_source(_args(yes=True))
    assert calls == []  # bailed before touching the DB


def test_wipe_source_refuses_without_confirm_database(monkeypatch):
    # Guard 2: must name the target DB explicitly.
    _arm(monkeypatch)
    calls = _stub_wipe(monkeypatch)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_source(_args(yes=True, confirm_database=None))
    assert calls == []


def test_wipe_source_refuses_on_database_name_mismatch(monkeypatch):
    # Guard 2: the named DB must match the connected one — this stops a delete
    # aimed at the wrong (e.g. production) database.
    _arm(monkeypatch)
    calls = _stub_wipe(monkeypatch)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_source(_args(yes=True, confirm_database="owlgraph"))  # != "test"
    assert calls == []


# ── set-password ──────────────────────────────────────────────────────────────
#
# The operator escape hatch: ADMIN_PASSWORD only seeds a *missing* account, the
# email reset flow needs SMTP (blocked on Render), and /auth/change-password needs
# the current password — so without this there is no way to rotate a known-but-
# unwanted password.

def _stub_sql(monkeypatch, select_rows):
    """Record run_sql calls; the first (the SELECT) returns select_rows."""
    calls: list[tuple] = []

    def _run_sql(query, params=None, *a, **k):
        calls.append((query, params or {}))
        return select_rows if query.strip().upper().startswith("SELECT") else []

    monkeypatch.setattr("app.db.arcadedb.run_sql", _run_sql)
    return calls


def test_set_password_hashes_and_updates(monkeypatch):
    from app.auth.security import verify_password
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])

    import manage
    manage.cmd_set_password(_args(email="boss@example.com", password="Zt9mQ2vLp4rK"))

    update = [(q, p) for q, p in calls if q.strip().upper().startswith("UPDATE")]
    assert len(update) == 1
    query, params = update[0]
    assert "SET password_hash" in query
    assert params["e"] == "boss@example.com"
    # Stores a bcrypt hash of the password, never the password itself.
    assert params["h"] != "Zt9mQ2vLp4rK"
    assert verify_password("Zt9mQ2vLp4rK", params["h"])


def test_set_password_normalises_the_email(monkeypatch):
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])

    import manage
    manage.cmd_set_password(_args(email="  BOSS@Example.COM  ", password="Zt9mQ2vLp4rK"))

    assert all(p.get("e") == "boss@example.com" for _, p in calls)


def test_set_password_exits_when_the_user_does_not_exist(monkeypatch):
    calls = _stub_sql(monkeypatch, [])  # SELECT finds nothing

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_set_password(_args(email="ghost@example.com", password="Zt9mQ2vLp4rK"))

    assert all(not q.strip().upper().startswith("UPDATE") for q, _ in calls)


def test_set_password_enforces_the_same_policy_as_the_api(monkeypatch):
    # Each of these is rejected by /auth/register and /auth/reset-password too —
    # both sides call password_policy_error.
    for bad in ("short", "a" * 73, "password123"):
        calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])
        import manage
        with pytest.raises(SystemExit):
            manage.cmd_set_password(_args(email="boss@example.com", password=bad))
        assert all(not q.strip().upper().startswith("UPDATE") for q, _ in calls), bad


def test_set_password_prompts_twice_and_rejects_a_mismatch(monkeypatch):
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])
    prompts = iter(["Zt9mQ2vLp4rK", "typo-on-the-repeat"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(prompts))

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_set_password(_args(email="boss@example.com", password=None))

    assert all(not q.strip().upper().startswith("UPDATE") for q, _ in calls)


def test_set_password_reads_from_a_hidden_prompt_when_not_given(monkeypatch):
    # The password must never have to appear in argv (shell history, ps output).
    from app.auth.security import verify_password
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "Zt9mQ2vLp4rK")

    import manage
    manage.cmd_set_password(_args(email="boss@example.com", password=None))

    update = [(q, p) for q, p in calls if q.strip().upper().startswith("UPDATE")]
    assert len(update) == 1
    assert verify_password("Zt9mQ2vLp4rK", update[0][1]["h"])
