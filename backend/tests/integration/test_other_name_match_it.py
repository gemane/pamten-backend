"""A name-only row meets the node that bears its name otherwise than as its
`name`, against a real ArcadeDB and its full-text index (findings item 62).

GLEIF names Tencent 騰訊控股有限公司 with the transliteration "Tencent Holdings
Limited" as an other name; EDGAR conforms T. Rowe Price Associates surname
first, "PRICE T ROWE ASSOCIATES INC". The SEC holder write of the one and the
Exhibit 21 row of the other minted uuid twins beside those nodes in the
2026-11-05 rebuild. A node's aliases are no door, and a namesake registered
in another country is a company of its own. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql
from app.scraper.legal_identity import Filer

pytestmark = pytest.mark.integration

TENCENT_LEI, TENCENT_CIK = "254900N4SLUMW4XUYY11", "0001293451"


def _node(it_db, nid, name, norm, search, country, **props):
    extra = "".join(f", {k}: ${k}" for k in props)
    it_db.run_command(f"CREATE (:Entity {{id: $id, name: $name, name_normalized: $norm, search_text: $search, "
                      f"country: $country, type: 'company', name_credibility: 92{extra}}})",
                      {"id": nid, "name": name, "norm": norm, "search": search, "country": country, **props})


def _upsert(name, country, cik=None, **kw):
    from app.scraper.graph_writer import _upsert_entity_by_name
    return _upsert_entity_by_name(name=name, cik=cik, source_id="sec-edgar", country=country, **kw)


def _count():
    return run_sql("SELECT count(*) AS n FROM Entity")[0]["n"]


def test_the_sec_filer_lands_on_the_gleif_node_named_in_another_script(it_db):
    _node(it_db, f"lei:{TENCENT_LEI}", "騰訊控股有限公司", "騰訊控股有限公司", "騰訊控股有限公司 Tencent Holdings Limited",
          "KY", lei_id=TENCENT_LEI, other_names=["Tencent Holdings Limited"], legal_form_code="4XP8",
          gleif_registration_status="ISSUED", validation_sources="FULLY_CORROBORATED")
    filer = Filer(cik=TENCENT_CIK, name="Tencent Holdings Ltd", home="KY")
    with patch("app.scraper.legal_identity.filer_facts", return_value=filer), \
         patch("app.scraper.legal_identity.gleif_parents", return_value=[]), \
         patch("app.scraper.sec_edgar.fetch_filer_name", return_value="Tencent Holdings Ltd"):
        got = _upsert("Tencent Holdings Ltd", "KY", cik=TENCENT_CIK, register_name=True)
    assert got == f"lei:{TENCENT_LEI}"
    [row] = run_sql("SELECT sec_cik, lei_source FROM Entity WHERE id = :id", {"id": got})
    assert row["sec_cik"] == TENCENT_CIK and row["lei_source"] == "name+place"
    assert _count() == 1


def test_the_exhibit_21_row_lands_on_edgars_surname_first_filer(it_db):
    _node(it_db, "sec:0000080255", "PRICE T ROWE ASSOCIATES INC /MD/", "price t rowe associates",
          "PRICE T ROWE ASSOCIATES INC /MD/ T. Rowe Price Associates, Inc.", "US", sec_cik="0000080255",
          aliases=["T. Rowe Price Associates, Inc."])
    _node(it_db, "sec:0001113169", "PRICE T ROWE GROUP INC", "price t rowe group",
          "PRICE T ROWE GROUP INC T. Rowe Price Associates, Inc. T. Rowe Price Group, Inc.", "US",
          sec_cik="0001113169", aliases=["T. Rowe Price Associates, Inc.", "T. Rowe Price Group, Inc."])
    assert _upsert("T. Rowe Price Associates, Inc.", "US") == "sec:0000080255"
    assert _count() == 2


def test_a_registers_other_name_with_the_form_kept(it_db):
    _node(it_db, "lei:549300JTZWECUOC8CW13", "애플코리아 유한회사", "애플코리아 유한회사",
          "애플코리아 유한회사 APPLE KOREA LIMITED", "KR", lei_id="549300JTZWECUOC8CW13",
          other_names=["APPLE KOREA LIMITED"])
    _node(it_db, "lei:5493004NRK1XLI2A3K89", "Apple Japan合同会社", "apple japan合同会社",
          "Apple Japan合同会社 Apple Japan LLC", "JP", lei_id="5493004NRK1XLI2A3K89", other_names=["Apple Japan LLC"])
    assert _upsert("Apple Korea Limited", "KR") == "lei:549300JTZWECUOC8CW13"
    # the row says Inc., the register LLC: another legal form, its own node
    assert _upsert("Apple Japan, Inc.", "JP").count("-") == 4
    assert _count() == 3


def test_aliases_and_namesakes_abroad_are_no_door(it_db):
    _node(it_db, "sec:0000810332", "REPUBLIC AIRWAYS HOLDINGS INC.", "republic airways holdings",
          "REPUBLIC AIRWAYS HOLDINGS INC. MESA AIR GROUP INC MESA AIRLINES INC", "US", sec_cik="0000810332",
          aliases=["MESA AIR GROUP INC", "MESA AIRLINES INC"])
    _node(it_db, "lei:529900J1WEMMIW7BOH57", "NEW YORK TIMES CO", "new york times",
          "NEW YORK TIMES CO New York Times Co.", "US", lei_id="529900J1WEMMIW7BOH57",
          other_names=["New York Times Co."])
    mesa = _upsert("Mesa Airlines, Inc.", "US")              # the subsidiary, not its parent
    nyt = _upsert("New York Times Limited", "GB")            # the UK company, not the US one
    assert mesa.count("-") == 4 and nyt.count("-") == 4
    assert _count() == 4
