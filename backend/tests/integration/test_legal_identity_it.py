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
NV_LEI, NV_CIK = "549300WSWTEW6DURBO45", "0000741508"
ABI_LEI, ABI_CIK = "5493008H3828EMEXB082", "0001668717"
SB_LEI, SB_CIK = "5493003BZYYYCDIO0R13", "0001065521"
FILERS = {"0000071691": Filer("0000071691", "NEW YORK TIMES CO", home="US-NY",
                              street="620 EIGHTH AVENUE", city="NEW YORK"),
          "0001286681": Filer("0001286681", "DOMINOS PIZZA INC", home="US-DE"),
          "0001293451": Filer("0001293451", "Tencent Holdings Ltd", home="KY"),
          NV_CIK: Filer(NV_CIK, "NV ENERGY, INC.", home="US-NV", street="6226 WEST SAHARA AVENUE", city="LAS VEGAS"),
          "0001385849": Filer("0001385849", "ENERGY FUELS INC", home="US-DE"),
          ABI_CIK: Filer(ABI_CIK, "Anheuser-Busch InBev SA/NV", home="BE"),
          # EDGAR states no incorporation for SoftBank, and it files no annual report
          SB_CIK: Filer(SB_CIK, "SOFTBANK GROUP CORP.", home=None)}
ADDRESSES = {"0000071691": ("620 EIGHTH AVENUE", "NEW YORK", None),
             NV_CIK: ("6226 WEST SAHARA AVENUE", "LAS VEGAS", "today's address: no filing within a year"),
             SB_CIK: ("1-7-1 KAIGAN MINATO-KU", "TOKYO", None)}


def _record(lei, name, jurisdiction, street, city, status="ISSUED", level="FULLY_CORROBORATED",
            updated="2026-04-07T00:00:00Z", other=(), form=None, hq=None):
    addr = {"addressLines": [street], "city": city, "country": jurisdiction[:2], "postalCode": "1"}
    head = {**addr, **hq} if hq else addr
    return golden_copy_record({
        "lei": lei,
        "entity": {"legalName": {"name": name}, "jurisdiction": jurisdiction, "status": "ACTIVE",
                   "legalForm": {"id": form},
                   "legalAddress": addr, "headquartersAddress": head,
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
    NV_LEI: _record(NV_LEI, "NV ENERGY, INC.", "US-NV", "6226 West Sahara Avenue", "Las Vegas", status="LAPSED",
                    updated="2023-09-27T00:00:00Z"),
    ABI_LEI: _record(ABI_LEI, "ANHEUSER-BUSCH INBEV", "BE", "Grand-Place 1", "Brussels", form="R85P"),
    # GLEIF on 2026-11-01: the headquarters in Japanese script, the legal address in Latin
    SB_LEI: _record(SB_LEI, "Softbank Group Corp", "JP", "Tokyo Portcity Takeshiba, 1-7-1 Kaigan, Minato-Ku",
                    "Tokyo", hq={"addressLines": ["海岸一丁目７番１号"], "city": "東京都 港区",
                                 "postalCode": "105-7537"}),
}
LLC_PARENT = [{"lei": INC_LEI, "names": ["Domino's Pizza, Inc."], "place": "US-DE", "status": "ACTIVE",
               "periods": [{"type": "RELATIONSHIP_PERIOD", "startDate": "1999-10-27T00:00:00Z"}]}]


@pytest.fixture(autouse=True)
def _registers():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.register_pointer.fetch_gleif_record", side_effect=RECORDS.get), \
         patch("app.scraper.legal_identity.filer_facts", side_effect=FILERS.get), \
         patch("app.scraper.legal_identity.filing_address_at", side_effect=lambda cik, day: ADDRESSES.get(cik)), \
         patch("app.scraper.legal_identity.gleif_parents", side_effect=lambda lei: LLC_PARENT if lei == LLC_LEI else []), \
         patch("app.scraper.legal_identity.gleif_children", return_value=[]), \
         patch("app.scraper.legal_identity.filer_held_at", return_value=[]), \
         patch("app.scraper.sec_edgar.fetch_filer_name", side_effect=lambda cik: FILERS[cik].name):
        yield


def _gleif(lei):
    from app.scraper.register_pointer import write_from_gleif
    return write_from_gleif(lei)


def _sec(name, cik, country="US"):
    from app.scraper.graph_writer import _upsert_entity_by_name
    return _upsert_entity_by_name(name=name, entity_type="company", cik=cik, source_id="sec",
                                  register_name=True, country=country)


def _nodes(it_db, where, params):
    return [dict(r) for r in it_db.run_sql(
        f"SELECT id, name, lei_id, sec_cik, lei_source, gleif_last_update, hq_street FROM Entity WHERE {where}",
        params)]


def test_nyt_sec_first_then_gleif_is_one_node(it_db):
    sec_id = _sec("NEW YORK TIMES CO", "0000071691")
    assert sec_id == "sec:0000071691"
    # the filer takes the LEI and, with it, the LEI's id — the one the GLEIF
    # importers address it by; the CIK's id forwards (findings item 20)
    assert _gleif(NYT_LEI) == f"lei:{NYT_LEI}"
    [row] = _nodes(it_db, "lei_id = :l OR sec_cik = :c", {"l": NYT_LEI, "c": "0000071691"})
    assert row["id"] == f"lei:{NYT_LEI}"
    assert it_db.run_sql("SELECT new_id FROM MergedId WHERE old_id = :o", {"o": sec_id})[0]["new_id"] == row["id"]
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


def test_the_bridge_joins_softbank_on_its_registered_building(it_db):
    # findings item 31: the place is only JP (EDGAR states none), so the record is
    # weak; the Japanese-script headquarters cannot compare, the Latin legal address
    # with the building's name in front is EDGAR's filing address
    from app.scraper import maintenance
    with patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", False):
        _gleif(SB_LEI)
    it_db.run_command("CREATE (:Entity {id: 'sec:0001065521', name: 'SOFTBANK GROUP CORP.', "
                      "name_normalized: 'softbank group', search_text: 'SOFTBANK GROUP CORP.', type: 'company', "
                      "sec_cik: '0001065521', name_credibility: 98})")
    with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
        out = maintenance.deduplicate_entities(limit=None)
    [bridged] = out["lei_bridged"]
    assert bridged["evidence"].startswith("name+country+address@")
    [row] = _nodes(it_db, "lei_id = :l OR sec_cik = :c", {"l": SB_LEI, "c": SB_CIK})
    assert row["lei_id"] == SB_LEI and row["sec_cik"] == SB_CIK


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


#: EDGAR's submissions for NV Energy, as `write_from_edgar` reads them (2026-10-30)
NV_SUBMISSIONS = {"name": "NV ENERGY, INC.", "stateOfIncorporation": "NV", "formerNames": [],
                  "addresses": {"business": {"street1": "6226 WEST SAHARA AVENUE", "city": "LAS VEGAS",
                                             "stateOrCountry": "NV", "zipCode": "89146"}}}


def test_a_cik_wikidata_states_lands_on_the_gleif_node(it_db):
    """Findings item 5: NV Energy's item Q1961658 states LEI and CIK; the
    filer left EDGAR in 2014, so no name search finds the CIK — the item's
    pointer is the only way in. The GLEIF node takes it, no second node."""
    from app.scraper.runner import _upsert_entity
    assert _gleif(NV_LEI) == f"lei:{NV_LEI}"
    with patch("app.scraper.sec_edgar._submissions", return_value=NV_SUBMISSIONS):
        got = _upsert_entity("NV Energy", None, None, None, "Q1961658", source_id="wikidata",
                             lei=NV_LEI, sec_cik="741508")
    assert got == f"lei:{NV_LEI}"
    [row] = _nodes(it_db, "lei_id = :l OR sec_cik = :c", {"l": NV_LEI, "c": NV_CIK})
    assert row["id"] == f"lei:{NV_LEI}" and row["sec_cik"] == NV_CIK
    assert row["lei_source"].startswith("name+place+address@2023-09-27")


def test_a_stated_cik_the_registers_refuse_writes_nothing(it_db):
    from app.scraper.runner import _upsert_entity
    _gleif(LLC_LEI)                                                # Domino's Pizza LLC, US-MI
    with patch("app.scraper.sec_edgar._submissions") as edgar:
        _upsert_entity("Domino's Pizza LLC", None, None, None, "Q1", source_id="wikidata",
                       lei=LLC_LEI, sec_cik="1286681")             # the Inc.'s CIK: name and place differ
    edgar.assert_not_called()
    assert _nodes(it_db, "sec_cik = :c", {"c": "0001286681"}) == []
    [row] = _nodes(it_db, "lei_id = :l", {"l": LLC_LEI})
    assert row["sec_cik"] is None


def test_nv_energy_and_energy_fuels_stay_two_companies(it_db):
    # the name key dropped "NV" anywhere: "energy", whose prefix "energy fuels" is
    _gleif(NV_LEI)
    fuels = _sec("ENERGY FUELS INC", "0001385849")
    assert fuels == "sec:0001385849"
    rows = {r["id"]: r for r in _nodes(it_db, "lei_id = :l OR sec_cik IS NOT NULL", {"l": NV_LEI})}
    assert rows[f"lei:{NV_LEI}"]["sec_cik"] is None
    [key] = [r["name_normalized"] for r in it_db.run_sql("SELECT name_normalized FROM Entity WHERE id = :i",
                                                         {"i": f"lei:{NV_LEI}"})]
    assert key == "nv energy"


@pytest.mark.parametrize("sec_first", [True, False])
def test_ab_inbev_joins_on_the_form_its_gleif_code_names(it_db, sec_first):
    """Findings item 19: GLEIF's "ANHEUSER-BUSCH INBEV" (ELF R85P: NV, SA, AG)
    is EDGAR's "Anheuser-Busch InBev SA/NV"; the code is read off the stored
    node as well as off the record."""
    if sec_first:
        _sec("Anheuser-Busch InBev SA/NV", ABI_CIK, country="BE")
    _gleif(ABI_LEI)
    if not sec_first:
        _sec("Anheuser-Busch InBev SA/NV", ABI_CIK, country="BE")
    [row] = _nodes(it_db, "lei_id = :l OR sec_cik = :c", {"l": ABI_LEI, "c": ABI_CIK})
    assert row["lei_id"] == ABI_LEI and row["sec_cik"] == ABI_CIK
    assert row["lei_source"] == "name(form R85P)+place"
