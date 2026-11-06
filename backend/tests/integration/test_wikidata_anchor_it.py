"""The item found for the company a query names enriches that company's node,
against a real ArcadeDB (findings item 60).

Shentel's Q23073960 states no register id and is labelled "Shentel"; the
query was EDGAR's "SHENANDOAH TELECOMMUNICATIONS CO" and the SEC filer node
of that name exists (the registers run first). The item's alias is the
filer's legal name, so the item lands on the filer instead of a `wd:` twin.
An item bearing none of the node's names, or a node that already carries an
item, still gets its own node. Only Wikidata's search is mocked."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql
from app.scraper import runner

pytestmark = pytest.mark.integration

FILER = "sec:0000354963"
ITEM = dict(name="Shentel", founded=None, revenue=None, description="US telecom", wikidata_id="Q23073960",
            aliases=["Shenandoah Telecommunications Company", "nTelos"], source_id="wikidata")


def _filer(it_db, qid=None):
    it_db.run_command("CREATE (:Entity {id: $id, name: 'SHENANDOAH TELECOMMUNICATIONS CO/VA/', "
                      "name_normalized: 'shenandoah telecommunications', search_text: 'SHENANDOAH TELECOMMUNICATIONS CO/VA/', "
                      "type: 'company', sec_cik: '0000354963', country: 'US', name_credibility: 98"
                      + (", wikidata_id: $qid" if qid else "") + "})", {"id": FILER, "qid": qid})


def _nodes():
    return {r["id"]: dict(r) for r in run_sql("SELECT id, wikidata_id, sec_cik, name FROM Entity")}


def test_the_item_enriches_the_node_the_query_named(it_db):
    _filer(it_db)
    got = runner._upsert_entity(**ITEM, anchor=FILER)
    assert got == FILER
    nodes = _nodes()
    assert list(nodes) == [FILER] and nodes[FILER]["wikidata_id"] == "Q23073960"
    assert nodes[FILER]["name"] == "SHENANDOAH TELECOMMUNICATIONS CO/VA/"      # the register's name stays


def test_an_item_bearing_none_of_the_nodes_names_gets_its_own_node(it_db):
    _filer(it_db)
    got = runner._upsert_entity(**{**ITEM, "aliases": ["nTelos"]}, anchor=FILER)
    assert got == "wd:Q23073960"
    assert _nodes()[FILER]["wikidata_id"] is None


def test_a_node_that_carries_an_item_is_no_anchor(it_db):
    _filer(it_db, qid="Q1")
    assert runner._upsert_entity(**ITEM, anchor=FILER) == "wd:Q23073960"


def test_run_scrape_hands_the_querys_node_to_the_searched_item(it_db):
    _filer(it_db)
    data = {"name": "Shentel", "instances": ["Q4830453"], "aliases": ITEM["aliases"], "description": "US telecom",
            "subsidiaries": [], "owners": [], "executives": []}
    with patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch("app.scraper.runner.get_source_enabled", return_value=True), \
         patch("app.scraper.runner.search_entity", return_value=[{"id": "Q23073960"}]), \
         patch("app.scraper.runner.pick_candidate", return_value="Q23073960"), \
         patch("app.scraper.runner.fetch_company_data", return_value=data), \
         patch("app.scraper.runner._stored_wikidata_item", return_value=None):
        res = runner.run_scrape("SHENANDOAH TELECOMMUNICATIONS CO", depth=0)
    assert res["status"] == "ok" and res["entity_id"] == FILER
    nodes = _nodes()
    assert list(nodes) == [FILER] and nodes[FILER]["wikidata_id"] == "Q23073960"
