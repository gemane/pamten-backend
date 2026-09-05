"""Wikidata node hygiene against a real database: an id-less related company
is skipped (no orphan minted), an id-bearing one is created and can hard-merge,
and an already-existing one gets the edge without a duplicate."""
import pytest
from unittest.mock import patch

from app.scraper import runner

pytestmark = pytest.mark.integration


def _company_data(subsidiaries):
    return {"qid": "Q_PARENT", "name": "Parent Corp", "description": None,
            "instances": ["Q4830453"], "country": None, "founded": None,
            "revenue": None, "subsidiaries": subsidiaries,
            "owners": [], "successors": [], "predecessors": [],
            "ceos": [], "officers": []}


def _scrape(it_db, data):
    src = runner._ensure_source(runner.WIKIDATA_SOURCE_NAME,
                                runner.WIKIDATA_SOURCE_URL, 80, "knowledge_base")
    counts: dict = {}
    with patch("app.scraper.runner.fetch_company_data", return_value=data):
        runner._scrape_node("Q_PARENT", depth=1, visited=set(), scraped=[],
                            source_id=src, counts=counts)
    return counts


def test_an_idless_subsidiary_mints_no_node_and_draws_no_edge(it_db):
    counts = _scrape(it_db, _company_data(
        [{"qid": "Q_SUB_NOID", "name": "Ghost Sub Ltd", "instances": []}]))
    assert counts.get("skipped_unidentified") == 1
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity "
                         "WHERE name = 'Ghost Sub Ltd'")[0]["n"] == 0
    # the parent exists, but it has no OWNS edge out
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0


def test_an_lei_bearing_subsidiary_is_created_and_can_hard_merge(it_db):
    lei = "SUB0000000000000TEST"
    _scrape(it_db, _company_data(
        [{"qid": "Q_SUB_LEI", "name": "Real Sub AG", "instances": [], "lei": lei}]))
    rows = it_db.run_sql("SELECT lei_id FROM Entity WHERE name = 'Real Sub AG'")
    assert rows and rows[0]["lei_id"] == lei, "created, carrying its LEI for later merge"
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1


def test_an_existing_subsidiary_gets_the_edge_without_being_recreated(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'existing-sub', name: 'Known Sub SA', "
        "name_normalized: 'known sub sa', search_text: 'Known Sub SA', "
        "type: 'company', wikidata_id: 'Q_SUB_KNOWN'})")
    _scrape(it_db, _company_data(
        [{"qid": "Q_SUB_KNOWN", "name": "Known Sub SA", "instances": []}]))
    subs = it_db.run_sql("SELECT id FROM Entity WHERE wikidata_id = 'Q_SUB_KNOWN'")
    assert [dict(r)["id"] for r in subs] == ["existing-sub"], "no duplicate minted"
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1
