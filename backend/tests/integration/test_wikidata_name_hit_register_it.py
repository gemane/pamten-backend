"""A Wikidata item found a node by NAME that holds another register id than
the item states, against a real ArcadeDB (findings item 50).

Sirius XM's item says it is owned by Q1516562 "Liberty Media", which states
LEI 549300ZKUTPIBZLWLL89 (the Nevada company, CIK 0001560385) and no CIK. In
the 2026-11-03 rebuild a Berkshire SC 13G/A had written the Delaware filer
"LIBERTY MEDIA CORPORATION" (CIK 0000869614) an hour before; the item took it
by name, and GLEIF, refusing to join its record to that filer, wrote its own
node without the item. The register is now asked first, with the name's node
as the candidate: it joins the node when it is the same legal entity, else
its own node takes the item. Only GLEIF's API and EDGAR's facts are mocked."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql
from app.scraper.legal_identity import Filer
from app.scraper.register_pointer import golden_copy_record

pytestmark = pytest.mark.integration

LEI = "549300ZKUTPIBZLWLL89"
RECORD = {"lei": LEI, "entity": {
    "legalName": {"name": "LIBERTY MEDIA CORPORATION", "language": "en"},
    "otherNames": [], "transliteratedOtherNames": [],
    "legalAddress": {"addressLines": ["112 North Curry Street"], "city": "Carson City", "country": "US",
                     "region": "US-NV"},
    "headquartersAddress": {"addressLines": ["12300 Liberty Boulevard"], "city": "Englewood", "country": "US",
                            "region": "US-CO"},
    "jurisdiction": "US-NV", "status": "ACTIVE", "legalForm": {"id": "8WM4", "other": None},
    "eventGroups": []},
    "registration": {"status": "ISSUED", "initialRegistrationDate": "2015-06-20T00:00:00Z",
                     "corroborationLevel": "FULLY_CORROBORATED"}}

FILERS = {
    "0000869614": Filer(cik="0000869614", name="LIBERTY MEDIA CORPORATION", home="US-DE"),
    "0001560385": Filer(cik="0001560385", name="Liberty Media Corp", home="US-NV"),
}


@pytest.fixture(autouse=True)
def _registers():
    with patch("app.scraper.register_pointer._gleif_enabled", return_value=True), \
         patch("app.scraper.register_pointer.fetch_gleif_record",
               side_effect=lambda lei: golden_copy_record(RECORD) if lei == LEI else None), \
         patch("app.scraper.maintenance._sec_lei_check_enabled", return_value=True), \
         patch("app.scraper.legal_identity.filer_facts", side_effect=FILERS.get), \
         patch("app.scraper.legal_identity.gleif_parents", return_value=[]):
        yield


def _filer(it_db, cik, name):
    it_db.run_command(f"CREATE (e:Entity {{id:'sec:{cik}', name:'{name}', sec_cik:'{cik}', type:'company', "
                      "name_normalized:'liberty media', search_text:'" + name + "', "
                      "source_id:'sec-edgar', name_credibility:98})")


def _owner_item():
    """As the owners loop writes a related company (`register_required`)."""
    from app.scraper.runner import _upsert_entity
    return _upsert_entity("Liberty Media", None, None, None, "Q1516562", source_id="wikidata",
                          lei=[LEI], register_required=True)


def _qid_holders():
    return [r["id"] for r in run_sql("SELECT id FROM Entity WHERE wikidata_id = 'Q1516562'")]


def test_a_namesake_of_another_legal_entity_does_not_take_the_item(it_db):
    _filer(it_db, "0000869614", "LIBERTY MEDIA CORPORATION")
    assert _owner_item() == f"lei:{LEI}"
    assert _qid_holders() == [f"lei:{LEI}"]
    de = run_sql("SELECT lei_id, wikidata_id, sec_cik FROM Entity WHERE id = 'sec:0000869614'")
    assert [dict(r) for r in de] == [{"lei_id": None, "wikidata_id": None, "sec_cik": "0000869614"}]


def test_the_same_legal_entity_found_by_name_is_joined_and_takes_the_item(it_db):
    """NYT's case (2026-10-29b): the filer IS the GLEIF record's company — one
    node, re-keyed `lei:`, with the item."""
    _filer(it_db, "0001560385", "Liberty Media Corp")
    assert _owner_item() == f"lei:{LEI}"
    assert _qid_holders() == [f"lei:{LEI}"]
    rows = [dict(r) for r in run_sql("SELECT id, sec_cik FROM Entity WHERE name_normalized = 'liberty media'")]
    assert rows == [{"id": f"lei:{LEI}", "sec_cik": "0001560385"}]


def test_the_order_of_the_writes_no_longer_decides(it_db):
    """The item before the namesake gives the same graph as after it."""
    assert _owner_item() == f"lei:{LEI}"
    _filer(it_db, "0000869614", "LIBERTY MEDIA CORPORATION")
    assert _owner_item() == f"lei:{LEI}"
    assert _qid_holders() == [f"lei:{LEI}"]


def test_the_name_hit_stands_when_no_register_knows_the_ids(it_db):
    _filer(it_db, "0000869614", "LIBERTY MEDIA CORPORATION")
    with patch("app.scraper.register_pointer.fetch_gleif_record", return_value=None):
        assert _owner_item() == "sec:0000869614"
    assert _qid_holders() == ["sec:0000869614"]
