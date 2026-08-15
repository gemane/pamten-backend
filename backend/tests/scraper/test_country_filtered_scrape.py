"""
Asked for one country, a source must not answer with another.

Left to itself, every instant source answers "Alphabet" with Alphabet Inc of
Mountain View — it is the most famous company by that name, and none of them
knows a country was chosen unless it is handed one. These tests cover the point
in each runner where that decision is made, which is always *before* the first
write, so a rejection leaves nothing behind.

Network and database are mocked. What matters here is the choice, not the fetch.
"""
from unittest.mock import patch

import pytest

from app.config import settings
from app.scraper import runner


@pytest.fixture(autouse=True)
def _scraper_on(monkeypatch):
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_WIKIDATA_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_SEC_EDGAR_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr(runner, "get_source_enabled", lambda _name: True)


# ── Wikidata: choosing among the candidates ───────────────────────────────────

CANDIDATES = [{"id": "Q1"}, {"id": "Q2"}, {"id": "Q3"}]


def _countries(mapping):
    """Patch the one SPARQL call the picker makes."""
    return patch("app.scraper.wikidata.countries_for",
                 lambda qids: {q: {"country": c, "hq_country": None} for q, c in mapping.items()})


class TestWikidataCandidate:
    def test_takes_the_top_hit_when_no_country_is_asked_for(self):
        assert runner._pick_candidate(CANDIDATES, None) == "Q1"

    def test_takes_the_candidate_in_the_asked_for_country(self):
        # The whole feature in one line: the German one is not the top hit.
        with _countries({"Q1": "US", "Q2": "DE", "Q3": "GB"}):
            assert runner._pick_candidate(CANDIDATES, "DE") == "Q2"

    def test_prefers_a_stated_match_over_one_that_states_nothing(self):
        with _countries({"Q1": None, "Q2": "DE", "Q3": None}):
            assert runner._pick_candidate(CANDIDATES, "DE") == "Q2"

    def test_falls_back_to_a_candidate_with_no_country_of_its_own(self):
        # Unknown is not a mismatch — see country_match. A Wikidata item without
        # P17 is ordinary, and rejecting it would lose real companies.
        with _countries({"Q1": "US", "Q2": None, "Q3": "GB"}):
            assert runner._pick_candidate(CANDIDATES, "DE") == "Q2"

    def test_rejects_when_every_candidate_is_somewhere_else(self):
        with _countries({"Q1": "US", "Q2": "GB", "Q3": "FR"}):
            assert runner._pick_candidate(CANDIDATES, "DE") is None

    def test_the_case_of_the_asked_for_country_does_not_decide(self):
        with _countries({"Q1": "US", "Q2": "DE", "Q3": "GB"}):
            assert runner._pick_candidate(CANDIDATES, "de") == "Q2"


class TestWikidataRun:
    def test_a_rejected_query_scrapes_nothing(self):
        with patch("app.scraper.runner.search_entity", return_value=CANDIDATES), \
             _countries({"Q1": "US", "Q2": "GB", "Q3": "FR"}), \
             patch("app.scraper.runner._scrape_node") as scrape_node:
            out = runner.run_scrape("Alphabet", depth=1, country="DE")
        assert out["status"] == "country_mismatch"
        assert out["total"] == 0
        scrape_node.assert_not_called()        # nothing written, nothing to undo

    def test_the_rejection_names_what_was_found_instead(self):
        with patch("app.scraper.runner.search_entity", return_value=CANDIDATES), \
             _countries({"Q1": "US", "Q2": "GB", "Q3": "US"}), \
             patch("app.scraper.runner._scrape_node"):
            out = runner.run_scrape("Alphabet", depth=1, country="DE")
        assert out["found_country"] == "GB, US" and out["requested_country"] == "DE"


# ── SEC EDGAR: checking the filer ─────────────────────────────────────────────

FILER = {"name": "ALPHABET INC.", "cik": "0001652044"}


class Reached(Exception):
    """Raised by the first write to prove the country check let the scrape past.

    Stopping there keeps these tests off the database entirely: what is being
    tested is the decision, and everything after it is the ordinary SEC scrape
    that its own tests already cover.
    """


def _sec(filer_country):
    """The two SEC calls the country check sits between."""
    return (patch("app.scraper.sec_edgar.scrape_company", return_value=dict(FILER)),
            patch("app.scraper.sec_edgar.fetch_filer_country", return_value=filer_country))


def _proceeded_past_the_check(filer_country, requested):
    """Did the scrape get as far as writing? True when the match was accepted."""
    scrape, country = _sec(filer_country)
    with scrape, country, patch("app.scraper.runner._ensure_source", side_effect=Reached):
        try:
            runner.run_scrape_sec_edgar("Alphabet", country=requested)
        except Reached:
            return True
    return False


class TestSecEdgar:
    def test_a_us_filer_is_rejected_for_a_german_query(self):
        scrape, country = _sec("US")
        with scrape, country, patch("app.scraper.runner._ensure_source") as src:
            out = runner.run_scrape_sec_edgar("Alphabet", country="DE")
        assert out["status"] == "country_mismatch" and out["total"] == 0
        # Rejected before the source row is even created, let alone the entity.
        src.assert_not_called()

    def test_a_matching_filer_is_kept(self):
        assert _proceeded_past_the_check("US", "US") is True

    def test_a_filer_of_unknown_country_is_kept(self):
        # EDGAR knows nothing about where this one is registered; that is not a
        # claim that it is somewhere else.
        assert _proceeded_past_the_check(None, "DE") is True

    def test_without_a_country_every_filer_is_kept(self):
        assert _proceeded_past_the_check("US", None) is True


# ── OpenCorporates: the jurisdiction code ─────────────────────────────────────

class TestOpenCorporates:
    @pytest.fixture(autouse=True)
    def _oc_on(self, monkeypatch):
        monkeypatch.setattr(settings, "SCRAPER_OPENCORPORATES_ENABLED", True)

    def test_a_jurisdiction_in_another_country_is_rejected(self):
        # "us_de" is Delaware; its first two characters are the country.
        with patch("app.scraper.open_corporates.scrape_company",
                   return_value={"name": "ALPHABET INC.", "jurisdiction_code": "us_de"}), \
             patch("app.scraper.runner._ensure_source") as src:
            out = runner.run_scrape_open_corporates("Alphabet", country="DE")
        assert out["status"] == "country_mismatch" and out["found_country"] == "US"
        src.assert_not_called()

    def test_a_matching_jurisdiction_is_kept(self):
        with patch("app.scraper.open_corporates.scrape_company",
                   return_value={"name": "ALPHABET GMBH", "jurisdiction_code": "de"}), \
             patch("app.scraper.runner._ensure_source", return_value="s1"), \
             patch("app.scraper.runner._upsert_entity_by_name", return_value="e1"):
            out = runner.run_scrape_open_corporates("Alphabet", country="DE")
        assert out["status"] != "country_mismatch"
