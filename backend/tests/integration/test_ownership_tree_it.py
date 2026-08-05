"""
Real-ArcadeDB tests for /relationships/ownership-tree.

This endpoint had no integration coverage, and that is exactly how it stayed
broken: it asked for `RETURN path`, but ArcadeDB serialises a path as its *string*
form — "(#1:3)-[#37:20725]->(#1:120)" — not an object with .nodes/.relationships.
Every entity that actually had a subsidiary produced a 500. The mocked unit tests
passed throughout, because their fake handed back an object with the attributes
the code wanted.

So these assert against a real database: that a tree comes back at all, that its
shape is right, and that the limit and truncation flag work on real rows.
"""
import pytest

from app.routers.relationships import ownership_tree_of

pytestmark = pytest.mark.integration


def _tree(it_db, n_subs: int = 3):
    it_db.run_command("CREATE (e:Entity {id:'P', name:'Parent', type:'company'})")
    for i in range(n_subs):
        it_db.run_command(f"CREATE (e:Entity {{id:'S{i}', name:'Sub {i}', type:'company'}})")
        it_db.run_command(
            f"MATCH (p:Entity {{id:'P'}}), (s:Entity {{id:'S{i}'}}) "
            f"CREATE (p)-[:OWNS {{until:null, stake_percent:{i + 1}, source_id:'s'}}]->(s)")


def test_returns_paths_with_nodes_and_relationships(it_db):
    _tree(it_db)
    paths, truncated = ownership_tree_of("P", depth=1, limit=10)

    assert len(paths) == 3
    assert truncated is False
    for p in paths:
        # The regression: with `RETURN path` this raised AttributeError instead.
        assert [n["id"] for n in p["nodes"]][0] == "P"
        assert len(p["relationships"]) == 1
        assert p["relationships"][0]["stake_percent"] in (1, 2, 3)


def test_nodes_carry_no_arcadedb_metadata(it_db):
    _tree(it_db, 1)
    node = ownership_tree_of("P", depth=1, limit=10)[0][0]["nodes"][0]
    assert not any(k.startswith("@") for k in node), f"metadata leaked: {sorted(node)}"


def test_limit_truncates_and_reports_it(it_db):
    _tree(it_db, 5)
    paths, truncated = ownership_tree_of("P", depth=1, limit=2)
    assert len(paths) == 2
    assert truncated is True


def test_limit_equal_to_the_row_count_is_not_truncated(it_db):
    # The boundary the limit+1 fetch exists for, checked against a real query.
    _tree(it_db, 4)
    paths, truncated = ownership_tree_of("P", depth=1, limit=4)
    assert len(paths) == 4
    assert truncated is False


def test_entity_without_subsidiaries_returns_empty(it_db):
    it_db.run_command("CREATE (e:Entity {id:'LONE', name:'Lone', type:'company'})")
    assert ownership_tree_of("LONE", depth=3, limit=10) == ([], False)


def test_depth_reaches_grandchildren(it_db):
    it_db.run_command("CREATE (e:Entity {id:'A', name:'A', type:'company'})")
    it_db.run_command("CREATE (e:Entity {id:'B', name:'B', type:'company'})")
    it_db.run_command("CREATE (e:Entity {id:'C', name:'C', type:'company'})")
    it_db.run_command("MATCH (a:Entity {id:'A'}), (b:Entity {id:'B'}) "
                      "CREATE (a)-[:OWNS {until:null, source_id:'s'}]->(b)")
    it_db.run_command("MATCH (b:Entity {id:'B'}), (c:Entity {id:'C'}) "
                      "CREATE (b)-[:OWNS {until:null, source_id:'s'}]->(c)")

    depth1 = ownership_tree_of("A", depth=1, limit=10)[0]
    depth2 = ownership_tree_of("A", depth=2, limit=10)[0]

    assert len(depth1) == 1
    assert len(depth2) == 2
    # The two-hop path carries both edges and all three nodes.
    longest = max(depth2, key=lambda p: len(p["relationships"]))
    assert len(longest["relationships"]) == 2
    assert [n["id"] for n in longest["nodes"]] == ["A", "B", "C"]
