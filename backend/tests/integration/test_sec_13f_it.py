"""Form 13F writes, against a real ArcadeDB (EDGAR mocked).

The unit tests prove the fetching and parsing; these prove what lands: holder
edges with counts, dollars and computed percentages, one node per filer however
many forms it appears in, the CUSIP stamp, and idempotent re-runs.
"""
from unittest.mock import patch

import pytest

from app.scraper import runner

pytestmark = pytest.mark.integration


HOLDERS = {
    "cusip_seen": "84615Q103", "period": "2026-06-30",
    "filings_total": 91, "filings_fetched": 2,
    "holders": [
        {"filer_cik": "1713833", "filer_name": "Gigafund Management Company, LLC",
         "shares": 171826745, "value_usd": 29358317651, "share_class": "CLASS A COM STK",
         "period": "2026-06-30",
         "source_url": "https://www.sec.gov/Archives/edgar/data/1713833/x-index.htm"},
        {"filer_cik": "1166588", "filer_name": "BNP PARIBAS FINANCIAL MARKETS",
         "shares": 707796, "value_usd": 120934025, "share_class": "Equity",
         "period": "2026-06-30",
         "source_url": "https://www.sec.gov/Archives/edgar/data/1166588/y-index.htm"},
    ],
}


def _company(it_db, cusip=None):
    props = ", cusip: $cu" if cusip else ""
    it_db.run_command(
        f"CREATE (:Entity {{id:'sx', name:'Space Exploration Technologies Corp.', "
        f"name_normalized:'space exploration technologies', "
        f"search_text:'Space Exploration Technologies Corp. SpaceX', "
        # UNPADDED on purpose: the XBRL denominator endpoint 404s an unpadded
        # CIK, so the runner must pad — with an already-padded fixture that
        # padding would be unobservable.
        f"type:'company', sec_cik:'1181412'{props}}})", {"cu": cusip})


def _run(it_db, holders=None, outstanding=13_100_000_000):
    with patch("app.scraper.sec_edgar.fetch_13f_holders",
               return_value=holders if holders is not None else HOLDERS) as fetched, \
         patch("app.scraper.sec_edgar.fetch_shares_outstanding",
               return_value=outstanding) as outs, \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        res = runner.run_sec_13f("SpaceX")
    return res, fetched, outs


def _edges(it_db):
    return [{k: v for k, v in r.items() if not k.startswith("@")}
            for r in it_db.run_sql("SELECT FROM OWNS")]


def test_holders_become_edges_with_counts_dollars_and_computed_percent(it_db):
    _company(it_db)
    res, _, _ = _run(it_db)
    assert res["status"] == "ok" and res["total"] == 2

    rows = it_db.run_command(
        "MATCH (f:Entity)-[r:OWNS]->(c:Entity {id:'sx'}) "
        "RETURN f.name AS filer, r.shares AS sh, r.value_usd AS usd, "
        "r.stake_percent AS pct, r.share_class AS cls, r.source_date AS d")
    got = {r["filer"]: r for r in rows}
    assert len(got) == 2
    giga = got["Gigafund Management Company, LLC"]
    assert giga["sh"] == 171826745 and giga["usd"] == 29358317651
    # 171,826,745 / 13.1bn — COMPUTED, not transcribed
    assert giga["pct"] == pytest.approx(1.3117, abs=0.001)
    assert giga["d"] == "2026-06-30"


def test_without_a_denominator_counts_stay_and_percent_is_absent(it_db):
    """SpaceX is private — no XBRL, no shares outstanding. A count with no
    percentage is honest; 0.0% would be the negligible-stake bug again."""
    _company(it_db)
    _run(it_db, outstanding=None)
    rows = it_db.run_command(
        "MATCH ()-[r:OWNS]->(:Entity {id:'sx'}) "
        "RETURN r.shares AS sh, r.stake_percent AS pct")
    assert all(r["sh"] and r.get("pct") is None for r in rows)


def test_a_filer_known_from_13g_gets_one_node_not_two(it_db):
    _company(it_db)
    it_db.run_command("CREATE (:Entity {id:'giga', name:'Gigafund', "
                      "name_normalized:'gigafund', sec_cik:'1713833', type:'fund'})")
    _run(it_db)
    n = it_db.run_command(
        "MATCH (e:Entity) WHERE e.sec_cik = '1713833' RETURN count(e) AS n")[0]["n"]
    assert n == 1, "the 13F filer must land on the node its CIK already has"


def test_the_cusip_is_stamped_fill_if_missing(it_db):
    _company(it_db)
    _run(it_db)
    assert it_db.run_command("MATCH (e:Entity {id:'sx'}) RETURN e.cusip AS c")[0]["c"] \
        == "84615Q103"

    # A second run reporting a different class's CUSIP must not clobber it.
    other = dict(HOLDERS, cusip_seen="69608A108")
    _run(it_db, holders=other)
    assert it_db.run_command("MATCH (e:Entity {id:'sx'}) RETURN e.cusip AS c")[0]["c"] \
        == "84615Q103"


def test_a_stored_cusip_reaches_the_fetcher(it_db):
    _company(it_db, cusip="84615Q103")
    _, fetched, _ = _run(it_db)
    assert fetched.call_args.kwargs["cusip"] == "84615Q103"


def test_the_denominator_cik_is_padded(it_db):
    _company(it_db)
    _, _, outs = _run(it_db)
    outs.assert_called_once_with("0001181412")


def test_rerun_refreshes_not_duplicates_and_lands_in_the_run_log(it_db):
    _company(it_db)
    _run(it_db)
    _run(it_db)
    n = it_db.run_command(
        "MATCH ()-[r:OWNS]->(:Entity {id:'sx'}) RETURN count(r) AS n")[0]["n"]
    assert n == 2, "a quarterly refresh must update the same edges"
    runs = it_db.run_sql("SELECT source, target, status, total FROM ScrapeRun "
                         "WHERE source = 'sec-13f'")
    assert len(runs) == 2
    assert all(r["status"] == "ok" and r["total"] == 2 for r in runs)


def test_the_claims_carry_the_counts(it_db):
    from app.claims import claims_for
    _company(it_db)
    _run(it_db)
    giga = it_db.run_command("MATCH (e:Entity) WHERE e.sec_cik = '1713833' "
                             "RETURN e.id AS id")[0]["id"]
    rows = claims_for(from_id=giga, to_id="sx")
    assert len(rows) == 1 and rows[0]["shares"] == 171826745


def test_an_unknown_company_writes_nothing(it_db):
    res, _, _ = _run(it_db)
    assert res["status"] == "no_results"
    assert _edges(it_db) == []


def test_the_filing_type_reaches_edge_claim_and_sources_panel(it_db):
    """The whole chain the "SEC EDGAR · 13F" label depends on: writer → OWNS
    edge → claim → the sources endpoint's claims query → the deduped row."""
    from app.claims import claims_for
    from app.routers.sources import get_sources_for_entity

    _company(it_db)
    _run(it_db)

    edge = it_db.run_command(
        "MATCH ()-[r:OWNS]->(:Entity {id:'sx'}) RETURN r.filing_type AS ft LIMIT 1")[0]
    assert edge["ft"] == "13F"

    giga = it_db.run_command("MATCH (e:Entity) WHERE e.sec_cik = '1713833' "
                             "RETURN e.id AS id")[0]["id"]
    assert claims_for(from_id=giga, to_id="sx")[0]["filing_type"] == "13F"

    rows = get_sources_for_entity("sx")
    sec_rows = [r for r in rows if r.get("filing_type")]
    assert sec_rows and all(r["filing_type"] == "13F" for r in sec_rows)
