"""
Real-ArcadeDB test: the country chosen in the search box narrows what `ensure_scrape`
considers to be "the company you meant".

Two companies share a name across countries — the ordinary case, not a contrived one:
Alphabet Inc of Mountain View and a German Alphabet GmbH. Without the country, the
resolver finds the American one, calls it fresh, and answers a German query with it.
That is a wrong answer no source ever gets asked about, so it cannot be caught by
mocking the sources: it happens in the database lookup, in Cypher, before any scrape.
"""
import pytest

pytestmark = pytest.mark.integration


def _entity(it_db, eid: str, name: str, country: str) -> None:
    """A company that already looks scraped and fresh, so the freshness gate serves it
    from the DB and the test is purely about *which* row is found."""
    from app.scraper.mapper import normalize_entity_name
    from datetime import datetime, timezone

    it_db.run_command(
        f"CREATE (:Entity {{id:'{eid}', name:'{name}', type:'company', "
        f"country:'{country}', name_normalized:'{normalize_entity_name(name)}', "
        f"search_text:'{name.lower()}', on_demand_scraped:true, scrape_depth:1, "
        f"last_scraped_at:'{datetime.now(timezone.utc).isoformat()}'}})"
    )


@pytest.fixture
def two_alphabets(it_db):
    _entity(it_db, "ent-us", "Alphabet Inc", "US")
    _entity(it_db, "ent-de", "Alphabet GmbH", "DE")
    return it_db


def _ensure(query, country=None):
    from app.scraper import ondemand
    return ondemand.ensure_scrape(query, depth=1, force=False, country=country)


def test_the_country_decides_which_company_is_meant(two_alphabets):
    assert _ensure("Alphabet", country="DE")["entity_id"] == "ent-de"
    assert _ensure("Alphabet", country="US")["entity_id"] == "ent-us"


def test_without_a_country_the_search_is_unrestricted(two_alphabets):
    # Either is a legitimate answer to an unrestricted query; what matters is that
    # one is found rather than the lookup coming back empty.
    assert _ensure("Alphabet")["entity_id"] in {"ent-us", "ent-de"}


def test_a_country_with_nothing_in_it_finds_nothing_to_serve(two_alphabets, monkeypatch):
    from app.config import settings

    # Scraper off, so this is the DB-only path: no company in France, and the US one
    # must not be offered as a substitute.
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", False)
    out = _ensure("Alphabet", country="FR")
    assert out["entity_id"] is None and out["profile"] is None


def test_the_country_is_case_insensitive_end_to_end(two_alphabets):
    # It arrives from a JSON body; ISO-2 comparisons below are upper-case.
    assert _ensure("Alphabet", country="de")["entity_id"] == "ent-de"


def test_the_country_reaches_the_sources_too(it_db, monkeypatch):
    """The other half: what the sources are told.

    A fake instant source records the country it was handed. The DB resolve and the
    source hand-off are separate paths — scoping only the first still imports the
    wrong company the moment the right one is absent.
    """
    from app.config import settings
    from app.scraper import ondemand
    from app.scraper.scraper_registry import ScraperSpec, register

    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})

    seen: list = []
    register(ScraperSpec(
        "faux", lambda q, d, c=None: (seen.append(c), {"status": "ok", "total": 0})[1],
        lambda: True, kind="instant"))

    ondemand.ensure_scrape("Nothing In The Database", depth=1, country="DE")
    assert seen == ["DE"]
