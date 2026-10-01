"""The whole tree below a company, against a real ArcadeDB: a level-by-level
adjacency walk (SQL `expand(outE())` from vertex rids), which is exactly the
kind of query a mocked session accepts and a real server may not."""
import pytest

from app.routers.relationships import subsidiary_tree_of

pytestmark = pytest.mark.integration


def _company(it_db, eid):
    it_db.run_command("CREATE (:Entity {id: $id, name: $id, type: 'company'})", {"id": eid})


def _owns(it_db, parent, child, **props):
    sets = "".join(f", {k}: ${k}" for k in props)
    it_db.run_command(
        f"MATCH (a:Entity {{id: $p}}), (b:Entity {{id: $c}}) CREATE (a)-[:OWNS {{source_id: 's'{sets}}}]->(b)",
        {"p": parent, "c": child, **props})


def _seed(it_db):
    for e in ("top", "mid1", "mid2", "leaf1", "leaf2", "deep", "sold", "short"):
        _company(it_db, e)
    _owns(it_db, "top", "mid1", stake_percent=100.0)
    _owns(it_db, "top", "mid2", stake_percent=60.0)
    _owns(it_db, "mid1", "leaf1", stake_percent=66.0)
    _owns(it_db, "mid2", "leaf1", stake_percent=34.0)          # a co-holder: two edges, one node
    _owns(it_db, "mid2", "leaf2")
    _owns(it_db, "leaf2", "deep")
    _owns(it_db, "deep", "top")                                # a cycle back to the root
    _owns(it_db, "top", "sold", until="2020-01-01")            # ended
    _owns(it_db, "top", "short", shortcut=True, direct_or_indirect="indirect")   # a proven shortcut


def test_every_level_once_with_one_parent_each_and_every_holding(it_db):
    _seed(it_db)
    tree = subsidiary_tree_of("top")
    assert tree["root_id"] == "top" and tree["truncated"] is False
    by = {n["entity"]["id"]: (n["parent_id"], n["depth"]) for n in tree["nodes"]}
    assert by == {"mid1": ("top", 1), "mid2": ("top", 1),
                  "leaf1": ("mid1", 2),           # the larger holder is the parent a list shows
                  "leaf2": ("mid2", 2), "deep": ("leaf2", 3)}
    pairs = sorted((e["from_id"], e["to_id"]) for e in tree["edges"])
    # every holding among the tree's companies — the co-holder's, and the cross-holding back
    # to the root, which is a real edge the graph may draw
    assert pairs == [("deep", "top"), ("leaf2", "deep"), ("mid1", "leaf1"), ("mid2", "leaf1"),
                     ("mid2", "leaf2"), ("top", "mid1"), ("top", "mid2")]
    # ended and shortcut edges are out, as in the profile; the cycle does not loop or re-add the root
    assert "sold" not in by and "short" not in by and "top" not in by
    rel = next(e["relationship"] for e in tree["edges"] if e["to_id"] == "mid2")
    assert rel["stake_percent"] == 60.0 and not any(k.startswith("@") for k in rel)
    assert tree["nodes"][0]["entity"]["name"] in ("mid1", "mid2")


def test_the_cap_is_on_companies_and_says_so(it_db):
    _seed(it_db)
    tree = subsidiary_tree_of("top", max_nodes=2)
    assert len(tree["nodes"]) == 2 and tree["truncated"] is True
    assert {n["entity"]["id"] for n in tree["nodes"]} == {"mid1", "mid2"}


def test_a_leaf_has_an_empty_tree_and_a_missing_company_none(it_db):
    _seed(it_db)
    assert subsidiary_tree_of("leaf1") == {"root_id": "leaf1", "nodes": [], "edges": [], "truncated": False}
    assert subsidiary_tree_of("nope") is None
