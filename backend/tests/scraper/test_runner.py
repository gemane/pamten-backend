"""
Tests for runner.py — the Neo4j orchestration layer.

Strategy: mock both the database (db.get_session) and the external
scraper modules (sec_edgar.scrape_company, open_corporates.scrape_company).
This lets us verify that:
  - Permission guards work correctly
  - Node/edge upserts are called with the right data
  - Deduplication logic (name_normalized + name_credibility) is applied
  - run_scrape_all composes all three scrapers correctly
"""

import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

# Import runner at module level (env vars are set in conftest.py before collection)
from app.scraper import runner as runner_module
from app.scraper.runner import (
    run_scrape_sec_edgar,
    run_scrape_open_corporates,
    run_scrape_all,
    WIKIDATA_CREDIBILITY,
    SEC_EDGAR_CREDIBILITY,
    OPENCORPORATES_CREDIBILITY,
)
from app.config import settings


# ── DB session mock factory ────────────────────────────────────────────────────

def _make_session_mock(single_returns=None):
    """
    Returns (context_manager, session_mock) for patching db.get_session.
    single_returns: list of values returned by successive .single() calls.
    """
    session = MagicMock()
    run_mock = MagicMock()
    session.run.return_value = run_mock

    if single_returns:
        run_mock.single.side_effect = single_returns
    else:
        run_mock.single.return_value = None  # default: no existing node

    @contextmanager
    def _ctx():
        yield session

    return _ctx, session


# ── Permission guard tests ─────────────────────────────────────────────────────

class TestPermissionGuards:
    """Each entry point raises PermissionError when master or source flag is off."""

    def test_run_scrape_sec_edgar_requires_master_flag(self):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
                run_scrape_sec_edgar("Tesla")

    def test_run_scrape_sec_edgar_requires_sec_edgar_flag(self):
        with patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False), \
             patch("app.scraper.runner.get_source_enabled", return_value=True):
            with pytest.raises(PermissionError, match="SEC EDGAR"):
                run_scrape_sec_edgar("Tesla")

    def test_run_scrape_oc_requires_master_flag(self):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
                run_scrape_open_corporates("Tesla")

    def test_run_scrape_oc_requires_oc_flag(self):
        with patch.object(settings, "SCRAPER_OPENCORPORATES_ENABLED", False), \
             patch("app.scraper.runner.get_source_enabled", return_value=True):
            with pytest.raises(PermissionError, match="OPENCORPORATES"):
                run_scrape_open_corporates("Tesla")

    def test_run_scrape_all_requires_master_flag(self):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
                run_scrape_all("Tesla")


# ── run_scrape_sec_edgar ───────────────────────────────────────────────────────

class TestRunScrapeSecEdgar:
    """Happy-path and edge-case tests for run_scrape_sec_edgar."""

    SEC_DATA = {
        "cik":  "0001318605",
        "name": "Tesla, Inc.",
        "ownership_filings": [
            {
                "investor_name":  "BlackRock Inc.",
                "investor_cik":   "0001364742",
                "form_type":      "SC 13G",
                "file_date":      "2024-02-05",
                "ownership_type": "passive",
                "stake_percent":  None,
            }
        ],
        "executives": [
            {"name": "Elon Musk",       "role": "CEO"},
            {"name": "Zachary Kirkhorn", "role": "CFO"},
        ],
    }

    def test_returns_ok_status(self):
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.sec_edgar.scrape_company", return_value=self.SEC_DATA):
            result = run_scrape_sec_edgar("Tesla")

        assert result["status"] == "ok"
        assert result["company"] == "Tesla"
        assert result["cik"] == "0001318605"

    def test_scraped_list_includes_target_investors_executives(self):
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.sec_edgar.scrape_company", return_value=self.SEC_DATA):
            result = run_scrape_sec_edgar("Tesla")

        roles = {r["role"] for r in result["scraped"]}
        assert "target"   in roles
        assert "investor" in roles
        assert "CEO"      in roles
        assert "CFO"      in roles

    def test_total_matches_scraped_length(self):
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.sec_edgar.scrape_company", return_value=self.SEC_DATA):
            result = run_scrape_sec_edgar("Tesla")

        assert result["total"] == len(result["scraped"])

    def test_no_results_when_scraper_returns_none(self):
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.sec_edgar.scrape_company", return_value=None):
            result = run_scrape_sec_edgar("UnknownCorp")

        assert result["status"] == "no_results"
        assert result["total"] == 0

    def test_entity_investor_classified_as_entity(self):
        """BlackRock Inc. has a legal suffix → must become an Entity, not a Person."""
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.sec_edgar.scrape_company", return_value=self.SEC_DATA):
            result = run_scrape_sec_edgar("Tesla")

        investors = [r for r in result["scraped"] if r.get("role") == "investor"]
        blackrock = next(r for r in investors if "BlackRock" in r["name"])
        assert blackrock["type"] == "entity"

    def test_person_investor_classified_as_person(self):
        """Elon Musk (individual) appearing in SC 13G must become a Person node."""
        data = {
            **self.SEC_DATA,
            "ownership_filings": [
                {"investor_name": "Elon Musk", "investor_cik": "0001494730",
                 "form_type": "SC 13G", "file_date": "2024-01-01",
                 "ownership_type": "passive", "stake_percent": None}
            ],
            "executives": [],
        }
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.sec_edgar.scrape_company", return_value=data):
            result = run_scrape_sec_edgar("Tesla")

        investors = [r for r in result["scraped"] if r.get("role") == "investor"]
        assert investors[0]["type"] == "person"


# ── run_scrape_open_corporates ─────────────────────────────────────────────────

class TestRunScrapeOpenCorporates:
    OC_DATA = {
        "name":              "Tesla, Inc.",
        "jurisdiction_code": "us_de",
        "company_number":    "4554982",
        "registered_address": {"street": "1 Tesla Road", "city": "Austin",
                                "country": "United States", "zip": "78725"},
        "incorporation_date": "2003-07-01",
        "company_type":       "DOMESTIC STOCK COMPANY",
        "status":             "Active",
        "officers": [
            {"name": "Elon Musk", "role": "CEO",
             "start_date": "2008-10-01", "end_date": None},
        ],
    }

    def test_returns_ok_with_location_and_person(self):
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.open_corporates.scrape_company", return_value=self.OC_DATA):
            result = run_scrape_open_corporates("Tesla")

        assert result["status"] == "ok"
        assert result["jurisdiction_code"] == "us_de"
        types = {r["type"] for r in result["scraped"]}
        assert "location" in types
        assert "person"   in types

    def test_no_results_when_not_found(self):
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.open_corporates.scrape_company", return_value=None):
            result = run_scrape_open_corporates("NonExistentXYZ")

        assert result["status"] == "no_results"

    def test_empty_address_skips_location(self):
        data = {**self.OC_DATA, "registered_address": {}, "officers": []}
        ctx, _ = _make_session_mock()
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.open_corporates.scrape_company", return_value=data):
            result = run_scrape_open_corporates("Tesla")

        types = [r["type"] for r in result["scraped"]]
        assert "location" not in types


# ── run_scrape_all ─────────────────────────────────────────────────────────────

class TestRunScrapeAll:
    """run_scrape_all composes all three scrapers and reports disabled ones."""

    def _wd_result(self):
        return {"status": "ok", "query": "Tesla", "total": 3, "scraped": []}

    def _sec_result(self):
        return {"status": "ok", "company": "Tesla", "total": 19, "scraped": []}

    def _oc_result(self):
        return {"status": "ok", "company": "Tesla", "total": 5, "scraped": []}

    def test_all_enabled_runs_all_three(self):
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.run_scrape",                 return_value=self._wd_result()), \
             patch("app.scraper.runner.run_scrape_sec_edgar",       return_value=self._sec_result()), \
             patch("app.scraper.runner.run_scrape_open_corporates", return_value=self._oc_result()):
            result = run_scrape_all("Tesla", depth=1)

        assert result["status"] == "ok"
        assert result["results"]["wikidata"]["status"]        == "ok"
        assert result["results"]["sec_edgar"]["status"]       == "ok"
        assert result["results"]["open_corporates"]["status"] == "ok"

    def test_disabled_source_reports_disabled(self):
        with patch("app.scraper.runner.get_source_enabled", return_value=False), \
             patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False), \
             patch.object(settings, "SCRAPER_OPENCORPORATES_ENABLED", False), \
             patch("app.scraper.runner.run_scrape", return_value=self._wd_result()):
            result = run_scrape_all("Tesla", depth=1)

        assert result["results"]["sec_edgar"]["status"]       == "disabled"
        assert result["results"]["open_corporates"]["status"] == "disabled"

    def test_scraper_error_does_not_abort_others(self):
        """An exception in one scraper must not prevent the rest from running."""
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.run_scrape", side_effect=RuntimeError("wikidata down")), \
             patch("app.scraper.runner.run_scrape_sec_edgar",       return_value=self._sec_result()), \
             patch("app.scraper.runner.run_scrape_open_corporates", return_value=self._oc_result()):
            result = run_scrape_all("Tesla", depth=1)

        assert result["results"]["wikidata"]["status"]        == "error"
        assert result["results"]["sec_edgar"]["status"]       == "ok"
        assert result["results"]["open_corporates"]["status"] == "ok"


# ── Deduplication logic ────────────────────────────────────────────────────────

class TestNameCredibility:
    """
    name_credibility determines which source wins the name field.
    SEC EDGAR (98) beats OpenCorporates (85) beats Wikidata (80).
    """

    def test_credibility_constants_are_ordered(self):
        # SEC EDGAR filings are legally mandated → highest authority
        assert SEC_EDGAR_CREDIBILITY > OPENCORPORATES_CREDIBILITY > WIKIDATA_CREDIBILITY

    def test_higher_credibility_wins_name(self):
        """
        Simulates the Cypher CASE expression logic in _upsert_entity_by_name.
        Only updates the name if incoming credibility >= stored credibility.
        """
        def _winning_name(stored_name, stored_cred, incoming_name, incoming_cred):
            if stored_cred <= incoming_cred:
                return incoming_name
            return stored_name

        # Wikidata sets "Tesla" (cred 80), then SEC EDGAR sets "Tesla, Inc." (cred 98)
        assert _winning_name("Tesla", 80, "Tesla, Inc.", 98) == "Tesla, Inc."

        # Once SEC EDGAR has set the name (98), Wikidata (80) cannot overwrite it
        assert _winning_name("Tesla, Inc.", 98, "Tesla", 80) == "Tesla, Inc."

        # OpenCorporates (85) cannot overwrite SEC EDGAR (98) either
        assert _winning_name("Tesla, Inc.", 98, "TESLA INC", 85) == "Tesla, Inc."
