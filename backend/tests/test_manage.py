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
        manage.cmd_wipe_source(_args(yes=True, confirm_database="pamten"))  # != "test"
    assert calls == []
