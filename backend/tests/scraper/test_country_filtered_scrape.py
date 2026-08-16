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


# ── Wikidata: the country is part of the question ─────────────────────────────
#
# Not a filter over global results — those are the wrong results. "Alphabet" asked
# of Wikidata returns the Mountain View company; Alphabet Fuhrparkmanagement, the
# German company of that name, is nowhere near the top and no amount of filtering
# afterwards would ever reach it.

class TestWikidataSearch:
    def test_a_country_makes_it_search_inside_that_country(self):
        with patch("app.scraper.runner.search_entity_in_country",
                   return_value=[{"id": "Q2650924", "label": "Alphabet Fuhrparkmanagement"}]) as scoped, \
             patch("app.scraper.runner.search_entity") as global_search, \
             patch("app.scraper.runner._scrape_node"), \
             patch("app.scraper.runner._ensure_source", return_value="s1"):
            runner.run_scrape("Alphabet", depth=0, country="DE")
        scoped.assert_called_once_with("Alphabet", "DE")
        global_search.assert_not_called()      # the world's best is not fetched at all

    def test_no_country_keeps_the_unrestricted_search(self):
        with patch("app.scraper.runner.search_entity", return_value=[{"id": "Q20800404"}]) as global_search, \
             patch("app.scraper.runner.search_entity_in_country") as scoped, \
             patch("app.scraper.runner._scrape_node"), \
             patch("app.scraper.runner._ensure_source", return_value="s1"):
            runner.run_scrape("Alphabet", depth=0)
        global_search.assert_called_once()
        scoped.assert_not_called()

    def test_nothing_in_that_country_is_a_real_answer(self):
        # Not "mismatch" — we never looked anywhere else, so there is nothing to
        # report having found instead.
        with patch("app.scraper.runner.search_entity_in_country", return_value=[]), \
             patch("app.scraper.runner._scrape_node") as scrape_node:
            out = runner.run_scrape("Alphabet", depth=1, country="FR")
        assert out["status"] == "no_results" and out["requested_country"] == "FR"
        scrape_node.assert_not_called()

    def test_it_scrapes_the_company_the_country_search_found(self):
        with patch("app.scraper.runner.search_entity_in_country",
                   return_value=[{"id": "Q2650924", "label": "Alphabet Fuhrparkmanagement"},
                                 {"id": "Q999", "label": "Jeannes Alphabet"}]), \
             patch("app.scraper.runner._ensure_source", return_value="s1"), \
             patch("app.scraper.runner._scrape_node") as scrape_node:
            runner.run_scrape("Alphabet", depth=1, country="DE")
        assert scrape_node.call_args[0][0] == "Q2650924"


class TestRankingWhatTheCountrySearchReturns:
    """The country-restricted search ranks by text relevance over the whole item,
    which is not the same as "is this the company I named"."""

    def rank(self, labels, query):
        from app.scraper.wikidata import rank_by_name
        cands = [{"id": f"Q{i}", "label": lab, "order": i} for i, lab in enumerate(labels)]
        return [c["label"] for c in rank_by_name(cands, query)]

    def test_the_named_company_beats_a_competition_named_after_it(self):
        # "barclays" in the UK really does come back with the Premier League
        # first: it was the Barclays Premier League and the alias is still there.
        assert self.rank(["Premier League", "Barclays", "ATP Finals"], "Barclays")[0] == "Barclays"

    def test_accents_do_not_decide(self):
        # Otherwise "Nestle" starts-with-matches "Nestle Nido" and loses "Nestlé".
        assert self.rank(["Nestle Nido", "Nestlé"], "Nestle")[0] == "Nestlé"

    def test_a_legal_suffix_does_not_decide(self):
        assert self.rank(["Alphabet City", "Alphabet Inc."], "Alphabet")[0] == "Alphabet Inc."

    def test_the_search_order_breaks_a_tie(self):
        assert self.rank(["Siemens Mobile", "Siemens Energy"], "Siemens Something") == [
            "Siemens Mobile", "Siemens Energy"]


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

    def test_a_filer_that_states_no_incorporation_is_rejected(self):
        # Deutsche Bank AG is the real case: it files with the SEC and leaves
        # `stateOfIncorporation` empty. Asked for a company in Germany, a record
        # that cannot say where it is is not the answer.
        assert _proceeded_past_the_check(None, "DE") is False

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
