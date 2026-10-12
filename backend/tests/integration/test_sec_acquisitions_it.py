"""XBRL business-combination write path against a real ArcadeDB: record →
runner → database → read the fields back. Only the network is mocked. The
record is Alphabet's real Q1 2026 10-Q (Wiz closed 2026-03-11 for 29.5 bn,
Intersect 2026-03-10 for 5.9 bn) and its FY2025 10-K (both deals pending)."""
import pytest
from unittest.mock import patch

from app.config import settings
from app.scraper import runner
from app.scraper.sec_writer import _upsert_owns_sec

pytestmark = pytest.mark.integration

BASE = "https://www.sec.gov/Archives/edgar/data/1652044"
Q1_URL = f"{BASE}/000165204426000048/goog-20260331_htm.xml"
FY_URL = f"{BASE}/000165204426000018/goog-20251231_htm.xml"
EX21_URL = f"{BASE}/000165204426000018/googexhibit2101q42025.htm"

ALPHABET_DATA = {
    "filings": [
        {"form": "10-Q", "accession": "0001652044-26-000048", "filing_date": "2026-04-30", "url": Q1_URL,
         "acquisitions": [
             {"member": "goog:WizIncMember", "name": "Wiz Inc", "closing": "2026-03-11",
              "value_usd": 29_500_000_000.0, "concept": "BusinessCombinationConsiderationTransferred1"},
             {"member": "goog:IntersectAcquisitionMember", "name": "Intersect", "closing": "2026-03-10",
              "value_usd": 5_900_000_000.0, "concept": "BusinessCombinationConsiderationTransferred1"}],
         "pending_only": [], "skipped_aggregate": 0},
        {"form": "10-K", "accession": "0001652044-26-000018", "filing_date": "2026-02-05", "url": FY_URL,
         "acquisitions": [], "pending_only": ["goog:IntersectMember", "goog:WizIncMember"],
         "skipped_aggregate": 0},
    ],
    "newest_url": Q1_URL, "no_instance": 0, "unparseable": 0,
}


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield


def _alphabet(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'alphabet', name: 'Alphabet Inc.', name_normalized: 'alphabet', "
        "search_text: 'Alphabet Inc.', type: 'company', sec_cik: '0001652044'})")


def _run(data=ALPHABET_DATA, **kw):
    with patch("app.scraper.sec_acquisitions.fetch_acquisitions", return_value=data):
        return runner.run_sec_acquisitions("Alphabet", **kw)


def _edge(it_db, name):
    rows = it_db.run_command(
        "MATCH (a:Entity {id:'alphabet'})-[r:OWNS]->(b:Entity {name: $n}) "
        "RETURN r.filing_type AS ft, r.ownership_type AS ot, r.stake_percent AS stake, "
        "r.since AS since, r.since_basis AS basis, r.since_source_url AS since_url, "
        "r.event_date AS event, r.value_usd AS value, r.source_date AS asof, "
        "r.source_url AS url, r.read_from AS grade, r.shortcut AS shortcut, "
        "r.direct_or_indirect AS doi, r.until AS until", {"n": name})
    assert len(rows) == 1, f"one active edge for {name}, got {len(rows)}"
    return dict(rows[0])


def _claims(it_db, name):
    rows = it_db.run_command(
        "MATCH (b:Entity {name: $n}) WITH b "
        "MATCH (c:Claim {kind: 'owns', to_id: b.id}) "
        "RETURN c.filing_type AS ft, c.since AS since, c.since_basis AS basis, c.event_date AS event, "
        "c.read_from AS grade, c.source_url AS url, c.stake_percent AS stake ORDER BY c.filing_type",
        {"n": name})
    return [dict(r) for r in rows]


def test_writes_the_closing_as_a_stated_start_with_the_price_and_reads_it_back(it_db):
    _alphabet(it_db)
    result = _run()
    assert result["status"] == "ok"
    assert (result["total"], result["enriched"], result["pending_only"], result["filings"]) == (2, 0, 2, 2)
    wiz = _edge(it_db, "Wiz Inc")
    assert wiz["ft"] == "10-Q XBRL"
    assert wiz["ot"] == "controlling"
    assert wiz["stake"] is None, "a business combination states no percentage; none is invented"
    assert wiz["since"] == "2026-03-11"
    assert wiz["basis"] is None, "a closing date is a STATED start, not a bound"
    assert wiz["since_url"] == Q1_URL
    assert wiz["event"] == "2026-03-11"
    assert wiz["value"] == 29_500_000_000.0
    assert wiz["asof"] == "2026-04-30"
    assert wiz["url"] == Q1_URL
    assert wiz["grade"] == "field"
    assert wiz["doi"] is None, "the filer is the group; it does not say which company bought"
    assert wiz["shortcut"] is None, "no chain in the graph → the edge is drawn"
    claims = _claims(it_db, "Wiz Inc")
    assert len(claims) == 1
    assert claims[0] == {"ft": "10-Q XBRL", "since": "2026-03-11", "basis": None,
                         "event": "2026-03-11", "grade": "field", "url": Q1_URL, "stake": None}
    assert _edge(it_db, "Intersect")["value"] == 5_900_000_000.0
    # the pending deals of the 10-K drew nothing: two subsidiaries, two edges
    n = it_db.run_command("MATCH (:Entity {id:'alphabet'})-[r:OWNS]->() RETURN count(r) AS n")
    assert dict(n[0])["n"] == 2
    runs = it_db.run_sql("SELECT status, total, entity_id FROM ScrapeRun WHERE source = 'sec-acquisitions'")
    assert len(runs) == 1 and runs[0]["status"] == "ok" and runs[0]["total"] == 2
    assert runs[0]["entity_id"] == "alphabet"


def test_the_exhibit_21_row_keeps_the_answer_and_gains_the_date_and_price(it_db):
    _alphabet(it_db)
    it_db.run_command(
        "CREATE (:Entity {id: 'wiz', name: 'Wiz Inc', name_normalized: 'wiz', "
        "search_text: 'Wiz Inc', type: 'company'})")
    sid = runner._ensure_source(runner.SEC_EDGAR_SOURCE_NAME, runner.SEC_EDGAR_SOURCE_URL,
                                runner.SEC_EDGAR_CREDIBILITY)

    def exhibit_21():
        _upsert_owns_sec(owner_id="alphabet", owned_id="wiz", source_id=sid,
                         ownership_type="controlling", file_date="2026-02-05",
                         stake_percent=100.0, source_url=EX21_URL, filing_type="EX-21",
                         filing_dates_the_stake=False, read_from="table")
    exhibit_21()
    result = _run()
    assert (result["total"], result["enriched"]) == (1, 1), "Intersect written, Wiz enriched"
    wiz = _edge(it_db, "Wiz Inc")
    # the list's answer stays …
    assert (wiz["ft"], wiz["stake"], wiz["url"], wiz["grade"]) == ("EX-21", 100.0, EX21_URL, "table")
    # … and gains what only the deal states
    assert (wiz["since"], wiz["basis"], wiz["since_url"]) == ("2026-03-11", None, Q1_URL)
    assert (wiz["event"], wiz["value"]) == ("2026-03-11", 29_500_000_000.0)
    claims = _claims(it_db, "Wiz Inc")
    assert [c["ft"] for c in claims] == ["10-Q XBRL", "EX-21"], "two SEC claims: the list and the deal"
    assert claims[1]["stake"] == 100.0 and claims[1]["since"] is None, "the list's claim untouched"
    # the same filing read again, in the other order: the date and the price survive
    exhibit_21()
    wiz = _edge(it_db, "Wiz Inc")
    assert (wiz["since"], wiz["basis"], wiz["since_url"], wiz["value"]) == \
        ("2026-03-11", None, Q1_URL, 29_500_000_000.0)
    assert wiz["ft"] == "EX-21"
    n = it_db.run_command("MATCH (:Entity {id:'alphabet'})-[r:OWNS]->(:Entity {id:'wiz'}) RETURN count(r) AS n")
    assert dict(n[0])["n"] == 1, "one edge for the pair, whichever filing spoke last"


def test_a_pair_the_register_draws_through_a_chain_is_a_shortcut(it_db):
    _alphabet(it_db)
    it_db.run_command(
        "CREATE (g:Entity {id: 'lei:GOOGLE', name: 'GOOGLE LLC', name_normalized: 'google', "
        "search_text: 'GOOGLE LLC', type: 'company', lei_id: 'GOOGLE'}), "
        "(w:Entity {id: 'lei:WIZ', name: 'WIZ, INC.', name_normalized: 'wiz', "
        "search_text: 'WIZ, INC.', type: 'company', lei_id: 'WIZ'})")
    it_db.run_command(
        "MATCH (a:Entity {id:'alphabet'}), (g:Entity {id:'lei:GOOGLE'}), (w:Entity {id:'lei:WIZ'}) "
        "CREATE (a)-[:OWNS {source_id: 'gleif', direct_or_indirect: 'direct', filing_type: 'RR', "
        "credibility_score: 92, since: '2015-10-02', source_url: 'https://search.gleif.org/#/record/GOOGLE', "
        "source_date: '2026-03-20', last_scraped_at: '2026-03-20T00:00:00+00:00'}]->(g), "
        "(g)-[:OWNS {source_id: 'gleif', direct_or_indirect: 'direct', filing_type: 'RR', "
        "credibility_score: 92, since: '2026-03-20', source_url: 'https://search.gleif.org/#/record/WIZ', "
        "source_date: '2026-03-20', last_scraped_at: '2026-03-20T00:00:00+00:00'}]->(w)")
    result = _run()
    assert result["shortcuts"] == 1
    n = it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE name_normalized = 'wiz'")[0]["n"]
    assert n == 1, "resolved by name onto the GLEIF node — no 'Wiz Inc' beside 'WIZ, INC.'"
    wiz = _edge(it_db, "WIZ, INC.")
    assert wiz["shortcut"] is True, "Alphabet → Google LLC → Wiz is already drawn; the group-level edge is redundant"
    assert (wiz["since"], wiz["value"]) == ("2026-03-11", 29_500_000_000.0), \
        "the deal's date and price are still recorded on the edge"
    chain = it_db.run_command(
        "MATCH (:Entity {id:'lei:GOOGLE'})-[r:OWNS]->(:Entity {id:'lei:WIZ'}) RETURN r.shortcut AS s")
    assert dict(chain[0])["s"] is None, "the chain itself is untouched"
    assert _edge(it_db, "Intersect")["shortcut"] is None, "no chain to Intersect → drawn"


def test_rerun_is_gated_idempotent_and_force_rereads(it_db):
    _alphabet(it_db)
    first, second, forced = _run(), _run(), _run(force=True)
    assert (first["status"], second["status"], forced["status"]) == ("ok", "fresh", "ok")
    n = it_db.run_command("MATCH (:Entity {id:'alphabet'})-[r:OWNS]->() RETURN count(r) AS n")
    assert dict(n[0])["n"] == 2, "a forced re-read updates, never doubles"
    stamp = it_db.run_command("MATCH (e:Entity {id:'alphabet'}) RETURN e.sec_acquisitions_ingested AS s")
    assert dict(stamp[0])["s"] == Q1_URL


def test_a_closing_without_a_one_day_period_is_written_undated(it_db):
    _alphabet(it_db)
    data = {**ALPHABET_DATA, "filings": [
        {**ALPHABET_DATA["filings"][0],
         "acquisitions": [{**ALPHABET_DATA["filings"][0]["acquisitions"][0], "closing": None}]}]}
    result = _run(data)
    assert (result["total"], result["undated"]) == (1, 1)
    wiz = _edge(it_db, "Wiz Inc")
    assert wiz["since"] is None and wiz["event"] is None and wiz["since_url"] is None
    assert wiz["value"] == 29_500_000_000.0 and wiz["asof"] == "2026-04-30"


def test_the_newest_filing_wins_when_two_name_the_same_deal(it_db):
    # The Q2 10-Q restates the deal (a measurement-period adjustment) under a
    # DIFFERENT member QName: one write per normalised name, the newest first.
    _alphabet(it_db)
    q2 = {"form": "10-Q", "accession": "0001652044-26-000071", "filing_date": "2026-07-23",
          "url": f"{BASE}/000165204426000071/goog-20260630_htm.xml",
          "acquisitions": [{"member": "goog:WizAcquisitionMember", "name": "Wiz", "closing": "2026-03-11",
                            "value_usd": 29_600_000_000.0,
                            "concept": "BusinessCombinationConsiderationTransferred1"}],
          "pending_only": [], "skipped_aggregate": 0}
    data = {**ALPHABET_DATA, "filings": [q2, *ALPHABET_DATA["filings"]], "newest_url": q2["url"]}
    result = _run(data)
    assert (result["total"], result["repeated"]) == (2, 1)
    n = it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE name_normalized = 'wiz'")[0]["n"]
    assert n == 1, "'Wiz' and 'Wiz Inc' are one company"
    wiz = _edge(it_db, "Wiz")
    assert (wiz["value"], wiz["url"], wiz["asof"]) == (29_600_000_000.0, q2["url"], "2026-07-23"), \
        "the newest statement is the current one"


def test_no_cik_asks_for_the_sec_scrape_first(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'nocik', name: 'Private GmbH', name_normalized: 'private', "
        "search_text: 'Private GmbH', type: 'company'})")
    result = runner.run_sec_acquisitions("Private GmbH")
    assert result["status"] == "needs_sec_scrape"
    n = it_db.run_command("MATCH ()-[r:OWNS]->() RETURN count(r) AS n")
    assert dict(n[0])["n"] == 0
