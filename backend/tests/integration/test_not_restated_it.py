"""A Schedule 13D/G holding whose last statement is older than its form's
restatement window is dimmed as `not_restated` — and un-dimmed when a newer
filing refreshes the edge. Register-tier edges were exempt from every stale
pass, so a 13G/A from 2014 stood at full strength beside this year's."""
import pytest

from app.scraper.maintenance import mark_not_restated

pytestmark = pytest.mark.integration

TODAY = "2026-10-25"


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (:Entity {id:'issuer', name:'VeriSign', type:'company'})")
    for i in range(1, 8):
        it_db.run_command(f"CREATE (:Entity {{id:'h{i}', name:'Holder {i}', type:'company'}})")
    return it_db


def _seed(db, holder, filing_type, filed, until=None, stale=False, reason=None, source="sec-edgar"):
    db.run_command(
        "MATCH (a:Entity {id:$h}), (b:Entity {id:'issuer'}) CREATE (a)-[:OWNS "
        "{source_id:$src, filing_type:$ft, source_date:$sd, until:$u, stale:$st, "
        "stale_reason:$reason, stale_since:$ss, credibility_score:98, stake_percent:7.1}]->(b)",
        {"h": holder, "src": source, "ft": filing_type, "sd": filed, "u": until,
         "st": stale, "reason": reason, "ss": filed if stale else None})


def _edge(db, holder):
    return db.run_command(
        "MATCH (a {id:$h})-[r:OWNS]->(b {id:'issuer'}) RETURN r.stale AS stale, "
        "r.stale_reason AS reason, r.stale_since AS since, r.until AS until", {"h": holder})[0]


def test_an_old_13g_is_dimmed_from_its_filing_date_and_a_recent_one_is_not(graph):
    _seed(graph, "h1", "13G/A", "2014-02-14")          # Macquarie's 7.11 % of VeriSign
    _seed(graph, "h2", "13G/A", "2025-11-14")
    res = mark_not_restated(TODAY)
    assert res["marked"] == 1 and res["judged"] == 2
    assert _edge(graph, "h1") == {"stale": True, "reason": "not_restated", "since": "2014-02-14", "until": None}
    assert _edge(graph, "h2")["stale"] is False


def test_a_13g_filed_since_2024_gets_the_13d_window(graph):
    # findings item 33: Berkshire's 35 % of Sirius XM, 13G/A 2025-02-14 — dimmed
    # by the old cadence, though nothing has been due since 2024's round
    _seed(graph, "h1", "13G/A", "2025-02-14", stale=True, reason="not_restated")
    _seed(graph, "h2", "13G/A", "2024-02-12")          # the last annual round
    _seed(graph, "h3", "13G/A", "2023-12-29")          # the old cadence
    res = mark_not_restated("2026-11-01")
    assert (res["marked"], res["cleared"]) == (1, 1)
    assert not _edge(graph, "h1")["stale"] and _edge(graph, "h1")["reason"] is None
    assert not _edge(graph, "h2")["stale"]
    assert _edge(graph, "h3")["reason"] == "not_restated"


def test_a_13d_gets_the_longer_window(graph):
    _seed(graph, "h1", "13D", "2009-04-30")            # Liberty Media's 2009 13D on Sirius XM
    _seed(graph, "h2", "13D/A", "2023-03-01")
    mark_not_restated(TODAY)
    assert _edge(graph, "h1")["reason"] == "not_restated"
    assert _edge(graph, "h2")["stale"] is False


def test_other_records_closed_holdings_and_other_reasons_are_left_alone(graph):
    _seed(graph, "h1", "EX-21", "2014-02-14")
    _seed(graph, "h2", "13F", "2014-02-14")
    _seed(graph, "h3", "13G/A", "2014-02-14", until="2015-02-14")
    _seed(graph, "h4", "13G/A", "2014-02-14", stale=True, reason="ex21_not_listed")
    _seed(graph, "h5", "13G/A", "2014-02-14", source="wikidata")
    res = mark_not_restated(TODAY)
    assert res["marked"] == 0
    assert _edge(graph, "h1")["stale"] is False and _edge(graph, "h2")["stale"] is False
    assert _edge(graph, "h3")["stale"] is False and _edge(graph, "h5")["stale"] is False
    assert _edge(graph, "h4")["reason"] == "ex21_not_listed"


def test_a_refreshed_holding_is_cleared_and_only_this_reason_is(graph):
    _seed(graph, "h1", "13G/A", "2026-02-13", stale=True, reason="not_restated")
    _seed(graph, "h2", "13G/A", "2026-02-13", stale=True, reason="13f_not_confirmed")
    res = mark_not_restated(TODAY)
    assert res["cleared"] == 1
    assert _edge(graph, "h1") == {"stale": False, "reason": None, "since": None, "until": None}
    assert _edge(graph, "h2")["reason"] == "13f_not_confirmed"


def test_the_pass_is_idempotent(graph):
    _seed(graph, "h1", "13G/A", "2014-02-14")
    mark_not_restated(TODAY)
    assert mark_not_restated(TODAY)["marked"] == 0
    assert _edge(graph, "h1")["since"] == "2014-02-14"
