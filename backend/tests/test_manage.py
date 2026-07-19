"""
Tests for the manage.py wipe-data command — in particular that it clears the
stale index entries DELETE FROM leaves behind (which otherwise 500 the SEC
scraper on the next import with RecordNotFoundException).
"""
import types

import pytest


def _args(**kw):
    return types.SimpleNamespace(**kw)


def test_wipe_data_drops_types_then_recreates_schema(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")
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


def test_wipe_data_refuses_without_debug(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda *a, **k: None)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_data(_args(yes=True))
