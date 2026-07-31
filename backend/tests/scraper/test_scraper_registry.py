"""
Registry-driven dispatch: run_scrape_all iterates the scraper registry, so adding a
scraper is registering a ScraperSpec — no orchestrator edits. These tests swap the
registry for fakes to prove the dispatch contract (enabled → run, disabled skip,
one scraper's failure is isolated) without touching the network or DB.
"""
import app.scraper.runner  # noqa: F401 - importing runner registers the built-in scrapers
from app.scraper.scraper_registry import ScraperSpec, get, register, registered


def test_builtin_scrapers_are_registered():
    names = [s.name for s in registered()]
    assert names == ["wikidata", "sec_edgar", "open_corporates"]  # registration order
    assert get("sec_edgar") is not None and get("nope") is None


def test_register_replaces_by_name(monkeypatch):
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
    register(ScraperSpec("x", lambda q, d: {"v": 1}, lambda: True))
    register(ScraperSpec("x", lambda q, d: {"v": 2}, lambda: True))
    assert len(registered()) == 1 and get("x").run("", 0) == {"v": 2}


def test_run_scrape_all_dispatches_registry(monkeypatch):
    from app.config import settings
    from app.scraper.runner import run_scrape_all
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)  # skip the DB dedup pass
    # swap in a clean registry of only our fakes (runner already registered the built-ins)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    register(ScraperSpec("good", lambda q, d: {"status": "ok", "q": q, "d": d, "total": 3}, lambda: True))
    register(ScraperSpec("off", lambda q, d: {"status": "ok"}, lambda: False))

    def boom(q, d):
        raise RuntimeError("kaboom")
    register(ScraperSpec("bad", boom, lambda: True))

    def denied(q, d):
        raise PermissionError("source off")
    register(ScraperSpec("denied", denied, lambda: True))

    out = run_scrape_all("Acme", depth=2)

    assert out["status"] == "ok" and out["query"] == "Acme"
    assert out["results"]["good"] == {"status": "ok", "q": "Acme", "d": 2, "total": 3}  # gets (query, depth)
    assert out["results"]["off"] == {"status": "disabled"}                              # enabled() False → not run
    assert out["results"]["bad"]["status"] == "error" and "kaboom" in out["results"]["bad"]["detail"]
    assert out["results"]["denied"] == {"status": "disabled", "detail": "source off"}   # PermissionError → disabled
