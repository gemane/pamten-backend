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
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
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
