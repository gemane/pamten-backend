"""A name alone does not carry a CIK onto a node, against a real ArcadeDB
(2026-10-29 rebuild). BlackRock's pre-2024 13G filings say "BlackRock Inc."
under CIK 0001364742 — the old parent, which EDGAR now names "BlackRock
Finance, Inc." — and that spelling found GLEIF's node of the 2024 parent;
"Tencent Music Entertainment Group" found Wikidata's "Tencent" by its first
word. The write keys on EDGAR's current name for the CIK. Only EDGAR is
mocked."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

NEW_LEI = "529900VBK42Y5HHRMD23"
OLD_CIK, NEW_CIK = "0001364742", "0002012383"
EDGAR = {OLD_CIK: "BlackRock Finance, Inc.", NEW_CIK: "BlackRock, Inc.",
         "0001744676": "Tencent Music Entertainment Group", "0000021344": "COCA COLA CO"}


def _node(it_db, nid, name, norm, **ids):
    extra = "".join(f", {k}: ${k}" for k in ids)
    it_db.run_command(f"CREATE (:Entity {{id: $id, name: $name, name_normalized: $norm, type: 'company', "
                      f"search_text: $name, name_credibility: 90{extra}}})",
                      {"id": nid, "name": name, "norm": norm, **ids})


def _upsert(name, cik, edgar=EDGAR, **kw):
    from app.scraper.graph_writer import _upsert_entity_by_name
    with patch("app.scraper.sec_edgar.fetch_filer_name", side_effect=edgar.get) as fetch:
        return _upsert_entity_by_name(name=name, cik=cik, source_id="sec-edgar", **kw), fetch


def _ids(nid):
    [row] = run_sql("SELECT sec_cik, lei_id, name FROM Entity WHERE id = :id", {"id": nid})
    return dict(row)


def test_an_old_filings_spelling_does_not_carry_the_old_cik_onto_the_new_parent(it_db):
    _node(it_db, f"lei:{NEW_LEI}", "BlackRock, Inc.", "blackrock", lei_id=NEW_LEI, country="US")
    old, fetch = _upsert("BlackRock Inc.", OLD_CIK)
    assert old == f"sec:{OLD_CIK}" and fetch.call_count == 1
    assert _ids(f"lei:{NEW_LEI}")["sec_cik"] is None
    # the new parent's own filer write, with EDGAR's name, lands on GLEIF's node
    new, fetch = _upsert("BlackRock, Inc.", NEW_CIK, register_name=True)
    assert new == f"lei:{NEW_LEI}" and not fetch.called          # the caller holds EDGAR's name
    assert _ids(new)["sec_cik"] == NEW_CIK
    assert run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 2


def test_a_name_prefix_does_not_carry_another_companys_cik(it_db):
    _node(it_db, "wd:Q860580", "Tencent", "tencent", wikidata_id="Q860580")
    nid, _ = _upsert("Tencent Music Entertainment Group", "0001744676")
    assert nid == "sec:0001744676"
    assert _ids("wd:Q860580")["sec_cik"] is None


def test_the_same_name_in_edgars_spelling_takes_the_cik(it_db):
    _node(it_db, "lei:UWJKFUJFZ02DKWI3RY53", "The Coca-Cola Company", "the coca-cola",
          lei_id="UWJKFUJFZ02DKWI3RY53")
    nid, _ = _upsert("The Coca-Cola Company", "0000021344")
    assert nid == "lei:UWJKFUJFZ02DKWI3RY53" and _ids(nid)["sec_cik"] == "0000021344"


def test_a_node_with_another_cik_keeps_it(it_db):
    _node(it_db, f"lei:{NEW_LEI}", "BlackRock, Inc.", "blackrock", lei_id=NEW_LEI, sec_cik=OLD_CIK)
    nid, fetch = _upsert("BlackRock, Inc.", NEW_CIK, register_name=True)
    assert nid == f"sec:{NEW_CIK}" and not fetch.called
    assert _ids(f"lei:{NEW_LEI}")["sec_cik"] == OLD_CIK


def test_when_edgar_cannot_say_the_name_match_stands(it_db):
    _node(it_db, f"lei:{NEW_LEI}", "BlackRock, Inc.", "blackrock", lei_id=NEW_LEI)
    nid, _ = _upsert("BlackRock Inc.", OLD_CIK, edgar={})
    assert nid == f"lei:{NEW_LEI}" and _ids(nid)["sec_cik"] == OLD_CIK
