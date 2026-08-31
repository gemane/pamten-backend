"""
Real-ArcadeDB tests for filer-side SEC ingestion.

The unit tests mock the network and prove the parsing; these prove the writes —
that holdings become OWNS edges pointing OUT of the filer, that an exited stake
is stored as history rather than as a current position, and that re-running
doesn't duplicate anything.

EDGAR is mocked here (no network in CI); only the graph is real.
"""
from unittest.mock import patch

import pytest

from app.scraper import runner

pytestmark = pytest.mark.integration


HOLDINGS = [
    {"subject_cik": "0000859737", "subject_name": "Hologic Inc", "stake_percent": 7.49,
     "file_date": "2026-04-30", "form_type": "SCHEDULE 13G", "until": None,
     "source_url": "https://sec.gov/x/1"},
    {"subject_cik": "0000821189", "subject_name": "EOG Resources Inc", "stake_percent": 10.01,
     "file_date": "2025-06-05", "form_type": "SCHEDULE 13G", "until": "2026-03-26",
     "source_url": "https://sec.gov/x/2"},
]


def _run(it_db, holdings=None, cik="0002100119", name="VANGUARD CAPITAL MANAGEMENT LLC",
         succeeds=None, succeeds_name="VANGUARD GROUP INC"):
    names = {cik: name}
    if succeeds:
        names[succeeds] = succeeds_name
    with patch("app.scraper.sec_edgar.fetch_filer_name", side_effect=lambda c: names.get(c)), \
         patch("app.scraper.sec_edgar.fetch_filer_holdings",
               return_value=HOLDINGS if holdings is None else holdings), \
         patch("app.scraper.sec_edgar.fetch_affiliated_managers", return_value=[]), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        return runner.run_sec_holdings(cik, succeeds_cik=succeeds)


def _edges(it_db) -> list[dict]:
    """All OWNS edges as plain dicts.

    Filtered in Python rather than SQL: ArcadeDB drops null properties entirely
    (a current holding has no `until` key at all, rather than `until = null`),
    and float equality in a WHERE clause doesn't match a stored double.
    """
    return [{k: v for k, v in r.items() if not k.startswith("@")}
            for r in it_db.run_sql("SELECT FROM OWNS")]


def _edge_with_stake(it_db, stake: float) -> dict | None:
    return next((e for e in _edges(it_db) if e.get("stake_percent") == stake), None)


def test_holdings_become_edges_out_of_the_filer(it_db):
    result = _run(it_db)
    assert result["status"] == "ok"
    assert result["total"] == 2

    rows = it_db.run_sql(
        "SELECT name FROM (SELECT expand(out('OWNS')) FROM Entity "
        "WHERE sec_cik = '0002100119')")
    assert {r["name"] for r in rows} == {"Hologic Inc", "EOG Resources Inc"}


def test_a_live_stake_is_current_and_carries_its_percent(it_db):
    _run(it_db)
    edge = _edge_with_stake(it_db, 7.49)
    assert edge is not None
    assert not edge.get("until"), "a live holding must not carry an end date"
    assert edge["source_url"] == "https://sec.gov/x/1"


def test_an_exited_stake_is_recorded_as_history(it_db):
    # The Vanguard case: a later 0% amendment means the holding ended. Storing it
    # as current would claim ownership the filer has explicitly disclaimed.
    _run(it_db)
    edge = _edge_with_stake(it_db, 10.01)
    assert edge is not None
    assert edge.get("until") == "2026-03-26"
    assert result_count(it_db) == 2


def result_count(it_db) -> int:
    return len(_edges(it_db))


def test_rerunning_does_not_duplicate_edges(it_db):
    _run(it_db)
    first = result_count(it_db)
    _run(it_db)
    assert result_count(it_db) == first, "a second run duplicated the edges"


def test_a_subject_already_in_the_graph_is_reused_not_duplicated(it_db):
    it_db.run_command(
        "CREATE (e:Entity {id:'lei:HOLOGIC', name:'HOLOGIC INC', "
        "name_normalized:'hologic', type:'company', sec_cik:'0000859737'})")
    _run(it_db)
    rows = it_db.run_sql("SELECT id FROM Entity WHERE sec_cik = '0000859737'")
    assert len(rows) == 1, "the subject was created a second time instead of matched on CIK"
    assert rows[0]["id"] == "lei:HOLOGIC"


def test_succession_links_the_old_filer_to_the_new_one(it_db):
    result = _run(it_db, succeeds="0000102909")
    assert result["succession"] == {"predecessor": "VANGUARD GROUP INC",
                                    "successor": "VANGUARD CAPITAL MANAGEMENT LLC"}
    rows = it_db.run_sql(
        "SELECT name FROM (SELECT expand(out('SUCCEEDED_BY')) FROM Entity "
        "WHERE sec_cik = '0000102909')")
    assert [r["name"] for r in rows] == ["VANGUARD CAPITAL MANAGEMENT LLC"]


def test_no_succession_edge_without_the_flag(it_db):
    _run(it_db)
    assert it_db.run_sql("SELECT count(*) AS n FROM SUCCEEDED_BY")[0]["n"] == 0


def test_an_unknown_filer_writes_nothing(it_db):
    with patch("app.scraper.sec_edgar.fetch_filer_name", return_value=None), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        result = runner.run_sec_holdings("0009999999")
    assert result["status"] == "no_results"
    assert result_count(it_db) == 0


# ── Affiliated managers ───────────────────────────────────────────────────────
#
# Modelled as RELATED_TO{relation:'affiliate'}, deliberately not OWNS: a 13F cover
# page naming another manager establishes group membership, not ownership. Writing
# it as ownership would invent a fact the filing doesn't support.

AFFILIATES = [
    {"cik": "0002100119", "name": "VANGUARD CAPITAL MANAGEMENT LLC",
     "source_url": "https://sec.gov/13fnt", "source_date": "2026-05-08", "form_type": "13F-NT"},
    {"cik": "0000933478", "name": "VANGUARD FIDUCIARY TRUST CO",
     "source_url": "https://sec.gov/13fnt", "source_date": "2026-05-08", "form_type": "13F-NT"},
]


def _run_with_affiliates(affiliates=None, holdings=None):
    with patch("app.scraper.sec_edgar.fetch_filer_name", return_value="VANGUARD GROUP INC"), \
         patch("app.scraper.sec_edgar.fetch_filer_holdings", return_value=holdings or []), \
         patch("app.scraper.sec_edgar.fetch_affiliated_managers",
               return_value=AFFILIATES if affiliates is None else affiliates), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        return runner.run_sec_holdings("0000102909")


def test_affiliates_are_linked_with_a_named_relation(it_db):
    result = _run_with_affiliates()
    assert result["affiliates"] == 2

    rows = it_db.run_sql(
        "SELECT name FROM (SELECT expand(out('RELATED_TO')) FROM Entity WHERE sec_cik='0000102909')")
    assert {r["name"] for r in rows} == {"VANGUARD CAPITAL MANAGEMENT LLC",
                                         "VANGUARD FIDUCIARY TRUST CO"}


def test_the_relation_is_labelled_and_sourced(it_db):
    _run_with_affiliates()
    edges = [{k: v for k, v in r.items() if not k.startswith("@")}
             for r in it_db.run_sql("SELECT FROM RELATED_TO")]
    assert edges and all(e["relation"] == "affiliate" for e in edges)
    assert all(e.get("source_url") == "https://sec.gov/13fnt" for e in edges)


def test_affiliation_is_not_written_as_ownership(it_db):
    # The whole point of the modelling choice.
    _run_with_affiliates()
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0


def test_rerunning_does_not_duplicate_affiliate_edges(it_db):
    _run_with_affiliates()
    first = it_db.run_sql("SELECT count(*) AS n FROM RELATED_TO")[0]["n"]
    _run_with_affiliates()
    assert it_db.run_sql("SELECT count(*) AS n FROM RELATED_TO")[0]["n"] == first


def test_an_affiliate_already_in_the_graph_is_matched_on_cik(it_db):
    it_db.run_command(
        "CREATE (e:Entity {id:'vcm', name:'Vanguard Capital Management', "
        "name_normalized:'vanguard capital management', type:'company', sec_cik:'0002100119'})")
    _run_with_affiliates()
    rows = it_db.run_sql("SELECT id FROM Entity WHERE sec_cik = '0002100119'")
    assert len(rows) == 1 and rows[0]["id"] == "vcm"


def test_a_filer_listing_itself_is_skipped(it_db):
    self_ref = [{"cik": "0000102909", "name": "VANGUARD GROUP INC",
                 "source_url": "u", "source_date": "2026-05-08", "form_type": "13F-NT"}]
    result = _run_with_affiliates(affiliates=self_ref)
    assert result["affiliates"] == 0
    assert it_db.run_sql("SELECT count(*) AS n FROM RELATED_TO")[0]["n"] == 0


# ── The issuer-side path closes an exit too (reported 2026-08-28) ────────────
# `fetch_ownership_filings` had no zero-percent handling at all — its filer-side
# sibling above has had it since the beginning. Vanguard's realignment 13G/As
# report 0 in every power row, and eighteen went into the graph as live
# "owns 0.0%" edges instead of closing the position.

def _write_issuer_side(it_db, filings):
    """Drive the issuer-side writer with parsed filings, as a scrape would."""
    from unittest.mock import patch
    data = {"name": "Apple Inc", "cik": "0000320193",
            "ownership_filings": filings, "executives": [], "holdings": []}
    with patch("app.scraper.sec_edgar.scrape_company", return_value=data), \
         patch("app.scraper.sec_edgar.fetch_filer_country", return_value="US"), \
         patch("app.scraper.sec_edgar.fetch_filer_headquarters", return_value=None), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        return runner.run_scrape_sec_edgar("Apple Inc")


def _filing(**over):
    base = {"investor_name": "The Vanguard Group", "investor_cik": "0000102909",
            "form_type": "SCHEDULE 13G/A", "file_date": "2025-02-10",
            "stake_percent": 7.48, "voting_power_pct": None, "ownership_type": "minority",
            "share_class": "Common Stock", "shares": None, "shares_outstanding": None,
            "voting_shares": None, "is_individual": False, "until": None,
            "source_url": "https://sec.gov/x/1", "group_members": []}
    base.update(over)
    return base


def test_an_issuer_side_exit_closes_the_holding(it_db):
    _write_issuer_side(it_db, [_filing(until="2026-03-26")])
    edge = _edge_with_stake(it_db, 7.48)
    assert edge is not None, "the position must stay in the timeline, not vanish"
    assert edge.get("until") == "2026-03-26"


def test_a_live_issuer_side_holding_stays_current(it_db):
    _write_issuer_side(it_db, [_filing()])
    edge = _edge_with_stake(it_db, 7.48)
    assert edge is not None and edge.get("until") is None


def _websites(it_db) -> dict:
    return {r["name"]: r.get("website")
            for r in it_db.run_sql("SELECT name, website FROM Entity")}


def test_the_website_reaches_the_filer_and_its_subjects(it_db):
    # The submissions document is already fetched (and cached) for the name,
    # country and headquarters — the website rides along for free, on the
    # filer AND on every subject with a CIK. First writer wins thereafter.
    with patch("app.scraper.sec_edgar.fetch_filer_website",
               side_effect=lambda c: f"https://cik-{c.lstrip('0')}.example.com/"):
        _run(it_db)
    sites = _websites(it_db)
    assert sites["VANGUARD CAPITAL MANAGEMENT LLC"] == "https://cik-2100119.example.com/"
    assert sites["Hologic Inc"] == "https://cik-859737.example.com/"
    assert sites["EOG Resources Inc"] == "https://cik-821189.example.com/"

    # A later run with a different answer must not clobber what is stored.
    with patch("app.scraper.sec_edgar.fetch_filer_website",
               return_value="https://usurper.example.com/"):
        _run(it_db)
    assert _websites(it_db)["Hologic Inc"] == "https://cik-859737.example.com/"
