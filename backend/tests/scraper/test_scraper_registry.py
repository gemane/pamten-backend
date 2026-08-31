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


def test_register_replaces_by_name(monkeypatch, fake_sources):
    fake_sources("x")
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
    register(ScraperSpec("x", lambda q, d, c=None: {"v": 1}, lambda: True))
    register(ScraperSpec("x", lambda q, d, c=None: {"v": 2}, lambda: True))
    assert len(registered()) == 1 and get("x").run("", 0) == {"v": 2}


def test_run_scrape_all_dispatches_registry(monkeypatch, fake_sources):
    fake_sources("good", "off", "bad", "denied")
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


def test_run_scrape_all_hands_the_country_to_each_source(monkeypatch, fake_sources):
    fake_sources("good")
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


def test_a_scraper_that_cannot_take_a_country_is_rejected_at_registration(monkeypatch, fake_sources):
    fake_sources("legacy", "modern", "modern2")
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


class TestTheSpecIsACompleteDeclaration:
    """The enrichment: metadata via the catalogue, enabled() derived, and the
    pieces that used to agree by convention cross-validated at registration."""

    def test_metadata_reads_from_the_catalogue(self):
        from app.scraper.scraper_registry import get
        spec = get("sec_edgar")
        assert spec.label == "SEC EDGAR"
        assert spec.credibility == 98
        assert spec.url == "https://www.sec.gov/edgar"

    def test_default_enabled_is_flag_and_toggle(self, monkeypatch, fake_sources):
        # Pydantic Settings refuses unknown fields, so the probe borrows a real
        # flag via the settings_flag override — which also exercises it.
        from unittest.mock import patch
        from app.config import settings
        fake_sources("probe")
        monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
        monkeypatch.setattr(settings, "SCRAPER_SEC_EDGAR_ENABLED", True)
        register(ScraperSpec("probe", lambda q, d, c=None: {},
                             settings_flag="SCRAPER_SEC_EDGAR_ENABLED"))
        spec = get("probe")
        with patch("app.scraper.scraper_registry.get_source_enabled", return_value=True):
            assert spec.enabled() is True
        with patch("app.scraper.scraper_registry.get_source_enabled", return_value=False):
            assert spec.enabled() is False
        monkeypatch.setattr(settings, "SCRAPER_SEC_EDGAR_ENABLED", False)
        with patch("app.scraper.scraper_registry.get_source_enabled") as toggle:
            assert spec.enabled() is False
            assert not toggle.called, "the flag must short-circuit — no DB read when off"

    def test_registration_rejects_a_source_without_a_catalogue_entry(self, monkeypatch):
        import pytest as _pytest
        monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
        with _pytest.raises(ValueError, match="no KNOWN_SOURCES catalogue entry"):
            register(ScraperSpec("ghost", lambda q, d, c=None: {}, lambda: True))

    def test_registration_rejects_a_kind_that_disagrees_with_the_catalogue(
            self, monkeypatch, fake_sources):
        import pytest as _pytest
        fake_sources("probe")                                     # catalogue says instant
        monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
        with _pytest.raises(ValueError, match="disagrees with the catalogue"):
            register(ScraperSpec("probe", lambda q, d, c=None: {}, lambda: True,
                                 kind="bulk"))

    def test_registration_rejects_a_missing_settings_flag(self, monkeypatch, fake_sources):
        import pytest as _pytest
        fake_sources("probe")
        monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
        with _pytest.raises(ValueError, match="Settings has no SCRAPER_PROBE_ENABLED"):
            register(ScraperSpec("probe", lambda q, d, c=None: {}))   # derived enabled

    def test_a_custom_enabled_needs_no_flag(self, monkeypatch, fake_sources):
        fake_sources("probe")
        monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
        register(ScraperSpec("probe", lambda q, d, c=None: {}, lambda: True))
        assert get("probe").enabled() is True

    def test_the_flag_override_is_honoured(self, monkeypatch, fake_sources):
        # open_corporates' real flag drops the underscore — the convention's one
        # historical exception, which is why the override exists.
        spec = get("open_corporates")
        assert spec.flag_name == "SCRAPER_OPENCORPORATES_ENABLED"
