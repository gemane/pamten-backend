"""A name has a time span, against a real ArcadeDB (2026-10-30).

After the 2026-10-29b rebuild "BlackRock" resolved to the pre-2024 filer, CIK
0001364742, which EDGAR has named "BlackRock Finance, Inc." since 2024-09-26:
its pre-2024 13G filings said "BlackRock Inc.", it kept that and "BlackRock,
Inc." as names, and Wikidata's Q219635 — which still states its CIK and LEI —
landed on it and broke the tie. The Exhibit 21 run read the old filer's 2024
list and never the new parent's. Here the chain runs through the writers:
the filer is written under EDGAR's name with the dated former names, the item
is refused as stale, and the lookup answers with the company that bears the
name now. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

NEW_LEI, OLD_LEI = "529900VBK42Y5HHRMD23", "549300LRIF3NWCU26A80"
OLD_CIK, NEW_CIK = "0001364742", "0002012383"
SUBMISSIONS = {
    OLD_CIK: {"name": "BlackRock Finance, Inc.", "stateOfIncorporation": "DE", "formerNames": [
        {"name": "New BlackRock, Inc.", "to": "2006-09-29T00:00:00.000Z"},
        {"name": "BlackRock, Inc.", "to": "2006-10-05T00:00:00.000Z"},
        {"name": "BlackRock Inc.", "to": "2024-09-26T00:00:00.000Z"}]},
    NEW_CIK: {"name": "BlackRock, Inc.", "stateOfIncorporation": "DE", "formerNames": [
        {"name": "BlackRock Funding, Inc. /DE", "to": "2024-10-01T00:00:00.000Z"}]},
}


def _edgar():
    return (patch("app.scraper.sec_edgar._submissions", side_effect=lambda cik: SUBMISSIONS[cik]),
            patch("app.scraper.sec_edgar.fetch_filer_name", side_effect=lambda cik: SUBMISSIONS[cik]["name"]))


def _seed_new_parent(it_db):
    it_db.run_command("CREATE (:Entity {id: $id, name: 'BlackRock, Inc.', name_normalized: 'blackrock', "
                      "type: 'company', search_text: 'BlackRock, Inc.', name_credibility: 92, "
                      "lei_id: $lei, country: 'US', jurisdiction_code: 'US-DE'})",
                      {"id": f"lei:{NEW_LEI}", "lei": NEW_LEI})


def _old_filer_from_an_old_13g():
    from app.scraper.graph_writer import _upsert_entity_by_name
    a, b = _edgar()
    with a, b:
        return _upsert_entity_by_name(name="BlackRock Inc.", cik=OLD_CIK, source_id="sec-edgar")


def _node(nid):
    rows = run_sql("SELECT name, name_normalized, sec_cik, wikidata_id, aliases, former_names FROM Entity "
                   "WHERE id = :id", {"id": nid})
    return dict(rows[0]) if rows else None


def test_an_old_filings_spelling_writes_the_filer_under_the_name_in_force(it_db):
    _seed_new_parent(it_db)
    nid = _old_filer_from_an_old_13g()
    assert nid == f"sec:{OLD_CIK}"
    old = _node(nid)
    assert old["name"] == "BlackRock Finance, Inc." and old["name_normalized"] == "blackrock finance"
    assert "BlackRock Inc.|2024-09-26" in old["former_names"]
    assert _node(f"lei:{NEW_LEI}")["sec_cik"] is None


def test_the_item_naming_the_old_filer_by_an_ended_name_is_refused(it_db):
    from app.scraper.runner import _upsert_entity
    _seed_new_parent(it_db)
    _old_filer_from_an_old_13g()
    eid = _upsert_entity("BlackRock", None, None, None, "Q219635", aliases=["BlackRock, Inc.", "BLK"],
                         lei=[OLD_LEI], sec_cik=[OLD_CIK])
    assert eid is None
    assert run_sql("SELECT count(*) AS n FROM Entity WHERE wikidata_id IS NOT NULL")[0]["n"] == 0
    assert "BLK" not in (_node(f"sec:{OLD_CIK}")["aliases"] or [])


def test_the_lookup_answers_with_the_company_that_bears_the_name_now(it_db):
    from app.routers.search import resolve_best_entity
    from app.scraper.graph_writer import _upsert_entity_by_name
    _seed_new_parent(it_db)
    _old_filer_from_an_old_13g()
    a, b = _edgar()
    with a, b:   # the new parent's own filings: EDGAR's name, on GLEIF's node
        assert _upsert_entity_by_name(name="BlackRock, Inc.", cik=NEW_CIK, source_id="sec-edgar",
                                      register_name=True) == f"lei:{NEW_LEI}"
    for q in ("BlackRock", "BlackRock, Inc.", "BlackRock Inc."):
        assert resolve_best_entity(q)["id"] == f"lei:{NEW_LEI}", q
    assert resolve_best_entity("BlackRock Finance")["id"] == f"sec:{OLD_CIK}"
