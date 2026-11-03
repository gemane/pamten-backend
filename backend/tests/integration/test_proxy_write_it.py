"""The proxy writer against a real ArcadeDB (findings item 46): a DEF 14A claim
beside the pair's 13D claim, the figure on the edge, the owner's name untouched,
and a name-only call that finds the company. Only EDGAR is mocked."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

URL = "https://www.sec.gov/Archives/edgar/data/1652044/000130817926000342/goog-20260424.htm"
PROXY = {"filing_date": "2026-04-24", "filing_url": URL, "share_class_structure": "multi_class",
         "owners": [{"name": "Sergey Brin", "voting_power_pct": 25.3, "below_one_percent": False},
                    {"name": "Sundar Pichai", "voting_power_pct": None, "below_one_percent": True}]}


@pytest.fixture
def seeded(it_db):
    from app.claims import KIND_OWNS, record_claim
    it_db.run_command("CREATE (:Entity {id:'goog', name:'Alphabet Inc.', type:'company', "
                      "name_normalized:'alphabet', sec_cik:'0001652044'})")
    it_db.run_command("CREATE (:Person {id:'sb', full_name:'Brin Sergey', name:'Brin Sergey'})")
    it_db.run_command("MATCH (p:Person {id:'sb'}),(c:Entity {id:'goog'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 6.0, source_id:'sec', filing_type:'13D/A'}]->(c)")
    record_claim(kind=KIND_OWNS, from_id="sb", to_id="goog", source_id="sec",
                 stake_percent=6.0, filing_type="13D/A")
    return it_db


def _write(entity_id="goog"):
    from app.scraper.proxy_write import write_proxy_ownership
    with patch("app.scraper.proxy_statement.fetch_proxy_ownership", return_value=PROXY) as fetch:
        out = write_proxy_ownership("Alphabet Inc.", entity_id)
    return out, fetch


def test_a_def14a_claim_beside_the_13d_claim(seeded):
    out, _ = _write()
    assert [u["proxy_name"] for u in out["updated"]] == ["Sergey Brin"]
    claims = seeded.run_sql("SELECT filing_type, voting_power_pct, stake_percent, read_from, source_url, "
                            "source_date FROM Claim WHERE from_id = 'sb' AND to_id = 'goog' ORDER BY filing_type")
    assert [c["filing_type"] for c in claims] == ["13D/A", "DEF 14A"]
    proxy_claim = claims[1]
    assert (proxy_claim["voting_power_pct"], proxy_claim["stake_percent"], proxy_claim["read_from"],
            proxy_claim["source_url"], proxy_claim["source_date"]) == (25.3, None, "table", URL, "2026-04-24")
    assert claims[0]["stake_percent"] == 6.0          # the 13D claim is untouched


def test_the_edge_carries_the_figure_and_the_name_stays(seeded):
    _write()
    edge = seeded.run_command("MATCH (:Person {id:'sb'})-[r:OWNS]->(:Entity {id:'goog'}) "
                              "RETURN r.voting_power_pct AS v, r.stake_percent AS s")[0]
    assert (edge["v"], edge["s"]) == (25.3, 6.0)
    person = seeded.run_sql("SELECT full_name FROM Person WHERE id = 'sb'")[0]
    assert person["full_name"] == "Brin Sergey"


def test_a_rerun_updates_the_same_claim(seeded):
    _write()
    _write()
    n = seeded.run_sql("SELECT count(*) AS n FROM Claim WHERE from_id = 'sb' AND filing_type = 'DEF 14A'")[0]["n"]
    assert n == 1


def test_a_name_only_call_finds_the_company_and_passes_its_cik(seeded):
    out, fetch = _write(entity_id=None)
    assert out.get("db_error") is None and out["entity_id"] == "goog"
    assert fetch.call_args.kwargs["cik"] == "0001652044"
