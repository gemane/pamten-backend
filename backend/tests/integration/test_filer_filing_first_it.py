"""The SEC writer meets a GLEIF node by a filer's name: the filer's own filing
decides before the registers' comparison — against a real ArcadeDB (findings
item 64).

Capital Ventures International's 13G write (CIK 0001011712) found its GLEIF
node lei:PIRK3IP22W3DKV63YG19 by name. The registers' comparison calls the
record weak (a P.O. box the filer's EDGAR address omits) and refused, so the
write minted sec:0001011712 beside it, and the dedup bridge merged the pair by
the filer's N-PX twelve seconds later (2026-11-05 and 11-06 rebuilds). The
writer now asks that filing first, as the bridge and the pointer do, and keeps
it as the join's evidence. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql
from app.scraper.legal_identity import Filer

pytestmark = pytest.mark.integration

LEI, CIK = "PIRK3IP22W3DKV63YG19", "0001011712"
NODE = f"lei:{LEI}"
FILING = {"cik": CIK, "lei": LEI, "form": "N-PX", "file_date": "2024-08-30",
          "source_url": "https://www.sec.gov/Archives/edgar/data/1011712/000101171224000004/primary_doc.xml"}
FILER = Filer(cik=CIK, name="CAPITAL VENTURES INTERNATIONAL", home=None,
              street="WINDWARD 1 REGATTA OFFICE PARK, WEST BAY ROAD", city="GRAND CAYMAN")


@pytest.fixture(autouse=True)
def _edgar():
    # the registers' comparison, as it stands for this pair: a weak record, no address match
    with patch("app.scraper.legal_identity.filer_facts", return_value=FILER), \
         patch("app.scraper.legal_identity.gleif_parents", return_value=[]), \
         patch("app.scraper.legal_identity.Evidence.address_at", return_value=None), \
         patch("app.scraper.legal_identity.Evidence.held_at", return_value=[]), \
         patch("app.scraper.graph_writer.settings.SCRAPER_GEOCODE_ENABLED", False):
        yield


def _gleif_node(it_db):
    it_db.run_command(
        f"CREATE (e:Entity {{id:'{NODE}', name:'CAPITAL VENTURES INTERNATIONAL', lei_id:'{LEI}', type:'company', "
        "name_normalized:'capital ventures international', search_text:'CAPITAL VENTURES INTERNATIONAL', "
        "country:'KY', legal_form_code:'OSBR', gleif_registration_status:'ISSUED', "
        "validation_sources:'FULLY_CORROBORATED', "
        "hq_street:'P. O. Box 897, Windward 1, Regatta Office Park, West Bay Road, Grand Cayman', "
        "hq_city:'George Town', source_id:'gleif', name_credibility:92})")


def _write_filer_by_name():
    from app.scraper import runner
    from app.scraper.graph_writer import _upsert_entity_by_name
    source_id = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL,
                                      runner.SEC_EDGAR_CREDIBILITY)
    return _upsert_entity_by_name(name="CAPITAL VENTURES INTERNATIONAL", entity_type="company", cik=CIK,
                                  source_id=source_id, register_name=True, country="KY")


def _nodes():
    return [dict(r) for r in run_sql(
        "SELECT id, sec_cik, lei_id, lei_source, lei_source_url FROM Entity "
        "WHERE lei_id = :lei OR sec_cik = :cik ORDER BY id", {"lei": LEI, "cik": CIK})]


def test_the_filers_filing_joins_the_write_to_the_gleif_node(it_db):
    _gleif_node(it_db)
    with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=FILING) as filed:
        got = _write_filer_by_name()
    assert got == NODE
    filed.assert_called_once_with(CIK, LEI, ["CAPITAL VENTURES INTERNATIONAL"])
    assert _nodes() == [{"id": NODE, "sec_cik": CIK, "lei_id": LEI, "lei_source": "filing",
                         "lei_source_url": FILING["source_url"]}]      # one node, the filing as evidence


def test_without_a_filing_the_registers_decide_and_the_weak_record_is_refused(it_db):
    """The twin the bridge merged hours later in the rebuilds — the rule's
    honest answer when no filing states the LEI."""
    _gleif_node(it_db)
    with patch("app.scraper.sec_lei.confirm_filer_lei", return_value=None):
        got = _write_filer_by_name()
    assert got == f"sec:{CIK}"
    assert [n["id"] for n in _nodes()] == [NODE, f"sec:{CIK}"]
    assert _nodes()[0]["sec_cik"] is None                                # the GLEIF node untouched
