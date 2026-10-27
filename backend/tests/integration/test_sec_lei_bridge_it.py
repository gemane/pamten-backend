"""The LEI the filer's own filings confirm, against a real ArcadeDB: the SEC
scrape lands on the GLEIF / Companies House node instead of minting a twin
keyed by the CIK, and the entity dedup folds a twin that already exists —
Unilever's gb-coh:00041424 (LEI, country NL) and EDGAR's UNILEVER PLC
(CIK 0000217410, GB) after the 2026-10-26 rebuild. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

from app.config import settings
from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

LEI = "549300MKFYEKVRWML317"
CIK = "0000217410"
URL = "https://www.sec.gov/Archives/edgar/data/217410/000021741023000039/a991-directorpdmrshareho.htm"


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True):
        yield


def _companies_house_node(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'gb-coh:00041424', name: 'Unilever Plc', name_normalized: 'unilever', "
        "type: 'company', country: 'NL', lei_id: $lei, register_id: 'RA000585:00041424', "
        "search_text: 'Unilever Plc', name_credibility: 90})", {"lei": LEI})


def _edgar_node(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'sec:0000217410', name: 'UNILEVER PLC', name_normalized: 'unilever', "
        "type: 'company', country: 'GB', sec_cik: $cik, search_text: 'UNILEVER PLC', "
        "name_credibility: 98})", {"cik": CIK})


def _unilever():
    return [dict(r) for r in run_sql("SELECT id, sec_cik, lei_id, lei_source_url, country FROM Entity "
                                     "WHERE name_normalized = 'unilever'")]


class TestTheUpsertResolvesByLei:
    def test_the_lei_finds_the_node_whatever_its_name_and_country_say(self, it_db):
        from app.scraper.graph_writer import _upsert_entity_by_name
        _companies_house_node(it_db)
        got = _upsert_entity_by_name("UNILEVER PLC", cik=CIK, lei=LEI, country="GB")
        assert got == "gb-coh:00041424"
        assert _unilever() == [{"id": "gb-coh:00041424", "sec_cik": CIK, "lei_id": LEI,
                                "lei_source_url": None, "country": "NL"}]

    def test_without_the_lei_the_country_keeps_them_apart(self, it_db):
        """The control: what the rebuild did — a second node keyed by the CIK."""
        from app.scraper.graph_writer import _upsert_entity_by_name
        _companies_house_node(it_db)
        assert _upsert_entity_by_name("UNILEVER PLC", cik=CIK, country="GB") == "sec:0000217410"
        assert len(_unilever()) == 2


def _scrape(confirmed):
    from app.scraper import runner, sec_edgar
    payload = {"name": "UNILEVER PLC", "cik": CIK, "lei": None, "former_names": [],
               "ownership_filings": [], "holdings": [], "executives": []}
    with patch.object(sec_edgar, "scrape_company", return_value=payload), \
         patch.object(sec_edgar, "fetch_filer_country", return_value="GB"), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_sic", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch("app.scraper.sec_lei.confirm_filer_lei",
               return_value={"source_url": URL} if confirmed else None) as confirm:
        runner.run_scrape_sec_edgar("Unilever")
    return confirm


class TestTheScrape:
    def test_a_confirmed_lei_puts_the_cik_on_the_node(self, it_db):
        _companies_house_node(it_db)
        confirm = _scrape(confirmed=True)
        confirm.assert_called_once_with(CIK, LEI, ["UNILEVER PLC", "Unilever Plc"])
        assert _unilever() == [{"id": "gb-coh:00041424", "sec_cik": CIK, "lei_id": LEI,
                                "lei_source_url": URL, "country": "NL"}]

    def test_not_confirmed_leaves_two_nodes_for_review(self, it_db):
        _companies_house_node(it_db)
        _scrape(confirmed=False)
        assert sorted(n["id"] for n in _unilever()) == ["gb-coh:00041424", "sec:0000217410"]


class TestTheDedup:
    def test_the_scoped_dedup_folds_an_existing_twin(self, it_db):
        from app.scraper.maintenance import deduplicate_entities_for
        _companies_house_node(it_db)
        _edgar_node(it_db)
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value={"source_url": URL}):
            out = deduplicate_entities_for(["sec:0000217410"])
        assert out["entities_merged"] == 1
        # the higher credibility survives, with both ids and the filing
        assert _unilever() == [{"id": "sec:0000217410", "sec_cik": CIK, "lei_id": LEI,
                                "lei_source_url": URL, "country": "GB"}]
        assert run_sql("SELECT FROM MergedId WHERE old_id = 'gb-coh:00041424'")

    def test_the_full_dedup_folds_it_too(self, it_db):
        from app.scraper.maintenance import deduplicate_entities
        _companies_house_node(it_db)
        _edgar_node(it_db)
        with patch("app.scraper.sec_lei.confirm_filer_lei", return_value={"source_url": URL}) as confirm:
            out = deduplicate_entities(limit=None)
        confirm.assert_called_once()
        assert out["entities_merged"] == 1 and len(out["lei_bridged"]) == 1
        assert [n["id"] for n in _unilever()] == ["sec:0000217410"]

    def test_a_pair_kept_apart_stays_apart(self, it_db):
        from app.scraper.maintenance import deduplicate_entities
        _companies_house_node(it_db)
        _edgar_node(it_db)
        it_db.run_command("MATCH (a:Entity {id:'gb-coh:00041424'}), (b:Entity {id:'sec:0000217410'}) "
                          "CREATE (a)-[:NOT_DUPLICATE]->(b)")
        with patch("app.scraper.sec_lei.confirm_filer_lei") as confirm:
            deduplicate_entities(limit=None)
        confirm.assert_not_called()
        assert len(_unilever()) == 2
