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
from app.scraper.runner import (
    run_scrape,
    run_scrape_sec_edgar,
    run_scrape_open_corporates,
    run_scrape_all,
    _upsert_entity,
    _upsert_entity_by_name,
    _upsert_person,
    _upsert_owns,
    _upsert_succession,
    _merge_aliases,
    _scrape_node,
    _upsert_role,
    _upsert_role_sec,
    _wikidata_url,
    _opencorporates_url,
    WIKIDATA_CREDIBILITY,
    SEC_EDGAR_CREDIBILITY,
    OPENCORPORATES_CREDIBILITY,
)
from app.config import settings


# ── Provenance URL helpers (pure) ──────────────────────────────────────────────

class TestProvenanceUrlHelpers:
    def test_wikidata_url_builds_qid_page(self):
        assert _wikidata_url("Q95") == "https://www.wikidata.org/wiki/Q95"

    def test_wikidata_url_none_when_missing(self):
        assert _wikidata_url(None) is None

    def test_opencorporates_url_builds_company_page(self):
        assert _opencorporates_url("gb", "01234567") == \
            "https://opencorporates.com/companies/gb/01234567"

    def test_opencorporates_url_none_when_incomplete(self):
        assert _opencorporates_url("gb", None) is None
        assert _opencorporates_url(None, "01234567") is None


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

    def test_run_scrape_requires_wikidata_flag(self):
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch.object(settings, "SCRAPER_WIKIDATA_ENABLED", False):
            with pytest.raises(PermissionError, match="SCRAPER_WIKIDATA_ENABLED"):
                run_scrape("Tesla")

    def test_run_scrape_sec_edgar_requires_master_flag(self):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
                run_scrape_sec_edgar("Tesla")

    def test_run_scrape_sec_edgar_requires_sec_edgar_flag(self):
        with patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.scraper_registry.get_source_enabled", return_value=True):
            with pytest.raises(PermissionError, match="SEC EDGAR"):
                run_scrape_sec_edgar("Tesla")

    def test_run_scrape_oc_requires_master_flag(self):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
                run_scrape_open_corporates("Tesla")

    def test_run_scrape_oc_requires_oc_flag(self):
        with patch.object(settings, "SCRAPER_OPENCORPORATES_ENABLED", False), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.scraper_registry.get_source_enabled", return_value=True):
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
        # `geocode_address` is stubbed because the scrape geocodes the registered
        # address it has just written: a live Nominatim request, to a rate-limited
        # public service, from a test about OpenCorporates. The geocoder has its
        # own tests.
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.runner.geocode_address", return_value=None), \
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
             patch("app.scraper.scraper_registry.get_source_enabled", return_value=True), \
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
             patch("app.scraper.scraper_registry.get_source_enabled", return_value=False), \
             patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False), \
             patch.object(settings, "SCRAPER_OPENCORPORATES_ENABLED", False), \
             patch("app.scraper.runner.run_scrape", return_value=self._wd_result()):
            result = run_scrape_all("Tesla", depth=1)

        assert result["results"]["sec_edgar"]["status"]       == "disabled"
        assert result["results"]["open_corporates"]["status"] == "disabled"

    def test_wikidata_flag_off_reports_disabled(self):
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.scraper_registry.get_source_enabled", return_value=True), \
             patch.object(settings, "SCRAPER_WIKIDATA_ENABLED", False), \
             patch("app.scraper.runner.run_scrape_sec_edgar",       return_value=self._sec_result()), \
             patch("app.scraper.runner.run_scrape_open_corporates", return_value=self._oc_result()):
            result = run_scrape_all("Tesla", depth=1)

        assert result["results"]["wikidata"]["status"] == "disabled"
        assert result["results"]["sec_edgar"]["status"] == "ok"

    def test_scraper_error_does_not_abort_others(self):
        """An exception in one scraper must not prevent the rest from running."""
        with patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.scraper_registry.get_source_enabled", return_value=True), \
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


# ── run_scrape (Wikidata) ─────────────────────────────────────────────────────

class TestRunScrapeWikidata:
    SEARCH_RESULT = [{"id": "Q380", "label": "Apple Inc.", "description": "tech co"}]
    COMPANY_DATA  = {
        "qid":         "Q380",
        "name":        "Apple Inc.",
        "description": "American technology company",
        "instances":   ["Q4830453"],
        "country":     "US",
        "founded":     1976,
        "revenue":     394328000000.0,
        "subsidiaries": [],
        "parents":     [],
        "ceos":        [],
    }

    @pytest.fixture(autouse=True)
    def _offline(self):
        """`run_scrape` asks Wikidata to classify each search hit before choosing
        one. These tests stub the search and the company data but not that, so
        every one of them was making a live API call — several seconds each, and
        eventually a CI timeout on an unrelated branch.

        Class-level rather than per-test: the dependency belongs to `run_scrape`,
        so every test here has it, and the next one added would inherit the hole."""
        with patch("app.scraper.runner.pick_candidate",
                   side_effect=lambda results, kind: results[0]["id"] if results else None):
            yield

    def _ctx(self):
        return _make_session_mock()[0]

    def test_raises_when_scraper_disabled(self):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
                run_scrape("Apple")

    def test_raises_when_wikidata_source_disabled(self):
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=False):
            with pytest.raises(PermissionError, match="[Ww]ikidata"):
                run_scrape("Apple")

    def test_returns_no_results_when_search_empty(self):
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.search_entity", return_value=[]):
            result = run_scrape("NoSuchCompany")
        assert result["status"] == "no_results"
        assert result["total"] == 0

    def test_returns_ok_with_scraped_entity(self):
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.search_entity", return_value=self.SEARCH_RESULT), \
             patch("app.scraper.runner.fetch_company_data", return_value=self.COMPANY_DATA), \
             patch("app.scraper.runner.db.get_session", self._ctx()):
            result = run_scrape("Apple", depth=1)
        assert result["status"] == "ok"
        assert result["wikidata_id"] == "Q380"
        assert result["total"] >= 1
        assert any(e["qid"] == "Q380" for e in result["scraped"])

    def test_depth_is_capped_at_3(self):
        scraped_calls = []
        def fake_scrape_node(qid, depth, *a, **kw):
            scraped_calls.append(depth)
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.search_entity", return_value=self.SEARCH_RESULT), \
             patch("app.scraper.runner._scrape_node", side_effect=fake_scrape_node), \
             patch("app.scraper.runner._ensure_source", return_value="src-1"):
            run_scrape("Apple", depth=99)
        assert scraped_calls[0] == 3

    def test_subsidiaries_are_written_as_owns_edges(self):
        data_with_sub = {
            **self.COMPANY_DATA,
            "subsidiaries": [{"qid": "Q312", "name": "Apple Records", "instances": []}],
        }
        owns_calls = []
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.search_entity", return_value=self.SEARCH_RESULT), \
             patch("app.scraper.runner.fetch_company_data", return_value=data_with_sub), \
             patch("app.scraper.runner.db.get_session", self._ctx()), \
             patch("app.scraper.runner._upsert_owns",
                   side_effect=lambda *a, **kw: owns_calls.append(a)):
            run_scrape("Apple", depth=1)
        assert len(owns_calls) >= 1

    def test_ceos_are_written_as_has_role_edges(self):
        data_with_ceo = {
            **self.COMPANY_DATA,
            "ceos": [{"qid": "Q88", "label": "Tim Cook",
                      "nationality": "US", "description": "",
                      "since": "2011-08-24", "until": None}],
        }
        role_calls = []
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.search_entity", return_value=self.SEARCH_RESULT), \
             patch("app.scraper.runner.fetch_company_data", return_value=data_with_ceo), \
             patch("app.scraper.runner.db.get_session", self._ctx()), \
             patch("app.scraper.runner._upsert_role",
                   side_effect=lambda *a, **kw: role_calls.append(a)):
            run_scrape("Apple", depth=1)
        assert len(role_calls) >= 1

    def test_non_human_officer_is_skipped(self):
        # a company wrongly listed as a subsidiary's "founder" (is_human False)
        # must NOT be created as a Person / HAS_ROLE.
        data = {
            **self.COMPANY_DATA,
            "officers": [
                {"qid": "Q312", "label": "Apple Inc.", "role": "Founder", "is_human": False},
                {"qid": "Q19837", "label": "Steve Jobs", "role": "Founder", "is_human": True},
            ],
        }
        person_calls = []
        with patch.object(settings, "SCRAPER_ENABLED", True), \
             patch("app.scraper.runner.get_source_enabled", return_value=True), \
             patch("app.scraper.runner.search_entity", return_value=self.SEARCH_RESULT), \
             patch("app.scraper.runner.fetch_company_data", return_value=data), \
             patch("app.scraper.runner.db.get_session", self._ctx()), \
             patch("app.scraper.runner._upsert_role", side_effect=lambda *a, **kw: None), \
             patch("app.scraper.runner._upsert_person",
                   side_effect=lambda **kw: person_calls.append(kw["full_name"]) or "pid"):
            run_scrape("Apple", depth=1)
        assert "Steve Jobs" in person_calls
        assert "Apple Inc." not in person_calls      # the company was skipped


# ── Wikidata DB helpers ───────────────────────────────────────────────────────

class TestUpsertEntity:
    def test_creates_new_entity_when_not_found(self):
        ctx, session = _make_session_mock()   # all lookups miss
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_entity("Acme", "company", "US", 2000, None, None, "Q1")
        # 3 sequential indexed resolution lookups (wikidata_id/name/name_normalized)
        # all miss, then CREATE
        assert session.run.call_count == 4

    def test_updates_existing_entity_when_found(self):
        ctx, session = _make_session_mock(single_returns=[{"id": "existing-uuid"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            eid = _upsert_entity("Acme", "company", "US", 2000, None, None, "Q1")
        assert eid == "existing-uuid"
        # run called twice: MATCH then SET
        assert session.run.call_count == 2

    def test_returns_string_id(self):
        ctx, _ = _make_session_mock()   # all lookups miss → creates a new node
        with patch("app.scraper.runner.db.get_session", ctx):
            eid = _upsert_entity("Acme", "company", None, None, None, None, "Q1")
        assert isinstance(eid, str) and len(eid) > 0

    def test_sets_search_text_on_create(self):
        # search_text (name + description + aliases) must be written so the
        # scraped entity enters the FULL_TEXT /search index.
        ctx, session = _make_session_mock()   # miss → CREATE
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_entity("Acme Corp", "company", None, None, None, "A widget maker",
                           "Q1", aliases=["Acme", "ACME Inc"])
        create = session.run.call_args_list[-1]
        assert "search_text: $search_text" in create.args[0]
        assert create.kwargs["search_text"] == "Acme Corp A widget maker Acme ACME Inc"

    def test_sets_search_text_on_update(self):
        ctx, session = _make_session_mock(single_returns=[{"id": "e1"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_entity("Acme", "company", None, None, None, None, "Q1")
        upd = session.run.call_args_list[-1]
        assert "e.search_text     = $search_text" in upd.args[0]
        assert upd.kwargs["search_text"] == "Acme"


class TestUpsertPerson:
    def test_creates_new_person_when_not_found(self):
        ctx, session = _make_session_mock()   # all lookups miss
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_person("Tim Cook", "US", "Apple CEO", "Q88")
        # 2 sequential indexed lookups (wikidata_id/full_name) miss, then CREATE
        assert session.run.call_count == 3

    def test_returns_existing_id_and_backfills_without_create(self):
        ctx, session = _make_session_mock(single_returns=[{"id": "person-uuid"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            pid = _upsert_person("Tim Cook", "US", "", "Q88")
        assert pid == "person-uuid"
        # MATCH + a detail backfill, but no CREATE (person already exists).
        assert session.run.call_count == 2
        backfill_cypher = session.run.call_args_list[1].args[0]
        assert "SET p.birth_date" in backfill_cypher
        assert "CREATE" not in backfill_cypher


class TestUpsertOwns:
    def test_creates_edge_when_not_exists(self):
        ctx, session = _make_session_mock(single_returns=[None])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_owns("owner-id", "owned-id", "src-1")
        assert session.run.call_count == 2

    def test_refreshes_and_backfills_when_edge_exists(self):
        ctx, session = _make_session_mock(single_returns=[{"r": "exists"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_owns("owner-id", "owned-id", "src-1",
                         source_url="https://www.wikidata.org/wiki/Q2283")
        # EXISTS check + a refresh, but no CREATE
        assert session.run.call_count == 2
        second_cypher = session.run.call_args_list[1].args[0]
        assert "SET r.last_scraped_at" in second_cypher
        assert "CREATE" not in second_cypher
        # Re-scrape backfills the specific record URL onto the existing edge
        assert "r.source_url" in second_cypher and "COALESCE" in second_cypher
        assert session.run.call_args_list[1].kwargs.get("surl") == \
            "https://www.wikidata.org/wiki/Q2283"


class TestUpsertRole:
    def test_creates_role_edge_when_not_exists(self):
        # Three statements for a DATED role that is genuinely new: the match, then
        # the "is there an undated edge for this role to adopt?" lookup, then the
        # CREATE. The middle one is what stops a person appearing twice on a board
        # when one scrape knows the start date and the other does not.
        ctx, session = _make_session_mock(single_returns=[None, None])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_role("p-id", "e-id", "CEO", "src-1", since="2011-08-24")
        assert session.run.call_count == 3
        assert "CREATE" in session.run.call_args_list[2].args[0]

    def test_an_undated_role_needs_no_adoption_lookup(self):
        ctx, session = _make_session_mock(single_returns=[None])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_role("p-id", "e-id", "CEO", "src-1")
        assert session.run.call_count == 2          # match, then CREATE

    def test_refreshes_and_backfills_when_same_role_and_since_exists(self):
        ctx, session = _make_session_mock(single_returns=[{"r": "exists"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_role("p-id", "e-id", "CEO", "src-1", since="2011-08-24",
                         source_url="https://www.wikidata.org/wiki/Q2283")
        # EXISTS check + a refresh, but no CREATE
        assert session.run.call_count == 2
        second_cypher = session.run.call_args_list[1].args[0]
        assert "SET r.last_scraped_at" in second_cypher
        assert "CREATE" not in second_cypher
        # Re-scrape backfills the specific record URL onto the existing edge
        assert "r.source_url" in second_cypher and "COALESCE" in second_cypher


class TestUpsertRoleSec:
    FORM4 = "https://www.sec.gov/Archives/edgar/data/789019/0001/form4.xml"

    def test_creates_role_with_form4_provenance(self):
        ctx, session = _make_session_mock(single_returns=[None])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_role_sec("p-id", "e-id", "Director", "sec-1",
                             source_url=self.FORM4, source_date="2024-02-13")
        assert session.run.call_count == 2
        create_cypher = session.run.call_args_list[1].args[0]
        create_kwargs = session.run.call_args_list[1].kwargs
        assert "CREATE" in create_cypher
        assert create_kwargs.get("surl") == self.FORM4
        assert create_kwargs.get("sdate") == "2024-02-13"

    def test_backfills_form4_provenance_when_role_exists(self):
        ctx, session = _make_session_mock(single_returns=[{"r": "exists"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_role_sec("p-id", "e-id", "Director", "sec-1",
                             source_url=self.FORM4, source_date="2024-02-13")
        assert session.run.call_count == 2  # EXISTS check + backfill, no CREATE
        second_cypher = session.run.call_args_list[1].args[0]
        assert "SET r.last_scraped_at" in second_cypher
        assert "CREATE" not in second_cypher
        assert "r.source_url" in second_cypher and "COALESCE" in second_cypher
        assert session.run.call_args_list[1].kwargs.get("surl") == self.FORM4


# ── _upsert_person: person detail (birth/death/aliases/nationalities) ──────────

class TestUpsertPersonDetail:
    def _create_call(self, session):
        return next(c for c in session.run.call_args_list
                    if "CREATE (p:Person" in c.args[0])

    def _backfill_call(self, session):
        return next(c for c in session.run.call_args_list
                    if "SET p.birth_date" in c.args[0])

    def test_create_stores_birth_death_aliases_nationalities(self):
        ctx, session = _make_session_mock()  # no existing person
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_person(
                "Elon Musk", nationality=None, description=None,
                wikidata_id="Q317521",
                birth_date="1971-06-28", death_date=None, birth_place="Pretoria",
                aliases=["Elon"], nationalities=["US", "CA"],
            )
        create = self._create_call(session)
        assert create.kwargs["bdate"] == "1971-06-28"
        assert create.kwargs["bplace"] == "Pretoria"
        assert create.kwargs["aliases"] == ["Elon"]
        assert create.kwargs["nats"] == ["US", "CA"]
        # single nationality is derived from the first of the list when not given
        assert create.kwargs["nat"] == "US"

    def test_existing_person_backfills_detail(self):
        ctx, session = _make_session_mock(single_returns=[{"id": "p-1"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            pid = _upsert_person(
                "Elon Musk", nationality=None, description=None,
                wikidata_id="Q317521",
                birth_date="1971-06-28", birth_place="Pretoria",
                aliases=["Elon"], nationalities=["US"],
            )
        assert pid == "p-1"
        backfill = self._backfill_call(session)
        assert backfill.kwargs["bdate"] == "1971-06-28"
        assert backfill.kwargs["bplace"] == "Pretoria"
        assert backfill.kwargs["aliases"] == ["Elon"]
        assert backfill.kwargs["nats"] == ["US"]

    def test_defaults_to_empty_lists_when_detail_absent(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_person("Jane Doe", nationality=None, description=None,
                           wikidata_id="Q2")
        create = self._create_call(session)
        assert create.kwargs["aliases"] == []
        assert create.kwargs["nats"] == []
        assert create.kwargs["bdate"] is None


# ── SEC insider (Form 4) holdings → OWNS edges ───────────────────────────────

def test_sec_insider_with_shares_gets_owns_edge():
    from app.scraper.runner import run_scrape_sec_edgar
    data = {
        "cik": "0001364742", "name": "BlackRock",
        "ownership_filings": [],
        "executives": [
            {"name": "Larry Fink", "role": "CEO", "shares_owned": 500000, "stake_percent": 0.34,
             "source_url": "https://sec.gov/x", "source_date": "2024-01-01"},
            {"name": "No Shares Director", "role": "Director", "shares_owned": None},
        ],
    }
    owns = []
    ctx, _ = _make_session_mock()
    with patch("app.scraper.runner.get_source_enabled", return_value=True), \
         patch("app.scraper.runner.db.get_session", ctx), \
         patch("app.scraper.sec_edgar.scrape_company", return_value=data), \
         patch("app.scraper.runner._upsert_owns_sec", side_effect=lambda **kw: owns.append(kw)):
        result = run_scrape_sec_edgar("BlackRock")

    fink = [c for c in owns if c.get("stake_percent") == 0.34]
    assert len(fink) == 1                                  # insider with shares → OWNS
    assert fink[0]["ownership_type"] == "minority"
    assert all(c.get("stake_percent") != 0 for c in owns)  # the no-shares director got none
    assert any(r.get("role") == "insider owner" for r in result["scraped"])


# ── Person-centric insider ownership (Option A, in the runner) ───────────────

def test_sec_person_centric_insider_owns():
    from contextlib import contextmanager
    from app.scraper.runner import run_scrape_sec_edgar
    data = {"cik": "0001364742", "name": "BlackRock", "ownership_filings": [],
            "executives": [], "shares_outstanding": 100000.0}

    @contextmanager
    def fake_ctx():
        sess = MagicMock()
        # the known-people query returns a founder/CEO already in the graph
        sess.run.return_value = [{"id": "fink", "name": "Larry Fink"}]
        yield sess

    owns = []
    with patch("app.scraper.runner.get_source_enabled", return_value=True), \
         patch("app.scraper.runner._ensure_source", return_value="src"), \
         patch("app.scraper.runner._upsert_entity_by_name", return_value="blk"), \
         patch("app.scraper.runner.db.get_session", fake_ctx), \
         patch("app.scraper.sec_edgar.scrape_company", return_value=data), \
         patch("app.scraper.sec_edgar.fetch_insider_holding",
               return_value={"shares_owned": 2000, "stake_percent": 2.0,
                             "source_url": "https://sec.gov/f4", "source_date": "2024-02-01"}), \
         patch("app.scraper.runner._upsert_owns_sec", side_effect=lambda **kw: owns.append(kw)):
        run_scrape_sec_edgar("BlackRock")

    assert len(owns) == 1
    assert owns[0]["owner_id"] == "fink" and owns[0]["stake_percent"] == 2.0


# ── source_id stamping on entity nodes (provenance for the node panel) ─────────

class TestEntitySourceStamping:
    """Regression: a big owner (e.g. a government or fund) whose subsidiaries are
    excluded from its own source panel showed *no* source, because the scrapers
    never stamped Entity.source_id. These lock in that they now do."""

    def _create_call(self, session):
        return next((c for c in session.run.call_args_list
                     if "CREATE (e:Entity" in c.args[0]), None)

    def _update_calls(self, session):
        return [c for c in session.run.call_args_list
                if "COALESCE(e.source_id" in c.args[0]]

    def test_wikidata_upsert_stamps_source_id_on_create(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.runner.resolve_entity_id", return_value=None):
            _upsert_entity(name="Gov", entity_type="government", country=None,
                           founded=None, revenue=None, description=None,
                           wikidata_id="Q1", source_id="WD-SRC")
        create = self._create_call(session)
        assert create is not None
        assert "source_id: $source_id" in create.args[0]
        assert create.kwargs["source_id"] == "WD-SRC"

    def test_wikidata_upsert_only_fills_missing_source_on_update(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.runner.resolve_entity_id", return_value="eid-1"):
            _upsert_entity(name="Gov", entity_type="government", country=None,
                           founded=None, revenue=None, description=None,
                           wikidata_id="Q1", source_id="WD-SRC")
        updates = self._update_calls(session)
        assert updates and updates[0].kwargs["source_id"] == "WD-SRC"

    def test_by_name_stamps_source_id_on_create(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.graph_writer.resolve_entity_id", return_value=None):
            _upsert_entity_by_name(name="Vanguard Group Inc", cik="0000102909",
                                   source_id="SEC-SRC")
        create = self._create_call(session)
        assert create is not None
        assert "source_id: $source_id" in create.args[0]
        assert create.kwargs["source_id"] == "SEC-SRC"

    def test_by_name_fills_missing_source_on_update(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.graph_writer.resolve_entity_id", return_value="eid-2"):
            _upsert_entity_by_name(name="Vanguard Group Inc", cik="0000102909",
                                   source_id="SEC-SRC")
        updates = self._update_calls(session)
        assert updates and updates[0].kwargs["source_id"] == "SEC-SRC"


# ── Succession (SUCCEEDED_BY, from Wikidata P1366/P1365) ──────────────────────

class TestSuccession:
    def test_upsert_succession_creates_directed_edge(self):
        ctx, session = _make_session_mock()  # .single() → None: edge doesn't exist yet
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_succession("id:pred", "id:succ", "src-1", source_url="http://wd/Q1")
        create = next((c for c in session.run.call_args_list
                       if "CREATE (a)-[:SUCCEEDED_BY" in c.args[0]), None)
        assert create is not None
        assert create.kwargs["pid"] == "id:pred" and create.kwargs["sid"] == "id:succ"

    def test_upsert_succession_skips_self_loop(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_succession("id:x", "id:x", "src-1")
        session.run.assert_not_called()

    def test_scrape_node_wires_successor_and_predecessor_direction(self):
        # Twitter (Q1390577) replaced by X Corp (Q117617480), and replaces nothing;
        # X Corp replaces Twitter. Verify predecessor→successor direction both ways.
        data = {
            "qid": "Q1390577", "name": "Twitter", "description": None,
            "instances": ["Q4830453"], "country": None, "founded": None, "revenue": None,
            "successors":   [{"qid": "Q117617480", "name": "X Corp.", "date": "2023-04-00"}],
            "predecessors": [],
        }
        calls = []
        with patch("app.scraper.runner.fetch_company_data", return_value=data), \
             patch("app.scraper.runner._upsert_entity",
                   side_effect=lambda **kw: f"id:{kw['wikidata_id']}"), \
             patch("app.scraper.runner._upsert_succession",
                   side_effect=lambda p, s, *a, **k: calls.append((p, s, k.get("since")))):
            _scrape_node("Q1390577", depth=0, visited=set(), scraped=[], source_id="src-1")
        # predecessor (Twitter) → successor (X Corp.), carrying the succession date
        assert ("id:Q1390577", "id:Q117617480", "2023-04-00") in calls

    def test_scrape_node_predecessor_points_into_current_entity(self):
        data = {
            "qid": "Q117617480", "name": "X Corp.", "description": None,
            "instances": ["Q4830453"], "country": None, "founded": None, "revenue": None,
            "successors":   [],
            "predecessors": [{"qid": "Q1390577", "name": "Twitter"}],
        }
        calls = []
        with patch("app.scraper.runner.fetch_company_data", return_value=data), \
             patch("app.scraper.runner._upsert_entity",
                   side_effect=lambda **kw: f"id:{kw['wikidata_id']}"), \
             patch("app.scraper.runner._upsert_succession",
                   side_effect=lambda p, s, *a, **k: calls.append((p, s))):
            _scrape_node("Q117617480", depth=0, visited=set(), scraped=[], source_id="src-1")
        # predecessor (Twitter) → successor (X Corp., the scraped entity)
        assert ("id:Q1390577", "id:Q117617480") in calls


# ── SEC formerNames → aliases / search_text ───────────────────────────────────

class TestFormerNamesAliases:
    def test_merge_aliases_unions_dedupes_drops_current_name(self):
        assert _merge_aliases(["Old A"], ["Old B", "old a", "Meta"], "Meta") == ["Old A", "Old B"]
        assert _merge_aliases(None, None) == []
        assert _merge_aliases([" X "], [" x "]) == ["X"]     # trim + case-fold

    def test_create_sets_aliases_and_search_text(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.graph_writer.resolve_entity_id", return_value=None):
            _upsert_entity_by_name("Meta Platforms, Inc.", cik="1326801",
                                   source_id="sec", former_names=["Facebook Inc"])
        create = next(c for c in session.run.call_args_list if "CREATE (e:Entity" in c.args[0])
        assert create.kwargs["aliases"] == ["Facebook Inc"]
        assert "Facebook Inc" in create.kwargs["search_text"]
        assert "Meta Platforms, Inc." in create.kwargs["search_text"]

    def test_existing_merges_without_duplicating(self):
        ctx, session = _make_session_mock(single_returns=[
            {"name": "Meta Platforms, Inc.", "aliases": ["Facebook Inc"], "descr": None},
        ])
        with patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.graph_writer.resolve_entity_id", return_value="eid-1"):
            _upsert_entity_by_name("Meta Platforms, Inc.", cik="1326801", source_id="sec",
                                   former_names=["Facebook Inc", "TheFacebook, Inc."])
        setcall = next(c for c in session.run.call_args_list if "aliases" in c.kwargs)
        assert setcall.kwargs["aliases"] == ["Facebook Inc", "TheFacebook, Inc."]  # no dup

    def test_no_former_names_leaves_aliases_untouched(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx), \
             patch("app.scraper.graph_writer.resolve_entity_id", return_value="eid-1"):
            _upsert_entity_by_name("Acme Corp", cik="999", source_id="sec")
        assert not any("aliases" in c.kwargs for c in session.run.call_args_list)


# ── Wikidata → GLEIF identifier bridge ────────────────────────────────────────
#
# Wikidata's P1278 carries the LEI for many large companies, which is the only
# identifier a Wikidata scrape and a GLEIF node have in common. Without it the two
# stay separate nodes — the Microsoft split, where the ownership graph sat on one
# and the executives on the other.

class TestIdentifierBridge:
    def test_resolves_on_the_lei_so_a_gleif_node_is_reused(self):
        # First two lookups (wikidata_id, sec_cik) miss, the lei_id lookup hits the
        # existing GLEIF node — so no second copy of the company is created.
        ctx, session = _make_session_mock(
            single_returns=[None, None, {"id": "lei:INR2EJN1ERAN0W5ZP974"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            eid = _upsert_entity("Microsoft", "company", "US", 1975, None, None, "Q2283",
                                 lei="INR2EJN1ERAN0W5ZP974")
        assert eid == "lei:INR2EJN1ERAN0W5ZP974"

    def test_lei_lookup_is_attempted_when_a_lei_is_known(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_entity("Microsoft", "company", "US", 1975, None, None, "Q2283",
                           lei="INR2EJN1ERAN0W5ZP974")
        assert any("lei_id" in call.args[0] for call in session.run.call_args_list), \
            "no lookup keyed on lei_id — the bridge would never find the GLEIF node"

    def test_no_extra_lookup_when_no_identifiers_are_known(self):
        # resolve_entity_id skips falsy ids, so the common case keeps its 3 lookups.
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_entity("Acme", "company", "US", 2000, None, None, "Q1")
        assert session.run.call_count == 4          # 3 lookups + CREATE

    def test_update_never_overwrites_an_existing_identifier(self):
        # A register is authoritative for its own id and Wikidata is crowd-edited;
        # clobbering lei_id would re-point a merge key at the wrong company.
        ctx, session = _make_session_mock(single_returns=[{"id": "existing"}])
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_entity("Microsoft", "company", "US", 1975, None, None, "Q2283",
                           lei="INR2EJN1ERAN0W5ZP974", sec_cik="0000789019")
        update = [c for c in session.run.call_args_list if "SET e.wikidata_id" in c.args[0]]
        assert len(update) == 1
        cypher, params = update[0].args[0], update[0].kwargs
        assert "e.lei_id          = COALESCE(e.lei_id, $lei)" in cypher
        assert "e.sec_cik         = COALESCE(e.sec_cik, $sec_cik)" in cypher
        assert params["lei"] == "INR2EJN1ERAN0W5ZP974"
        assert params["sec_cik"] == "0000789019"

    def test_created_node_carries_the_identifiers(self):
        ctx, session = _make_session_mock()
        with patch("app.scraper.runner.db.get_session", ctx):
            _upsert_entity("Microsoft", "company", "US", 1975, None, None, "Q2283",
                           lei="INR2EJN1ERAN0W5ZP974", sec_cik="0000789019")
        create = [c for c in session.run.call_args_list if "CREATE (e:Entity" in c.args[0]]
        assert len(create) == 1
        assert "lei_id: $lei" in create[0].args[0]
        assert create[0].kwargs["lei"] == "INR2EJN1ERAN0W5ZP974"
