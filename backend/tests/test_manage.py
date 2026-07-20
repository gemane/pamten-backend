"""
Tests for the manage.py wipe-data command.

Covers both the batched-delete mechanics (clears the stale index entries
DELETE FROM leaves behind, which otherwise 500 the SEC scraper on the next
import) and the three safety guards that keep it from ever running against a
production database by accident.
"""
import types

import pytest


def _args(**kw):
    kw.setdefault("confirm_database", "test")  # matches ARCADEDB_DATABASE in conftest
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


def test_wipe_data_drops_types_then_recreates_schema(monkeypatch):
    _arm(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda q, *a, **k: calls.append(q))
    recreated: list[bool] = []
    monkeypatch.setattr("app.db.schema.ensure_indexes",
                        lambda: recreated.append(True) or {"ok": [], "failed": []})

    import manage
    manage.cmd_wipe_data(_args(yes=True))

    # each data/overlay type is drained in batches (short requests that stay
    # under the DB proxy timeout) then the emptied type is dropped
    for t in ("OWNS", "HAS_ROLE", "Entity", "Person", "Location", "Source",
              "Flag", "Suppression", "Pin", "ScrapeRun", "MergeLog"):
        assert f"DELETE FROM {t} LIMIT 10000" in calls
        assert f"DROP TYPE {t} IF EXISTS UNSAFE" in calls
    # ... but user accounts and config are left alone
    for t in ("User", "ScraperSource", "Peer"):
        assert f"DELETE FROM {t} LIMIT 10000" not in calls
        assert f"DROP TYPE {t} IF EXISTS UNSAFE" not in calls
    # ... and the empty types + indexes are recreated afterward
    assert recreated == [True]


def test_wipe_data_refuses_without_the_dedicated_flag(monkeypatch):
    # Guard 1: DEBUG must NOT be enough — only ALLOW_DESTRUCTIVE_WIPE arms it.
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_WIPE", raising=False)
    monkeypatch.setenv("DEBUG", "true")
    ran: list[str] = []
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda q, *a, **k: ran.append(q))

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_data(_args(yes=True))
    assert ran == []  # bailed before touching the DB


def test_wipe_data_refuses_without_confirm_database(monkeypatch):
    # Guard 2: must name the target DB explicitly.
    _arm(monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda q, *a, **k: ran.append(q))

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_data(_args(yes=True, confirm_database=None))
    assert ran == []


def test_wipe_data_refuses_on_database_name_mismatch(monkeypatch):
    # Guard 2: the named DB must match the connected one — this is what stops a
    # wipe aimed at the wrong (e.g. production) database.
    _arm(monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda q, *a, **k: ran.append(q))

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_data(_args(yes=True, confirm_database="pamten"))  # != "test"
    assert ran == []
