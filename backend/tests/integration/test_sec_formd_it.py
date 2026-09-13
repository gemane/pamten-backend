"""Form D write path against a real ArcadeDB: record → runner → database →
read back. Pins the roles, the jurisdiction fill, the corporate-person skip
(the Berkshire rule) and the accession gate."""
import pytest
from unittest.mock import patch

from app.config import settings
from app.scraper import runner

pytestmark = pytest.mark.integration

DATA = {
    "persons": [
        {"name": "Elon Musk", "roles": ["Executive Officer", "Director"]},
        {"name": "Gwynne Shotwell", "roles": ["Executive Officer"]},
        {"name": "137 Holdings Alpha, Llc", "roles": ["Executive Officer"]},  # GP vehicle
    ],
    "jurisdiction": "DELAWARE", "entity_type": "Corporation",
    "offering_amount": 1000.0, "amount_sold": 1000.0,
    "accession": "0001181412-22-000003", "filing_date": "2022-08-05",
    "url": "https://www.sec.gov/Archives/edgar/data/1181412/000118141222000003/primary_doc.xml",
    "form": "D",
}


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield


def _issuer(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'sx', name: 'Space Exploration Technologies Corp.', "
        "name_normalized: 'space exploration technologies', "
        "search_text: 'Space Exploration Technologies Corp.', type: 'company', "
        "sec_cik: '1181412'})")


def test_roles_jurisdiction_and_the_corporate_skip(it_db):
    _issuer(it_db)
    with patch("app.scraper.sec_formd.latest_form_d", return_value=DATA):
        res = runner.run_sec_formd("Space Exploration Technologies Corp.")
    assert res["status"] == "ok"
    assert res["total"] == 3                    # Musk x2 roles + Shotwell x1
    assert res["corporate_skipped"] == 1        # the GP LLC stayed out
    rows = it_db.run_command(
        "MATCH (p:Person)-[r:HAS_ROLE]->(:Entity {id:'sx'}) "
        "RETURN p.full_name AS n, r.role AS role, r.source_date AS d ORDER BY n, r.role")
    got = [(x["n"], x["role"]) for x in rows]
    assert ("Elon Musk", "Director") in got and ("Elon Musk", "Executive Officer") in got
    assert ("Gwynne Shotwell", "Executive Officer") in got
    assert all(x["d"] == "2022-08-05" for x in rows), "the filing date is the honesty marker"
    assert it_db.run_command(
        "MATCH (p:Person) WHERE p.full_name CONTAINS 'Holdings' RETURN p") == [], \
        "no GP vehicle minted as a person"
    juris = it_db.run_command(
        "MATCH (e:Entity {id:'sx'}) RETURN e.country AS c, e.jurisdiction_code AS jc")[0]
    assert juris["c"] == "US" and juris["jc"] == "US-DE"


def test_the_accession_gate_and_force(it_db):
    _issuer(it_db)
    with patch("app.scraper.sec_formd.latest_form_d", return_value=DATA):
        first = runner.run_sec_formd("Space Exploration Technologies Corp.")
        second = runner.run_sec_formd("Space Exploration Technologies Corp.")
        forced = runner.run_sec_formd("Space Exploration Technologies Corp.", force=True)
    assert first["status"] == "ok"
    assert second["status"] == "fresh"
    assert forced["status"] == "ok"
    n = it_db.run_command(
        "MATCH ()-[r:HAS_ROLE]->(:Entity {id:'sx'}) RETURN count(r) AS n")[0]["n"]
    assert n == 3, "force re-run updates, never doubles roles"


def test_no_cik_asks_for_the_sec_scrape_first(it_db):
    it_db.run_command("CREATE (:Entity {id:'x', name:'Private GmbH', "
                      "name_normalized:'private', search_text:'Private GmbH', type:'company'})")
    res = runner.run_sec_formd("Private GmbH")
    assert res["status"] == "needs_sec_scrape"
