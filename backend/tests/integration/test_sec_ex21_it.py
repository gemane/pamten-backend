"""Exhibit 21 write path against a real ArcadeDB: record → runner → database →
read the fields back. The mapper proving a field means nothing about storage
(the PSC register_id lesson), so this drives run_sec_ex21 end to end with only
the network mocked."""
import pytest
from unittest.mock import patch

from app.config import settings
from app.scraper import runner

pytestmark = pytest.mark.integration

APPLE_DATA = {
    "subsidiaries": [
        {"name": "Apple Operations International Limited", "jurisdiction": "Ireland"},
        {"name": "Braeburn Capital, Inc.", "jurisdiction": "Nevada, U.S.",
         "stake_percent": 100.0},
        {"name": "Diagnosys (Pinpoint) Inc.", "jurisdiction": "Florida, USA"},
        {"name": "Apple Ruritania GmbH", "jurisdiction": "Ruritania"},  # unmappable
    ],
    "form": "10-K", "filing_date": "2025-10-31",
    "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/a10-kexhibit21109272025.htm",
}


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield


def _apple(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'apple', name: 'Apple Inc.', name_normalized: 'apple', "
        "search_text: 'Apple Inc.', type: 'company', sec_cik: '0000320193'})")


def test_writes_subsidiaries_with_provenance_and_reads_them_back(it_db):
    _apple(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=APPLE_DATA):
        result = runner.run_sec_ex21("Apple")
    assert result["status"] == "ok"
    assert result["total"] == 4
    assert result["unmapped_jurisdictions"] == 1

    rows = it_db.run_command(
        "MATCH (a:Entity {id:'apple'})-[r:OWNS]->(b:Entity) "
        "RETURN b.name AS name, b.country AS country, r.filing_type AS ft, "
        "r.ownership_type AS ot, r.since AS since, r.source_date AS asof, "
        "r.source_url AS url, r.stake_percent AS stake")
    got = {dict(r)["name"]: dict(r) for r in rows}
    assert set(got) == {"Apple Operations International Limited",
                        "Braeburn Capital, Inc.", "Apple Ruritania GmbH",
                        "Diagnosys (Pinpoint) Inc."}
    ie = got["Apple Operations International Limited"]
    assert ie["country"] == "IE"
    assert ie["ft"] == "EX-21"
    assert ie["ot"] == "controlling"
    # A subsidiary LIST says what is held as of the filing, not since when:
    # the date is the as-of/source date and no start date is invented.
    assert ie["since"] is None
    assert ie["asof"] == "2025-10-31"
    assert "a10-kexhibit21" in ie["url"]
    assert ie["stake"] is None, "the exhibit states no stake; none is invented"
    assert got["Braeburn Capital, Inc."]["country"] == "US"
    # The finer grain the filing stated must survive — the reported bug.
    juris = it_db.run_command(
        "MATCH (:Entity {id:'apple'})-[:OWNS]->(b:Entity {name:'Diagnosys (Pinpoint) Inc.'}) "
        "RETURN b.country AS country, b.jurisdiction_code AS jc")
    assert dict(juris[0])["country"] == "US"
    assert dict(juris[0])["jc"] == "US-FL", "Florida, not just United States"
    assert got["Braeburn Capital, Inc."]["stake"] == 100.0, \
        "a STATED percentage (Astronics-style column) is stored"
    assert got["Apple Ruritania GmbH"]["country"] is None, \
        "unmappable jurisdiction stays uncoded, never guessed"


def test_resolves_an_existing_node_instead_of_duplicating(it_db):
    _apple(it_db)
    # the Irish subsidiary already exists from a GLEIF import, with an LEI
    it_db.run_command(
        "CREATE (:Entity {id: 'lei:APPLEOPS00000000001X', "
        "name: 'Apple Operations International Limited', "
        "name_normalized: 'apple operations international', "
        "search_text: 'Apple Operations International Limited', type: 'company', "
        "lei_id: 'APPLEOPS00000000001X'})")
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=APPLE_DATA):
        runner.run_sec_ex21("Apple")
    # count on `name`, not search_text: the FULL_TEXT index makes equality on
    # search_text behave as a token match ('Apple' matched three rows here)
    n = it_db.run_sql("SELECT count(*) AS n FROM Entity "
                      "WHERE name = 'Apple Operations International Limited'")[0]["n"]
    assert n == 1, "resolved by name — no duplicate beside the GLEIF node"
    edge = it_db.run_command(
        "MATCH (a:Entity {id:'apple'})-[:OWNS]->(b:Entity {id:'lei:APPLEOPS00000000001X'}) "
        "RETURN count(*) AS n")
    assert dict(edge[0])["n"] == 1, "the edge points at the existing node"


def test_rerun_is_gated_and_idempotent(it_db):
    _apple(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=APPLE_DATA):
        first = runner.run_sec_ex21("Apple")
        second = runner.run_sec_ex21("Apple")
        forced = runner.run_sec_ex21("Apple", force=True)
    assert first["status"] == "ok"
    assert second["status"] == "fresh", "same filing → gated"
    assert forced["status"] == "ok"
    n = it_db.run_command("MATCH (:Entity {id:'apple'})-[r:OWNS]->() RETURN count(r) AS n")
    assert dict(n[0])["n"] == 4, "force re-run updates, never doubles edges"


def test_no_cik_asks_for_the_sec_scrape_first(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'nocik', name: 'Private GmbH', name_normalized: 'private', "
        "search_text: 'Private GmbH', type: 'company'})")
    result = runner.run_sec_ex21("Private GmbH")
    assert result["status"] == "needs_sec_scrape"
    n = it_db.run_command("MATCH ()-[r:OWNS]->() RETURN count(r) AS n")
    assert dict(n[0])["n"] == 0, "nothing written before the record is judged"


# ── Co-holders a percentage cell names ────────────────────────────────────────

CHUBB_DATA = {
    "subsidiaries": [
        {"name": "Chubb Tempest Reinsurance Ltd.", "jurisdiction": "Bermuda", "stake_percent": 100.0},
        {"name": "Chubb Bermuda Insurance Ltd.", "jurisdiction": "Bermuda", "stake_percent": 100.0},
        # "66.66% 33.33% (Chubb Bermuda Insurance Ltd.)"
        {"name": "Oasis Investments Ltd.", "jurisdiction": "Bermuda", "stake_percent": 66.66,
         "co_owners": [{"name": "Chubb Bermuda Insurance Ltd.", "stake_percent": 33.33}]},
        # "87.99% 12.01% (Chubb Limited)" — the co-holder is the filer itself
        {"name": "Chubb INA Holdings LLC", "jurisdiction": "USA (Delaware)", "stake_percent": 87.99,
         "co_owners": [{"name": "Chubb Limited", "stake_percent": 12.01}]},
        # a co-holder the list does not carry: no edge, nothing looked up
        {"name": "Chubb Seguros Chile S.A.", "jurisdiction": "Chile", "stake_percent": 99.0,
         "co_owners": [{"name": "Some Stranger Holdings", "stake_percent": 1.0}]},
    ],
    "form": "10-K", "filing_date": "2026-02-26",
    "url": "https://www.sec.gov/Archives/edgar/data/896159/000089615926000012/cb-20251231xex211.htm",
}


def test_co_holders_get_their_own_edges_and_the_filer_its_own_share(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'chubb', name: 'CHUBB LIMITED', name_normalized: 'chubb', "
        "search_text: 'CHUBB LIMITED', type: 'company', sec_cik: '0000896159'})")
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=CHUBB_DATA):
        result = runner.run_sec_ex21("Chubb")
    assert result["status"] == "ok"
    assert result["total"] == 5 and result["co_owner_edges"] == 1

    rows = it_db.run_command(
        "MATCH (a:Entity)-[r:OWNS]->(b:Entity) "
        "RETURN a.name AS owner, b.name AS owned, r.stake_percent AS stake, r.filing_type AS ft")
    edges = {(r["owner"], r["owned"]): (r.get("stake"), r.get("ft")) for r in rows}
    # the listed holding at its stated share…
    assert edges[("CHUBB LIMITED", "Oasis Investments Ltd.")] == (66.66, "EX-21")
    # …and the co-holder's edge, from the listed subsidiary, at its share
    assert edges[("Chubb Bermuda Insurance Ltd.", "Oasis Investments Ltd.")] == (33.33, "EX-21")
    # a co-holder that is the filer becomes the filer's own stake
    assert edges[("CHUBB LIMITED", "Chubb INA Holdings LLC")][0] == 12.01
    # an unlisted co-holder draws nothing and creates nobody
    assert not any(o == "Some Stranger Holdings" for o, _ in edges)
    assert it_db.run_command("MATCH (e:Entity) WHERE e.name CONTAINS 'Stranger' RETURN count(e) AS n")[0]["n"] == 0
    assert len(edges) == 6
