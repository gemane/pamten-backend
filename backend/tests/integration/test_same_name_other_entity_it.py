"""A Wikidata item that shares its name with a node of another legal entity,
against a real ArcadeDB (findings item 30). JPMorgan Chase's subsidiaries on
Wikidata include J.P. Morgan Securities LLC (LEI ZBUT…) and J P Morgan
Securities PLC (LEI K6Q0…); both names key "jp morgan securities". The
first was written by GLEIF from its LEI, the second found that node by name,
its LEI disagreed, and nothing was written. A name-only hit whose register id
is not the item's is another company of that name: the item's LEI goes to
GLEIF. BlackRock's item, whose LEI is the old parent's (now BlackRock
Finance, Inc.), is still refused — by GLEIF's own former name. Only GLEIF's
API is mocked."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql
from app.scraper.register_pointer import golden_copy_record

pytestmark = pytest.mark.integration

LLC, PLC, BLK_OLD = "ZBUT11V806EZRVTWT807", "K6Q0W1PS1L1O4IQL9C32", "549300LRIF3NWCU26A80"


def _attrs(lei, name, country, jurisdiction, events=()):
    return {"lei": lei, "entity": {
        "legalName": {"name": name, "language": "en"}, "otherNames": [], "transliteratedOtherNames": [],
        "legalAddress": {"addressLines": ["1 Street"], "city": "City", "country": country},
        "headquartersAddress": {"addressLines": ["1 Street"], "city": "City", "country": country},
        "jurisdiction": jurisdiction, "status": "ACTIVE", "legalForm": {"id": None, "other": None},
        "eventGroups": [{"events": list(events)}]},
        "registration": {"status": "ISSUED", "initialRegistrationDate": "2012-06-06T00:00:00Z"}}


RECORDS = {
    LLC: _attrs(LLC, "J.P. MORGAN SECURITIES LLC", "US", "US-DE"),
    PLC: _attrs(PLC, "J.P. MORGAN SECURITIES PLC", "GB", "GB"),
    # GLEIF's record of 2026-11-01: renamed from BLACKROCK, INC. on 2024-09-30
    BLK_OLD: {**_attrs(BLK_OLD, "BlackRock Finance, Inc.", "US", "US-DE", events=[
        {"type": "CHANGE_LEGAL_NAME", "status": "COMPLETED", "effectiveDate": "2024-09-30T22:00:00Z"}])},
}
RECORDS[BLK_OLD]["entity"]["otherNames"] = [{"name": "BLACKROCK, INC.", "type": "PREVIOUS_LEGAL_NAME"}]


@pytest.fixture(autouse=True)
def _gleif():
    with patch("app.scraper.register_pointer._gleif_enabled", return_value=True), \
         patch("app.scraper.register_pointer.fetch_gleif_record",
               side_effect=lambda lei: golden_copy_record(RECORDS[lei]) if lei in RECORDS else None), \
         patch("app.scraper.maintenance._sec_lei_check_enabled", return_value=False):
        yield


def _item(name, qid, lei):
    from app.scraper.runner import _upsert_entity
    return _upsert_entity(name, None, None, "", qid, source_id="wikidata", lei=lei)


def _by_lei(lei):
    return [dict(r) for r in run_sql("SELECT id, name, wikidata_id FROM Entity WHERE lei_id = :l", {"l": lei})]


@pytest.mark.parametrize("plc_first", [True, False])
def test_two_companies_of_one_name_are_both_written(it_db, plc_first):
    items = [("J P Morgan Securities PLC", "Q113466019", PLC),
             ("J.P. Morgan Securities LLC", "Q113466790", LLC)]
    for name, qid, lei in (items if plc_first else items[::-1]):
        assert _item(name, qid, lei) == f"lei:{lei}"
    assert _by_lei(PLC) == [{"id": f"lei:{PLC}", "name": "J.P. MORGAN SECURITIES PLC", "wikidata_id": "Q113466019"}]
    assert _by_lei(LLC) == [{"id": f"lei:{LLC}", "name": "J.P. MORGAN SECURITIES LLC", "wikidata_id": "Q113466790"}]


def test_an_item_whose_lei_names_the_old_parent_is_still_refused(it_db):
    it_db.run_command("CREATE (e:Entity {id:'lei:529900VBK42Y5HHRMD23', name:'BlackRock, Inc.', "
                      "lei_id:'529900VBK42Y5HHRMD23', type:'company', name_normalized:'blackrock', "
                      "search_text:'BlackRock, Inc.', name_credibility:92})")
    assert _item("BlackRock", "Q219635", BLK_OLD) is None
    assert run_sql("SELECT id FROM Entity WHERE wikidata_id = 'Q219635'") == []
    assert [r["wikidata_id"] for r in _by_lei("529900VBK42Y5HHRMD23")] == [None]


def test_a_contradiction_by_id_still_writes_nothing(it_db):
    """Only a NAME hit is set aside: a node the item's own QID found, whose LEI
    is another, is refused as before."""
    it_db.run_command(f"CREATE (e:Entity {{id:'lei:{LLC}', name:'J.P. MORGAN SECURITIES LLC', lei_id:'{LLC}', "
                      "wikidata_id:'Q113466019', type:'company', name_normalized:'jp morgan securities'})")
    assert _item("J P Morgan Securities PLC", "Q113466019", PLC) is None
    assert _by_lei(PLC) == []


def test_a_name_hit_without_register_ids_is_still_used(it_db):
    """A node of that name that holds no LEI or CIK contradicts nothing: the
    item enriches it, as before."""
    it_db.run_command("CREATE (e:Entity {id:'u-jpms', name:'J.P. Morgan Securities LLC', type:'company', "
                      "name_normalized:'jp morgan securities', source_id:'sec-edgar'})")
    assert _item("J.P. Morgan Securities LLC", "Q113466790", LLC) == "u-jpms"
    assert [r["wikidata_id"] for r in run_sql("SELECT wikidata_id FROM Entity WHERE id = 'u-jpms'")] == ["Q113466790"]
