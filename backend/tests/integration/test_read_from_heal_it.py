"""
`heal-read-from` against a real ArcadeDB: the batched SQL UPDATEs stamp the
right grade on edges, seats and claims from before the grade, and only those.
"""
import pytest

from app.scraper.read_from_heal import heal_read_from

pytestmark = pytest.mark.integration


@pytest.fixture
def graph(it_db):
    for sid, name in (("s-gleif", "GLEIF"), ("s-sec", "SEC EDGAR"), ("s-wd", "Wikidata")):
        it_db.run_command("CREATE (:Source {id:$i, name:$n})", {"i": sid, "n": name})
    for cid in ("a", "b", "c", "d", "e", "f", "g", "h", "i"):
        it_db.run_command("CREATE (:Entity {id:$id, name:$id})", {"id": cid})
    it_db.run_command("CREATE (:Person {id:'p', full_name:'Pat Person'})")
    edges = [  # (owner, owned, source, filing_type, source_date, read_from)
        ("a", "b", "s-gleif", "RR", "2026-01-01", None),
        ("a", "c", "s-sec", "13F", "2026-01-01", None),
        ("a", "d", "s-sec", "13G/A", "2025-02-14", None),   # XML era
        ("a", "e", "s-sec", "13G/A", "2019-02-14", None),   # text era
        ("a", "f", "s-sec", "EX-21", "2026-02-01", None),   # graded by the next run
        ("a", "g", "s-sec", "13G", "2026-02-01", "field"),  # already stamped
        ("a", "h", "s-sec", "13G/A", "2019-02-14", "prose"),  # stamped before the split: re-graded
        ("a", "i", "s-sec", "EX-8.1", "2026-02-01", "prose"),  # a paragraph list: stays prose
    ]
    for o, t, s, ft, d, rf in edges:
        it_db.run_command(
            f"MATCH (x:Entity {{id:'{o}'}}), (y:Entity {{id:'{t}'}}) "
            "CREATE (x)-[:OWNS {source_id:$s, filing_type:$ft, source_date:$d, read_from:$rf}]->(y)",
            {"s": s, "ft": ft, "d": d, "rf": rf})
        it_db.run_command(
            "CREATE (:Claim {claim_key:$k, kind:'owns', from_id:$o, to_id:$t, source_id:$s, "
            "filing_type:$ft, source_date:$d, read_from:$rf})",
            {"k": f"{o}|{t}|{s}", "o": o, "t": t, "s": s, "ft": ft, "d": d, "rf": rf})
    it_db.run_command(
        "MATCH (p:Person {id:'p'}), (y:Entity {id:'a'}) "
        "CREATE (p)-[:HAS_ROLE {role:'CEO', source_id:'s-wd'}]->(y)")
    it_db.run_command(
        "CREATE (:Claim {claim_key:'p|a|s-wd', kind:'role', from_id:'p', to_id:'a', source_id:'s-wd'})")
    return it_db


def _grades(it_db):
    rows = it_db.run_command(
        "MATCH (:Entity {id:'a'})-[r:OWNS]->(y:Entity) RETURN y.id AS id, r.read_from AS rf")
    return {r["id"]: r.get("rf") for r in rows}


def test_the_known_readers_are_stamped_and_the_rest_left(graph):
    res = heal_read_from()
    assert _grades(graph) == {"b": "field", "c": "field", "d": "field", "e": "form",
                              "f": None, "g": "field", "h": "form", "i": "prose"}
    claims = {r["to"]: r.get("rf") for r in graph.run_command(
        "MATCH (c:Claim {kind:'owns'}) RETURN c.to_id AS to, c.read_from AS rf")}
    assert claims == _grades(graph), "the claims follow the same rules"
    seat = graph.run_command("MATCH (:Person {id:'p'})-[r:HAS_ROLE]->() RETURN r.read_from AS rf")
    assert seat[0].get("rf") == "field"
    assert res == {"GLEIF:OWNS:field": 1, "GLEIF:Claim:field": 1,
                   "Wikidata:HAS_ROLE:field": 1, "Wikidata:Claim:field": 1,
                   "SEC EDGAR:OWNS:field": 2, "SEC EDGAR:Claim:field": 2,
                   # e by the date fill, h by the re-grade — one key
                   "SEC EDGAR:OWNS:form": 2, "SEC EDGAR:Claim:form": 2,
                   "dry_run": False}


def test_dry_run_counts_the_same_and_changes_nothing(graph):
    res = heal_read_from(dry_run=True)
    assert res["SEC EDGAR:OWNS:form"] == 2 and res["GLEIF:OWNS:field"] == 1
    assert _grades(graph)["b"] is None and _grades(graph)["h"] == "prose"


def test_running_twice_stamps_nothing_more(graph):
    heal_read_from()
    assert heal_read_from() == {"dry_run": False}
