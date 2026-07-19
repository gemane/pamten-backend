"""
Tests for the manage.py wipe-data command — in particular that it clears the
stale index entries DELETE FROM leaves behind (which otherwise 500 the SEC
scraper on the next import with RecordNotFoundException).
"""
import types

import pytest


def _args(**kw):
    return types.SimpleNamespace(**kw)


def test_wipe_data_deletes_types_then_rebuilds_indexes(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")
    calls: list[str] = []
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda q, *a, **k: calls.append(q))
    monkeypatch.setattr("app.db.schema.ensure_indexes", lambda: {"ok": [], "failed": []})

    import manage
    manage.cmd_wipe_data(_args(yes=True))

    # every data type is wiped — core data, edges, and the derived overlays/logs
    for t in ("OWNS", "HAS_ROLE", "Entity", "Person", "Location", "Source",
              "Flag", "Suppression", "Pin", "ScrapeRun", "MergeLog"):
        assert f"DELETE FROM {t}" in calls
    # ... but user accounts and config are left alone
    for t in ("User", "ScraperSource", "Peer"):
        assert f"DELETE FROM {t}" not in calls
    # ... and stale index entries are cleared (the fix)
    assert "REBUILD INDEX *" in calls
    assert calls.index("REBUILD INDEX *") > calls.index("DELETE FROM Entity")


def test_wipe_data_refuses_without_debug(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda *a, **k: None)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_data(_args(yes=True))
