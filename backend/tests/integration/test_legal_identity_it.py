"""A GLEIF record and an SEC filer of one legal entity become one node in
either write order (`legal_identity`, findings item 4), against a real
ArcadeDB. The registers are mocked with the facts they gave on 2026-10-30."""
from unittest.mock import patch

import pytest

from app.config import settings
from app.scraper.legal_identity import Filer
from app.scraper.register_pointer import golden_copy_record

pytestmark = pytest.mark.integration

NYT_LEI, INC_LEI, LLC_LEI, TENCENT_LEI = ("529900J1WEMMIW7BOH57", "25490005ZWM1IF9UXU57",
                                         "254900BIRZMWFO44TO04", "254900N4SLUMW4XUYY11")
FILERS = {"0000071691": Filer("0000071691", "NEW YORK TIMES CO", home="US-NY",
                              street="620 EIGHTH AVENUE", city="NEW YORK"),
          "0001286681": Filer("0001286681", "DOMINOS PIZZA INC", home="US-DE"),
          "0001293451": Filer("0001293451", "Tencent Holdings Ltd", home="KY")}


def _record(lei, name, jurisdiction, street, city, status="ISSUED", level="FULLY_CORROBORATED",
            updated="2026-04-07T00:00:00Z", other=()):
    addr = {"addressLines": [street], "city": city, "country": jurisdiction[:2], "postalCode": "1"}
    return golden_copy_record({
        "lei": lei,
        "entity": {"legalName": {"name": name}, "jurisdiction": jurisdiction, "status": "ACTIVE",
                   "legalAddress": addr, "headquartersAddress": addr,
                   "otherNames": [{"name": n, "type": "TRANSLITERATED"} for n in other]},
        "registration": {"status": status, "corroborationLevel": level, "lastUpdateDate": updated,
                         "initialRegistrationDate": "2019-01-02T12:46:26Z"}})


RECORDS = {
    NYT_LEI: _record(NYT_LEI, "New York Times Co.", "US", "620 Eighth Avenue", "New York", status="LAPSED",
                     level="ENTITY_SUPPLIED_ONLY", updated="2021-07-16T05:04:45Z"),
    INC_LEI: _record(INC_LEI, "Domino's Pizza, Inc.", "US-DE", "30 Frank Lloyd Wright Drive", "Ann Arbor"),
    LLC_LEI: _record(LLC_LEI, "Domino's Pizza LLC", "US-MI", "30 Frank Lloyd Wright Drive", "Ann Arbor"),
    TENCENT_LEI: _record(TENCENT_LEI, "騰訊控股有限公司", "KY", "Kejizhongyi Avenue", "Shenzhen",
                         other=["Tencent Holdings Limited"]),
}
LLC_PARENT = [{"lei": INC_LEI, "names": ["Domino's Pizza, Inc."], "place": "US-DE", "status": "ACTIVE",
               "periods": [{"type": "RELATIONSHIP_PERIOD", "startDate": "1999-10-27T00:00:00Z"}]}]


@pytest.fixture(autouse=True)
def _registers():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.register_pointer.fetch_gleif_record", side_effect=RECORDS.get), \
         patch("app.scraper.legal_identity.filer_facts", side_effect=FILERS.get), \
         patch("app.scraper.legal_identity.filing_address_at",
               side_effect=lambda cik, day: ("620 EIGHTH AVENUE", "NEW YORK", None) if cik == "0000071691" else None), \
         patch("app.scraper.legal_identity.gleif_parents", side_effect=lambda lei: LLC_PARENT if lei == LLC_LEI else []), \
         patch("app.scraper.legal_identity.gleif_children", return_value=[]), \
         patch("app.scraper.legal_identity.filer_held_at", return_value=[]), \
         patch("app.scraper.sec_edgar.fetch_filer_name", side_effect=lambda cik: FILERS[cik].name):
        yield


def _gleif(lei):
    from app.scraper.register_pointer import write_from_gleif
    return write_from_gleif(lei)


def _sec(name, cik):
    from app.scraper.graph_writer import _upsert_entity_by_name
    return _upsert_entity_by_name(name=name, entity_type="company", cik=cik, source_id="sec",
                                  register_name=True, country="US")


def _nodes(it_db, where, params):
    return [dict(r) for r in it_db.run_sql(
        f"SELECT id, name, lei_id, sec_cik, lei_source, gleif_last_update, hq_street FROM Entity WHERE {where}",
        params)]


def test_nyt_sec_first_then_gleif_is_one_node(it_db):
    sec_id = _sec("NEW YORK TIMES CO", "0000071691")
    assert _gleif(NYT_LEI) == sec_id
    [row] = _nodes(it_db, "lei_id = :l OR sec_cik = :c", {"l": NYT_LEI, "c": "0000071691"})
    assert row["id"] == sec_id == "sec:0000071691"
    assert row["name"] == "NEW YORK TIMES CO"                       # the filer's own name stays
    assert row["lei_source"] == "name+country+address@2021-07-16"
    assert row["gleif_last_update"] == "2021-07-16" and row["hq_street"] == "620 Eighth Avenue"


def test_nyt_gleif_first_then_sec_is_one_node(it_db):
    lei_id = _gleif(NYT_LEI)
    assert lei_id == f"lei:{NYT_LEI}"
    assert _sec("NEW YORK TIMES CO", "0000071691") == lei_id
    [row] = _nodes(it_db, "lei_id = :l OR sec_cik = :c", {"l": NYT_LEI, "c": "0000071691"})
    assert row["sec_cik"] == "0000071691" and row["lei_source"] == "name+country+address@2021-07-16"


@pytest.mark.parametrize("sec_first", [True, False])
def test_dominos_cik_lands_on_the_inc_only(it_db, sec_first):
    if sec_first:
        _sec("DOMINOS PIZZA INC", "0001286681")
    _gleif(LLC_LEI)
    _gleif(INC_LEI)
    if not sec_first:
        _sec("DOMINOS PIZZA INC", "0001286681")
    rows = {r["lei_id"]: r for r in _nodes(it_db, "name_normalized = 'dominos pizza'", {})}
    assert set(rows) == {INC_LEI, LLC_LEI}
    assert rows[INC_LEI]["sec_cik"] == "0001286681" and rows[INC_LEI]["lei_source"] == "name+place"
    assert rows[LLC_LEI]["sec_cik"] is None


def test_the_dedup_bridge_joins_a_pair_written_apart(it_db):
    from app.scraper import maintenance
    with patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False):
        _gleif(NYT_LEI)                                            # minted: SEC scraping was off
    it_db.run_command("CREATE (:Entity {id: 'sec:0000071691', name: 'NEW YORK TIMES CO', "
                      "name_normalized: 'new york times', search_text: 'NEW YORK TIMES CO', type: 'company', "
                      "sec_cik: '0000071691', country: 'US', name_credibility: 98})")
    with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
        out = maintenance.deduplicate_entities(limit=None)
    assert [b["evidence"] for b in out["lei_bridged"]] == ["name+country+address@2021-07-16"]
    [row] = _nodes(it_db, "lei_id = :l OR sec_cik = :c", {"l": NYT_LEI, "c": "0000071691"})
    assert row["lei_id"] == NYT_LEI and row["sec_cik"] == "0000071691"


def test_the_bridge_finds_tencent_by_its_transliterated_name(it_db):
    from app.scraper import maintenance
    with patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False):
        _gleif(TENCENT_LEI)
    it_db.run_command("CREATE (:Entity {id: 'sec:0001293451', name: 'Tencent Holdings Ltd', "
                      "name_normalized: 'tencent holdings', search_text: 'Tencent Holdings Ltd', "
                      "type: 'company', sec_cik: '0001293451', country: 'KY', name_credibility: 98})")
    with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
        joined = maintenance.bridge_sec_lei_pairs(set())
    assert [(j["cik_id"], j["lei_id"], j["evidence"]) for j in joined] == [
        ("sec:0001293451", TENCENT_LEI, "name+place")]
