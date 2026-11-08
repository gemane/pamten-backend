"""Exhibit 21 of a filing several registrants file together, against a real
ArcadeDB: the co-registrant's branch is written, and what an earlier read
wrote from the group's list is dimmed."""
import pytest
from unittest.mock import patch

from app.config import settings
from app.scraper import runner

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.sec_edgar.fetch_filer_identity", return_value=(None, None)):   # no network
        yield


def test_a_co_registrant_keeps_its_branch_and_the_groups_list_it_was_given_dims(it_db):
    # NSTAR Electric files its 10-K together with Eversource: the exhibit is
    # Eversource's list. An earlier read gave NSTAR all of it — its sibling
    # Aquarion among them; the filer's part is Harbor Electric alone.
    it_db.run_command(
        "CREATE (:Entity {id: 'nstar', name: 'NSTAR Electric Company', name_normalized: 'nstar electric', "
        "search_text: 'NSTAR Electric Company', type: 'company', sec_cik: '0000013372'})")
    folder = "https://www.sec.gov/Archives/edgar/data/13372"
    older = {"subsidiaries": [{"name": "Harbor Electric Energy Company", "jurisdiction": "Massachusetts"},
                              {"name": "Aquarion Company", "jurisdiction": "Delaware"}],
             "form": "10-K", "filing_date": "2025-02-12", "url": f"{folder}/000007274125000010/ex21.htm"}
    newer = {"subsidiaries": [{"name": "Harbor Electric Energy Company", "jurisdiction": "Massachusetts",
                               "parent_basis": "indent"}],
             "form": "10-K", "filing_date": "2026-02-12", "url": f"{folder}/000007274126000010/ex21.htm",
             "group_of": "EVERSOURCE ENERGY"}
    for data in (older, newer):
        with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=data):
            result = runner.run_sec_ex21("NSTAR Electric")
    assert result["status"] == "ok" and result["group_of"] == "EVERSOURCE ENERGY"
    assert result["total"] == 1 and result["stale"] == 1
    got = {r["name"]: r.get("stale") for r in it_db.run_command(
        "MATCH (:Entity {id:'nstar'})-[r:OWNS]->(b:Entity) WHERE r.until IS NULL "
        "RETURN b.name AS name, r.stale AS stale")}
    assert got == {"Harbor Electric Energy Company": False, "Aquarion Company": True}
    note = it_db.run_command("MATCH (r:ScrapeRun {source: 'sec-ex21'}) RETURN r.error AS note "
                             "ORDER BY r.started_at DESC LIMIT 1")[0]["note"]
    assert "the filing's list is EVERSOURCE ENERGY's; 1 in this filer's branch" in note


def test_a_co_registrant_without_a_branch_writes_nothing_and_dims_what_it_was_given(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'etx', name: 'Entergy Texas, Inc.', name_normalized: 'entergy texas', "
        "search_text: 'Entergy Texas, Inc.', type: 'company', sec_cik: '0001427437'})")
    folder = "https://www.sec.gov/Archives/edgar/data/1427437"
    older = {"subsidiaries": [{"name": "Entergy Louisiana, LLC", "jurisdiction": "Texas"}],
             "form": "10-K", "filing_date": "2025-02-19", "url": f"{folder}/000006598425000174/ex21.htm"}
    newer = {"subsidiaries": [], "form": "10-K", "filing_date": "2026-02-19",
             "url": f"{folder}/000006598426000174/ex21.htm", "group_of": "ENTERGY CORP /DE/"}
    for data in (older, newer):
        with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=data):
            result = runner.run_sec_ex21("Entergy Texas")
    assert result["status"] == "ok" and result["total"] == 0 and result["stale"] == 1
    assert it_db.run_command("MATCH (e:Entity {id:'etx'}) RETURN e.sec_ex21_ingested AS u")[0]["u"] == newer["url"]


def test_a_partnership_with_its_own_list_reads_it_not_the_reits(it_db):
    # findings item 78: Kilroy's 10-K carries the REIT's list (21.1) and the
    # partnership's own (21.2); read from the partnership's CIK the first used
    # to win. Real exhibits, only the network mocked.
    from pathlib import Path
    fx = Path(__file__).parent.parent / "scraper" / "fixtures"
    acc = "0001628280-26-007051"
    it_db.run_command(
        "CREATE (:Entity {id: 'krlp', name: 'Kilroy Realty, L.P.', name_normalized: 'kilroy realty lp', "
        "search_text: 'Kilroy Realty, L.P.', type: 'company', sec_cik: '0001493976'})")
    base = "https://www.sec.gov/Archives/edgar/data/1493976/000162828026007051/"
    cands = [{"url": base + f, "form": "10-K", "filing_date": "2026-02-10", "accession": acc, "period": None}
             for f in ("kilroy_ex21_1.htm", "kilroy_ex21_2.htm")]
    # an earlier read wrote something only the REIT's list carries
    older = {"subsidiaries": [{"name": "Kilroy Realty Corporation Only Sub, LLC", "jurisdiction": "Delaware"}],
             "form": "10-K", "filing_date": "2025-02-10", "url": base.replace("26007051", "25007051") + "ex21.htm"}
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=older):
        runner.run_sec_ex21("Kilroy Realty, L.P.")
    with patch("app.scraper.sec_ex21.annual_filings", return_value=[("10-K", acc, "2026-02-10", "2025-12-31")]), \
         patch("app.scraper.sec_ex21.exhibit_candidates", return_value=cands), \
         patch("app.scraper.sec_ex21.co_registrants",
               return_value=[("1025996", "KILROY REALTY CORP"), ("1493976", "Kilroy Realty, L.P.")]), \
         patch("app.scraper.sec_ex21._get_text", side_effect=lambda u: (fx / u.rsplit("/", 1)[1]).read_text()):
        result = runner.run_sec_ex21("Kilroy Realty, L.P.")
    assert result["status"] == "ok" and result["total"] == 77 and "group_of" not in result
    rows = it_db.run_command(
        "MATCH (:Entity {id:'krlp'})-[r:OWNS]->(b:Entity) WHERE r.until IS NULL "
        "RETURN b.name AS name, r.stale AS stale, r.source_url AS url")
    current = {r["name"] for r in rows if not r["stale"]}
    assert len(current) == 77 and "Kilroy Realty, L.P." not in current
    assert all(r["url"].endswith("kilroy_ex21_2.htm") for r in rows if not r["stale"])
    assert {r["name"] for r in rows if r["stale"]} == {"Kilroy Realty Corporation Only Sub, LLC"}
