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
    assert tree["root_id"] == "top" and tree["truncated"] is False and tree["total"] == 5
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
    # …and still knows how large the whole tree is: five companies, the
    # co-held one once, the ended and shortcut ones not, the root never
    assert tree["total"] == 5


def test_a_leaf_has_an_empty_tree_and_a_missing_company_none(it_db):
    _seed(it_db)
    assert subsidiary_tree_of("leaf1") == {"root_id": "leaf1", "nodes": [], "edges": [], "truncated": False, "total": 0}
    assert subsidiary_tree_of("nope") is None


def test_a_company_hangs_under_its_deepest_holder_not_the_flat_list(it_db):
    """Microsoft's flat Exhibit 21 names Activision's subsidiaries beside
    Activision; GLEIF puts them under it. The deeper statement places them."""
    for e in ("ms", "act", "king", "candy", "linkedin"):
        _company(it_db, e)
    _owns(it_db, "ms", "act")
    _owns(it_db, "ms", "king")            # the flat list
    _owns(it_db, "ms", "candy")           # the flat list
    _owns(it_db, "ms", "linkedin")
    _owns(it_db, "act", "king")           # the specific holder
    _owns(it_db, "king", "candy")         # …and one further down
    tree = subsidiary_tree_of("ms")
    by = {n["entity"]["id"]: (n["parent_id"], n["depth"]) for n in tree["nodes"]}
    assert by == {"act": ("ms", 1), "linkedin": ("ms", 1), "king": ("act", 2), "candy": ("king", 3)}
    # every holding is still an edge, each at its holder's level
    assert sorted((e["from_id"], e["to_id"], e["depth"]) for e in tree["edges"]) == [
        ("act", "king", 2), ("king", "candy", 3), ("ms", "act", 1), ("ms", "candy", 1),
        ("ms", "king", 1), ("ms", "linkedin", 1)]


def test_a_cross_holding_never_detaches_a_branch(it_db):
    for e in ("top", "a", "b"):
        _company(it_db, e)
    _owns(it_db, "top", "a", stake_percent=100.0)
    _owns(it_db, "top", "b", stake_percent=100.0)
    _owns(it_db, "a", "b", stake_percent=10.0)
    _owns(it_db, "b", "a", stake_percent=10.0)      # a and b hold each other
    tree = subsidiary_tree_of("top")
    by = {n["entity"]["id"]: (n["parent_id"], n["depth"]) for n in tree["nodes"]}
    # the 100 % holder places both; the 10 % cross-holdings are edges, not parents
    assert by == {"a": ("top", 1), "b": ("top", 1)}
    assert len(tree["edges"]) == 4


def test_a_cross_holding_between_equals_never_detaches_a_branch(it_db):
    for e in ("top", "a", "b"):
        _company(it_db, e)
    _owns(it_db, "top", "a")
    _owns(it_db, "top", "b")
    _owns(it_db, "a", "b")
    _owns(it_db, "b", "a")                          # no stakes stated anywhere: the deeper holder wins…
    tree = subsidiary_tree_of("top")
    by = {n["entity"]["id"]: (n["parent_id"], n["depth"]) for n in tree["nodes"]}
    # …for one of them; the other stays on the root, so it is still a tree
    assert sorted(by.values()) in ([("a", 2), ("top", 1)], [("b", 2), ("top", 1)])


def test_at_the_same_depth_the_larger_stake_is_the_parent(it_db):
    for e in ("top", "a", "b", "shared"):
        _company(it_db, e)
    _owns(it_db, "top", "a", stake_percent=100.0)
    _owns(it_db, "top", "b", stake_percent=100.0)
    _owns(it_db, "a", "shared", stake_percent=10.9)
    _owns(it_db, "b", "shared", stake_percent=79.7)
    tree = subsidiary_tree_of("top")
    assert {n["entity"]["id"]: n["parent_id"] for n in tree["nodes"]}["shared"] == "b"


def test_the_largest_holder_places_a_company_even_when_a_small_one_sits_deeper(it_db):
    for e in ("top", "big", "x", "y", "small", "co"):
        _company(it_db, e)
    _owns(it_db, "top", "big")
    _owns(it_db, "top", "x")
    _owns(it_db, "x", "y")
    _owns(it_db, "y", "small")
    _owns(it_db, "big", "co", stake_percent=99.9)
    _owns(it_db, "small", "co", stake_percent=0.1)
    tree = subsidiary_tree_of("top")
    assert {n["entity"]["id"]: n["parent_id"] for n in tree["nodes"]}["co"] == "big"


def test_the_tree_as_of_a_day(it_db):
    """An ended holding is back before its end, a stated later start is gone, a
    lower bound stays — the profile's rule, level by level."""
    for e in ("top", "old", "new", "bound", "under-old"):
        _company(it_db, e)
    _owns(it_db, "top", "old", since="2010-01-01", until="2018-03-31")
    _owns(it_db, "old", "under-old", since="2011-01-01")
    _owns(it_db, "top", "new", since="2021-05-01")
    _owns(it_db, "top", "bound", since="2023-06-30", since_basis="first_listed")
    _company(it_db, "newly")
    _owns(it_db, "top", "newly", since="2023-06-30", since_basis="newly_listed")   # the 2022 list does not name it
    ids = lambda t: {n["entity"]["id"] for n in t["nodes"]}
    assert ids(subsidiary_tree_of("top")) == {"new", "bound", "newly"}
    assert ids(subsidiary_tree_of("top", as_of="2015-12-31")) == {"old", "under-old", "bound"}
    assert ids(subsidiary_tree_of("top", as_of="2018-03-31")) == {"bound"}          # until == day: ended
    assert ids(subsidiary_tree_of("top", as_of="2021-12-31")) == {"new", "bound"}
    assert ids(subsidiary_tree_of("top", as_of="2023-12-31")) == {"new", "bound", "newly"}


def test_every_holding_says_how_many_companies_sit_below_the_one_it_reaches(it_db):
    _seed(it_db)
    tree = subsidiary_tree_of("top")
    below = {(e["from_id"], e["to_id"]): e["relationship"].get("descendants") for e in tree["edges"]}
    assert below == {
        ("top", "mid1"): 1,            # leaf1
        ("top", "mid2"): 3,            # leaf1 (co-held, once), leaf2, deep
        ("mid1", "leaf1"): 0, ("mid2", "leaf1"): 0,
        ("mid2", "leaf2"): 1,          # deep
        ("leaf2", "deep"): 0,          # its holding of the root is a cross-holding, not a subsidiary
        ("deep", "top"): None,         # the root is not a tree node and gets no figure
    }
