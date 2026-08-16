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
    register(ScraperSpec("x", lambda q, d, c=None: {"v": 1}, lambda: True))
    register(ScraperSpec("x", lambda q, d, c=None: {"v": 2}, lambda: True))
    assert len(registered()) == 1 and get("x").run("", 0) == {"v": 2}


def test_run_scrape_all_dispatches_registry(monkeypatch):
    from app.config import settings
    from app.scraper.runner import run_scrape_all
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)  # skip the DB dedup pass
    # swap in a clean registry of only our fakes (runner already registered the built-ins)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    register(ScraperSpec("good", lambda q, d, c=None: {"status": "ok", "q": q, "d": d, "c": c, "total": 3}, lambda: True))
    register(ScraperSpec("off", lambda q, d, c=None: {"status": "ok"}, lambda: False))

    def boom(q, d, c=None):
        raise RuntimeError("kaboom")
    register(ScraperSpec("bad", boom, lambda: True))

    def denied(q, d, c=None):
        raise PermissionError("source off")
    register(ScraperSpec("denied", denied, lambda: True))

    out = run_scrape_all("Acme", depth=2)

    assert out["status"] == "ok" and out["query"] == "Acme"
    # gets (query, depth, country) — no country asked for here
    assert out["results"]["good"] == {"status": "ok", "q": "Acme", "d": 2, "c": None, "total": 3}
    assert out["results"]["off"] == {"status": "disabled"}                              # enabled() False → not run
    assert out["results"]["bad"]["status"] == "error" and "kaboom" in out["results"]["bad"]["detail"]
    assert out["results"]["denied"] == {"status": "disabled", "detail": "source off"}   # PermissionError → disabled


def test_run_scrape_all_hands_the_country_to_each_source(monkeypatch):
    """Dropped anywhere between the caller and `spec.run`, the scrape still
    succeeds — with whichever company the source liked best, which is the bug
    the country exists to prevent."""
    from app.config import settings
    from app.scraper.runner import run_scrape_all
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
    register(ScraperSpec("good", lambda q, d, c=None: {"status": "ok", "c": c, "total": 1},
                         lambda: True))

    out = run_scrape_all("Alphabet", depth=1, country="DE")
    assert out["results"]["good"]["c"] == "DE"


def test_a_scraper_that_cannot_take_a_country_is_rejected_at_registration(monkeypatch):
    """Not a style rule — a correctness one.

    Every dispatcher wraps `spec.run` in `except Exception`, so one source
    failing cannot sink the rest. A run() with the old two-argument signature
    therefore raises a TypeError that is swallowed and logged, and the scrape
    reports success having run nothing. Four test files in this repo did exactly
    that when the signature changed. Registration is the last place it is loud.
    """
    import pytest

    # A private registry: the built-ins are asserted elsewhere, and fakes that
    # escape into the real one break those tests instead of this one.
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    with pytest.raises(TypeError, match="must accept"):
        register(ScraperSpec("legacy", lambda q, d: {"total": 0}, lambda: True))

    # And the correct shape still registers, whether the country is positional
    # or defaulted.
    register(ScraperSpec("modern", lambda q, d, c: {"total": 0}, lambda: True))
    register(ScraperSpec("modern2", lambda q, d, c=None: {"total": 0}, lambda: True))
    assert get("modern") and get("modern2")
