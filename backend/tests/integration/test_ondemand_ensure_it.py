"""
Real-ArcadeDB test for on-demand `ensure_scrape`: a fake instant source writes an Entity
and marks the scrape target; we drive the freshness state machine end to end — absent →
scrape + stamp; fresh → no-op; force → re-scrape; deepen → only depth-aware runs, depth
bumps. Skipped unless ARCADEDB_IT_URL is set (see conftest.py).
"""
import pytest

pytestmark = pytest.mark.integration


def _instant_source(calls):
    """A fake depth-aware instant source: upserts one Entity, marks it as the scrape
    target (so the autodedup wrapper stamps freshness), and records its calls."""
    from app.db.arcadedb import run_sql
    from app.scraper.graph_writer import _with_autodedup, set_scrape_target
    from app.scraper.mapper import normalize_entity_name

    eid = "ent-acme"

    def _run(q, d, c=None):
        calls.append((q, d))
        run_sql("UPDATE Entity SET name = :n, name_normalized = :nn, search_text = :st, "
                "type = 'company' UPSERT WHERE id = :id",
                {"n": q, "nn": normalize_entity_name(q), "st": q.lower(), "id": eid})
        set_scrape_target(eid, d)
        return {"status": "ok", "total": 1, "entity_id": eid}

    return _with_autodedup(_run), eid


def test_ensure_freshness_state_machine(it_db, monkeypatch):
    from app.config import settings
    from app.db.arcadedb import run_sql
    from app.scraper import ondemand
    from app.scraper.graph_writer import _with_autodedup
    from app.scraper.scraper_registry import ScraperSpec, register

    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)   # skip the DB dedup pass
    monkeypatch.setattr(settings, "SCRAPER_ONDEMAND_COOLDOWN_HOURS", 0)  # exercise force/deepen w/o the cooldown gate
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    calls: list = []
    faux, eid = _instant_source(calls)
    register(ScraperSpec("faux", faux, lambda: True, kind="instant", depth_aware=True))

    blind_calls: list = []
    register(ScraperSpec(
        "blind", _with_autodedup(lambda q, d, c=None: (blind_calls.append((q, d)),
                                                      {"status": "ok", "total": 0})[1]),
        lambda: True, kind="instant", depth_aware=False))

    bulk_calls: list = []
    register(ScraperSpec("bulkz", lambda q, d, c=None: bulk_calls.append((q, d)) or {"total": 0},
                         lambda: True, kind="bulk"))

    def _row():
        return dict(run_sql(
            "SELECT on_demand_scraped, scrape_depth, last_scraped_at FROM Entity WHERE id = :id",
            {"id": eid})[0])

    # 1) absent → scrapes the enabled instant sources, never bulk, stamps freshness
    out = ondemand.ensure_scrape("Acme Corp", depth=1)
    assert out["scraped"] and out["reason"] == "absent" and out["entity_id"] == eid
    assert sorted(out["sources_run"]) == ["blind", "faux"] and bulk_calls == []
    r = _row()
    assert r["on_demand_scraped"] is True and r["scrape_depth"] == 1 and r["last_scraped_at"]

    # 2) immediate re-ensure → fresh, no new source calls
    n_faux, n_blind = len(calls), len(blind_calls)
    out2 = ondemand.ensure_scrape("Acme Corp", depth=1)
    assert not out2["scraped"] and out2["reason"] == "fresh"
    assert len(calls) == n_faux and len(blind_calls) == n_blind

    # 2b) force within the cooldown window → blocked, served from DB, no source calls
    monkeypatch.setattr(settings, "SCRAPER_ONDEMAND_COOLDOWN_HOURS", 24)
    out_cd = ondemand.ensure_scrape("Acme Corp", depth=1, force=True)
    assert not out_cd["scraped"] and out_cd["reason"] == "cooldown"
    assert len(calls) == n_faux                        # nothing re-fetched during the cooldown
    monkeypatch.setattr(settings, "SCRAPER_ONDEMAND_COOLDOWN_HOURS", 0)

    # 3) force past the cooldown → re-scrapes despite being fresh
    out3 = ondemand.ensure_scrape("Acme Corp", depth=1, force=True)
    assert out3["scraped"] and out3["reason"] == "forced"
    assert len(calls) == n_faux + 1

    # 4) deepen to depth 2 → only depth-aware faux runs (blind skipped), depth bumps to 2
    n_blind = len(blind_calls)
    out4 = ondemand.ensure_scrape("Acme Corp", depth=2)
    assert out4["scraped"] and out4["reason"] == "deepen"
    assert len(blind_calls) == n_blind                 # depth-blind source skipped on deepen
    assert _row()["scrape_depth"] == 2 and out4["depth_reached"] == 2
