"""The Wikidata scrape reads by the id the node holds, against a real
ArcadeDB: the query resolves to its node through the resolver the SEC passes
use, and that node's QID or CIK picks the item — so EDGAR's spelling ("Wells
Fargo & Company/Mn"), which no label search finds, still reaches its item
(findings item 7, 2026-10-31). Only the network is mocked."""
import pytest
from unittest.mock import patch

from app.config import settings
from app.scraper import runner

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_WIKIDATA_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True), \
         patch("app.scraper.runner.pick_candidate", side_effect=lambda hits, kind: hits[0]["id"] if hits else None):
        yield


def _wells(it_db, wikidata_id=None):
    it_db.run_command(
        "CREATE (:Entity {id: 'sec:0000072971', name: 'Wells Fargo & Company/Mn', "
        "name_normalized: 'wells fargo', type: 'company', search_text: 'Wells Fargo & Company/Mn', "
        "sec_cik: '0000072971', wikidata_id: $q})", {"q": wikidata_id})


def _scrape(name, statements):
    with patch("app.scraper.runner.search_by_statement",
               side_effect=lambda prop, value: statements.get((prop, value), [])) as by_id, \
         patch("app.scraper.runner.search_entity", return_value=[]) as by_name, \
         patch("app.scraper.runner._scrape_node") as node:
        out = runner.run_scrape(name, depth=0)
    return out, by_id, by_name, node


def test_the_nodes_cik_finds_the_item(it_db):
    _wells(it_db)
    out, by_id, by_name, node = _scrape("Wells Fargo & Company/Mn",
                                        {("P5531", "0000072971"): [{"id": "Q744149"}]})
    assert out["wikidata_id"] == "Q744149"
    assert node.call_args.args[0] == "Q744149"
    by_name.assert_not_called()


def test_the_nodes_own_qid_needs_no_lookup(it_db):
    _wells(it_db, wikidata_id="Q744149")
    out, by_id, by_name, _ = _scrape("Wells Fargo & Company/Mn", {})
    assert out["wikidata_id"] == "Q744149"
    by_id.assert_not_called()
    by_name.assert_not_called()


def test_no_node_means_the_name_search(it_db):
    out, by_id, by_name, _ = _scrape("Some Unknown Holdings Llc", {})
    assert out["status"] == "no_results"
    by_id.assert_not_called()
    by_name.assert_called_once()
