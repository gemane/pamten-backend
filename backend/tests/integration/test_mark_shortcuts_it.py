"""Real-ArcadeDB tests for the ownership-shortcut pass.

GLEIF records "X is the ultimate parent of Y" alongside the chain that links
them. Most of those edges duplicate a path the graph already draws; some are the
only link to a company. Telling them apart is a global property of the graph, so
it is computed once here and stamped on the edge.

Run against a real database because it is variable-length Cypher with a
predicate over every hop — the exact shape this codebase has repeatedly got
wrong against mocks.
"""
import pytest

from app.db.arcadedb import run_query
from app.scraper.maintenance import mark_ownership_shortcuts

pytestmark = pytest.mark.integration


def _co(it_db, eid: str) -> None:
    it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{eid}', type: 'company'}})")


def _owns(it_db, a: str, b: str, doi: str) -> None:
    it_db.run_command(
        f"MATCH (x:Entity {{id: '{a}'}}), (y:Entity {{id: '{b}'}}) "
        f"CREATE (x)-[:OWNS {{until: null, source_id: 's', direct_or_indirect: '{doi}'}}]->(y)")


def _flag(a: str, b: str):
    rows = run_query(
        "MATCH (x:Entity {id:$a})-[r:OWNS]->(y:Entity {id:$b}) "
        "WHERE r.direct_or_indirect = 'indirect' RETURN r.shortcut AS f", {"a": a, "b": b})
    return rows[0]["f"] if rows else "no-edge"


def _redundant(it_db):
    """parent -> mid -> leaf, plus the ultimate-parent shortcut parent -> leaf."""
    for e in ("parent", "mid", "leaf"):
        _co(it_db, e)
    _owns(it_db, "parent", "mid", "direct")
    _owns(it_db, "mid", "leaf", "direct")
    _owns(it_db, "parent", "leaf", "indirect")


def _load_bearing(it_db):
    """Only the shortcut exists — GLEIF gave the top of the chain, not its steps."""
    for e in ("parent", "orphan"):
        _co(it_db, e)
    _owns(it_db, "parent", "orphan", "indirect")


# ── Proving redundancy ────────────────────────────────────────────────────────

def test_a_duplicated_shortcut_is_marked(it_db):
    _redundant(it_db)
    mark_ownership_shortcuts()
    assert _flag("parent", "leaf") is True


def test_the_only_link_to_a_company_is_not_marked(it_db):
    """The 58 that vanished. Marking this would hide the company entirely."""
    _load_bearing(it_db)
    mark_ownership_shortcuts()
    assert _flag("parent", "orphan") is False


def test_both_cases_in_one_graph(it_db):
    _redundant(it_db)
    _co(it_db, "orphan")
    _owns(it_db, "parent", "orphan", "indirect")

    res = mark_ownership_shortcuts()

    assert _flag("parent", "leaf") is True
    assert _flag("parent", "orphan") is False
    assert res["marked_redundant"] == 1
    assert res["marked_load_bearing"] == 1


def test_a_chain_through_another_shortcut_does_not_count(it_db):
    """Reachability must be by DIRECT edges only — the graph will not draw the
    other shortcut either, so a route through it is not a route.

    This is precisely the error that produced the regression: measuring
    reachability through edges that were themselves being hidden."""
    for e in ("p", "a", "b"):
        _co(it_db, e)
    _owns(it_db, "p", "a", "indirect")    # hidden if redundant
    _owns(it_db, "a", "b", "direct")
    _owns(it_db, "p", "b", "indirect")    # only reachable VIA the other shortcut

    mark_ownership_shortcuts()

    assert _flag("p", "a") is False
    assert _flag("p", "b") is False, "reached only through an edge that is itself hidden"


# ── Re-running ────────────────────────────────────────────────────────────────

def test_running_twice_changes_nothing(it_db):
    _redundant(it_db)
    mark_ownership_shortcuts()
    second = mark_ownership_shortcuts()
    assert second["marked_redundant"] == 0
    assert second["marked_load_bearing"] == 0
    assert second["unchanged"] == 1
    assert _flag("parent", "leaf") is True


def test_a_flag_is_cleared_when_its_chain_disappears(it_db):
    """A delta that retires a direct edge turns a redundant shortcut into the only
    link there is. If the pass only ever set flags, that company would silently
    vanish on the next render."""
    _redundant(it_db)
    mark_ownership_shortcuts()
    assert _flag("parent", "leaf") is True

    it_db.run_command("MATCH (:Entity {id:'mid'})-[r:OWNS]->(:Entity {id:'leaf'}) DELETE r")
    mark_ownership_shortcuts()

    assert _flag("parent", "leaf") is False


# ── Batching ──────────────────────────────────────────────────────────────────

def test_limit_bounds_the_work_and_reports_the_rest(it_db):
    for n in range(3):
        for e in (f"p{n}", f"m{n}", f"l{n}"):
            _co(it_db, e)
        _owns(it_db, f"p{n}", f"m{n}", "direct")
        _owns(it_db, f"m{n}", f"l{n}", "direct")
        _owns(it_db, f"p{n}", f"l{n}", "indirect")

    res = mark_ownership_shortcuts(limit=1)
    assert res["parents_total"] == 3
    assert res["parents_processed"] == 1
    assert res["remaining"] == 2


def test_a_graph_with_no_shortcuts_is_a_no_op(it_db):
    _co(it_db, "a")
    _co(it_db, "b")
    _owns(it_db, "a", "b", "direct")
    res = mark_ownership_shortcuts()
    assert res["parents_total"] == 0
