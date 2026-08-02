"""
On-demand source selection is KIND-driven: `ensure_scrape` runs the enabled `instant`
sources, never `bulk` (GLEIF), skips an admin-disabled instant source, and on a `deepen`
pass runs only depth-aware sources. Registry + resolvers are faked (no DB/network).
"""
import contextlib

import app.scraper.runner  # noqa: F401 - ensures the module is importable / built-ins register
from app.config import settings
from app.scraper import ondemand
from app.scraper.scraper_registry import ScraperSpec, register


@contextlib.contextmanager
def _fake_record_run(source, target):
    yield {}


def _setup(monkeypatch, entity, *, enable_oc=False):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr("app.scraper.run_log.record_run", _fake_record_run)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    def mk(name, kind, depth_aware, enabled=True):
        def run(q, d):
            calls.append((name, d))
            return {"status": "ok", "total": 1, "entity_id": "e1"}
        register(ScraperSpec(name, run, (lambda: enabled), kind=kind, depth_aware=depth_aware))

    mk("wikidata", "instant", True)
    mk("sec_edgar", "instant", False)
    mk("open_corporates", "instant", False, enabled=enable_oc)   # admin-toggleable instant
    mk("gleif", "bulk", False)                                   # bulk — must never run

    # Resolve/profile without touching the DB.
    import app.routers.search as search
    monkeypatch.setattr(search, "resolve_best_entity", lambda q, country=None: entity)
    monkeypatch.setattr(search, "get_full_profile",
                        lambda eid: {"entity": {"id": eid, "scrape_depth": 1}})
    return calls


def test_absent_runs_all_enabled_instant_sources_never_bulk(monkeypatch):
    calls = _setup(monkeypatch, entity=None)   # absent
    out = ondemand.ensure_scrape("Acme", depth=1, force=False)
    assert out["scraped"] and out["reason"] == "absent"
    names = [c[0] for c in calls]
    assert names == ["wikidata", "sec_edgar"]          # instant + enabled; oc off, gleif bulk
    assert "gleif" not in names and "open_corporates" not in names
    assert out["sources_run"] == ["wikidata", "sec_edgar"]
    # depth-aware source gets the requested depth; depth-blind gets 0
    assert dict(calls) == {"wikidata": 1, "sec_edgar": 0}


def test_enabled_open_corporates_participates(monkeypatch):
    calls = _setup(monkeypatch, entity=None, enable_oc=True)
    ondemand.ensure_scrape("Acme", depth=1, force=False)
    assert [c[0] for c in calls] == ["wikidata", "sec_edgar", "open_corporates"]


def test_deepen_runs_only_depth_aware_sources(monkeypatch):
    # Fresh + shallow → requesting a deeper pass is a "deepen": only Wikidata re-runs.
    entity = {"id": "e1", "on_demand_scraped": True, "scrape_depth": 1,
              "last_scraped_at": "2026-08-02T00:00:00+00:00"}
    monkeypatch.setattr(ondemand, "decide_scrape",
                        lambda *a, **k: ondemand.ScrapeDecision(True, "deepen", 2))
    calls = _setup(monkeypatch, entity=entity)
    out = ondemand.ensure_scrape("Acme", depth=2, force=False)
    assert out["reason"] == "deepen"
    assert [c[0] for c in calls] == ["wikidata"]        # sec_edgar (depth-blind) skipped
    assert calls == [("wikidata", 2)]


def test_master_switch_off_serves_db_without_scraping(monkeypatch):
    calls = _setup(monkeypatch, entity=None)
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", False)
    out = ondemand.ensure_scrape("Acme", depth=1, force=False)
    assert not out["scraped"] and out["reason"] == "disabled" and calls == []


def test_fresh_entity_is_served_from_db(monkeypatch):
    from datetime import datetime, timezone
    entity = {"id": "e1", "on_demand_scraped": True, "scrape_depth": 2,
              "last_scraped_at": datetime.now(timezone.utc).isoformat()}
    calls = _setup(monkeypatch, entity=entity)
    out = ondemand.ensure_scrape("Acme", depth=1, force=False)
    assert not out["scraped"] and out["reason"] == "fresh" and calls == []
    assert out["entity_id"] == "e1" and out["profile"]["entity"]["id"] == "e1"
