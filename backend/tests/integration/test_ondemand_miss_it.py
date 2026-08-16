"""
A search that finds nothing is remembered.

The freshness gate protects the sources by looking at the *company* — when it was last
scraped, how deep. A search that finds nothing has no company to hang that on, so
nothing recorded the attempt and every repeat ran every source again: click "search
sources for Alphabet" with France selected, get nothing, click again, and Wikidata and
EDGAR are both asked the same hopeless question a second time.

Against a real ArcadeDB because the memory is a row: the key has to round-trip through
the database, and a mocked session would agree with whatever the code did.
"""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def scraping(it_db, monkeypatch):
    """A registry with one source that finds nothing, and the DB pointed at the test
    database. Returns the call log."""
    from app.config import settings
    from app.scraper.scraper_registry import ScraperSpec, register

    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr(settings, "SCRAPER_ONDEMAND_COOLDOWN_HOURS", 24)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    calls: list = []
    register(ScraperSpec("faux", lambda q, d, c=None: (calls.append((q, c)),
                                                       {"status": "no_results", "total": 0})[1],
                         lambda: True, kind="instant"))
    return calls


def _ensure(query, country=None):
    from app.scraper import ondemand
    return ondemand.ensure_scrape(query, depth=1, force=True, country=country)


def test_the_same_fruitless_search_is_not_run_twice(scraping):
    first = _ensure("Alphabet", "FR")
    assert first["scraped"] and first["entity_id"] is None
    assert len(scraping) == 1

    second = _ensure("Alphabet", "FR")
    assert second["reason"] == "recently_missed"
    assert second["missed_at"]                      # says when, so a client can explain
    assert len(scraping) == 1                       # the source was not asked again


def test_another_country_is_another_question(scraping):
    _ensure("Alphabet", "FR")
    _ensure("Alphabet", "DE")
    # France having no Alphabet says nothing about Germany.
    assert [c[1] for c in scraping] == ["FR", "DE"]


def test_an_unrestricted_search_is_its_own_question_too(scraping):
    _ensure("Alphabet", "FR")
    _ensure("Alphabet")
    assert [c[1] for c in scraping] == ["FR", None]


def test_a_different_company_is_unaffected(scraping):
    _ensure("Alphabet", "FR")
    _ensure("Beta Industries", "FR")
    assert len(scraping) == 2


def test_the_same_name_written_differently_is_the_same_search(scraping):
    # Normalised on the way in — "Alphabet Inc." and "alphabet" are one question,
    # or the memory is trivially defeated by typing a suffix.
    _ensure("Alphabet Inc.", "FR")
    _ensure("alphabet", "FR")
    assert len(scraping) == 1


def test_it_forgets_once_the_cooldown_has_passed(scraping, monkeypatch):
    from app.db.arcadedb import run_sql
    from app.scraper import ondemand

    _ensure("Alphabet", "FR")
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    run_sql("UPDATE ScrapeMiss SET missed_at = :t WHERE key = :k",
            {"t": old, "k": ondemand._miss_key("Alphabet", "FR")})

    _ensure("Alphabet", "FR")
    assert len(scraping) == 2                       # asked again, a day later

    # …and the expired row was cleared on the way past rather than left to pile up.
    assert run_sql("SELECT count(*) AS n FROM ScrapeMiss WHERE key = :k",
                   {"k": ondemand._miss_key("Alphabet", "FR")})[0]["n"] == 1


def test_a_cooldown_of_zero_turns_the_memory_off(scraping, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SCRAPER_ONDEMAND_COOLDOWN_HOURS", 0)
    _ensure("Alphabet", "FR")
    _ensure("Alphabet", "FR")
    assert len(scraping) == 2


def test_finding_something_cancels_the_memory(it_db, monkeypatch):
    """A miss is not a verdict on the company, only on that moment. Once a source
    does find it, the next search must not be turned away by a stale miss."""
    from app.config import settings
    from app.db.arcadedb import run_sql
    from app.scraper import ondemand
    from app.scraper.graph_writer import set_scrape_target
    from app.scraper.mapper import normalize_entity_name
    from app.scraper.scraper_registry import ScraperSpec, register

    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    found = {"yet": False}

    def _run(q, d, c=None):
        if not found["yet"]:
            return {"status": "no_results", "total": 0}
        run_sql("UPDATE Entity SET name = :n, name_normalized = :nn, search_text = :st, "
                "type = 'company', country = 'DE' UPSERT WHERE id = 'ent-a'",
                {"n": q, "nn": normalize_entity_name(q), "st": q.lower()})
        set_scrape_target("ent-a", d)
        return {"status": "ok", "total": 1, "entity_id": "ent-a"}

    register(ScraperSpec("faux", _run, lambda: True, kind="instant"))

    assert ondemand.ensure_scrape("Acme", depth=1, force=True, country="DE")["entity_id"] is None
    found["yet"] = True

    # Still inside the cooldown, so this one is refused — the memory is doing its job.
    assert ondemand.ensure_scrape("Acme", depth=1, force=True,
                                  country="DE")["reason"] == "recently_missed"

    # Clear it the way a successful scrape does, and the next search goes through.
    run_sql("DELETE FROM ScrapeMiss WHERE key = :k", {"k": ondemand._miss_key("Acme", "DE")})
    out = ondemand.ensure_scrape("Acme", depth=1, force=True, country="DE")
    assert out["entity_id"] == "ent-a"

    # And the success left no miss behind for the next caller to trip over.
    assert run_sql("SELECT count(*) AS n FROM ScrapeMiss WHERE key = :k",
                   {"k": ondemand._miss_key("Acme", "DE")})[0]["n"] == 0
