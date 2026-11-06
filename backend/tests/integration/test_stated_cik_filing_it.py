"""A Wikidata item on a GLEIF node states a CIK; the filer's own filing states
the LEI — against a real ArcadeDB (findings item 63).

Capital Ventures International's Q104626435 states LEI PIRK3IP22W3DKV63YG19
and CIK 0001011712. GLEIF's record names a P.O. box the filer's EDGAR address
omits, so the registers' comparison calls the record weak and refuses, while
the filer's N-PX of 2024 states the LEI. `attach_stated_cik` asks that filing
first and writes the filer onto the node, keeping the filing as the source.
Only EDGAR is mocked: the full-text reader and the submissions document."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql
from app.scraper import register_pointer as rp
from app.scraper.legal_identity import Filer

pytestmark = pytest.mark.integration

LEI, CIK = "PIRK3IP22W3DKV63YG19", "0001011712"
NODE = f"lei:{LEI}"
FILING = {"cik": CIK, "lei": LEI, "form": "N-PX", "file_date": "2024-08-30",
          "source_url": f"https://www.sec.gov/Archives/edgar/data/1011712/000101171224000004/primary_doc.xml"}
SUBMISSIONS = {"cik": "1011712", "name": "CAPITAL VENTURES INTERNATIONAL", "stateOfIncorporation": "E9",
               "formerNames": [], "website": None,
               "addresses": {"business": {"street1": "WINDWARD 1 REGATTA OFFICE PARK, WEST BAY ROAD", "street2": None,
                                          "city": "GRAND CAYMAN", "stateOrCountry": "E9", "zipCode": "KY1-1103"}},
               "filings": {"recent": {"form": ["N-PX", "SC 13G/A"], "filingDate": ["2024-08-30", "2024-02-14"],
                                      "accessionNumber": ["0001011712-24-000004", "0001011712-24-000001"]}}}
FILER = Filer(cik=CIK, name="CAPITAL VENTURES INTERNATIONAL", home=None,
              street="WINDWARD 1 REGATTA OFFICE PARK, WEST BAY ROAD", city="GRAND CAYMAN")


@pytest.fixture(autouse=True)
def _edgar():
    with patch("app.scraper.register_pointer._sec_enabled", return_value=True), \
         patch("app.scraper.sec_edgar._submissions", return_value=SUBMISSIONS), \
         patch("app.scraper.legal_identity.filer_facts", return_value=FILER), \
         patch("app.scraper.legal_identity.gleif_parents", return_value=[]), \
         patch("app.scraper.legal_identity.Evidence.address_at", return_value=None), \
         patch("app.scraper.legal_identity.Evidence.held_at", return_value=[]):
        yield


def _gleif_node(it_db):
    it_db.run_command(
        f"CREATE (e:Entity {{id:'{NODE}', name:'CAPITAL VENTURES INTERNATIONAL', lei_id:'{LEI}', type:'company', "
        "name_normalized:'capital ventures international', search_text:'CAPITAL VENTURES INTERNATIONAL', "
        "country:'KY', legal_form_code:'OSBR', gleif_registration_status:'ISSUED', "
        "validation_sources:'FULLY_CORROBORATED', "
        "hq_street:'P. O. Box 897, Windward 1, Regatta Office Park, West Bay Road, Grand Cayman', "
        "hq_city:'George Town', source_id:'gleif', name_credibility:92})")


def _node():
    rows = run_sql("SELECT id, sec_cik, lei_id, lei_source, lei_source_url FROM Entity WHERE lei_id = :lei",
                   {"lei": LEI})
    return [dict(r) for r in rows]


def test_the_filers_filing_joins_what_the_registers_call_weak(it_db):
    _gleif_node(it_db)
    with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=FILING) as filed:
        got = rp.attach_stated_cik({"lei_id": LEI, "sec_cik": None, "name": "CAPITAL VENTURES INTERNATIONAL"},
                                   NODE, [CIK])
    assert got == NODE
    filed.assert_called_once_with(CIK, LEI, ["CAPITAL VENTURES INTERNATIONAL"])
    assert _node() == [{"id": NODE, "sec_cik": CIK, "lei_id": LEI, "lei_source": "filing",
                        "lei_source_url": FILING["source_url"]}]
    # one node: EDGAR's filer was written onto the GLEIF record, not beside it
    assert run_sql("SELECT count(*) AS n FROM Entity WHERE sec_cik = :cik", {"cik": CIK})[0]["n"] == 1


def test_without_a_filing_the_registers_decide_and_refuse_the_weak_record(it_db):
    _gleif_node(it_db)
    with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
        got = rp.attach_stated_cik({"lei_id": LEI, "sec_cik": None, "name": "CAPITAL VENTURES INTERNATIONAL"},
                                   NODE, [CIK])
    assert got is None
    assert _node() == [{"id": NODE, "sec_cik": None, "lei_id": LEI, "lei_source": None, "lei_source_url": None}]
