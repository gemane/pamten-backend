"""`descendant_counts`: how many companies sit below each one in a walked tree.
Pure arithmetic over the tree's nodes and edges — the walk itself is pinned
against a real ArcadeDB in tests/integration/test_subsidiary_tree_it.py."""
from app.routers.relationships import descendant_counts


def _node(cid, parent, depth):
    return {"entity": {"id": cid}, "parent_id": parent, "depth": depth}


def _edge(a, b):
    return {"from_id": a, "to_id": b, "depth": 0, "relationship": {}}


def test_a_chain_counts_everything_below_each_link():
    nodes = [_node("a", "root", 1), _node("b", "a", 2), _node("c", "b", 3)]
    edges = [_edge("root", "a"), _edge("a", "b"), _edge("b", "c")]
    assert descendant_counts(nodes, edges) == {"a": 2, "b": 1, "c": 0}


def test_a_company_held_twice_is_counted_once_per_holder_above_it():
    # mid1 and mid2 both hold leaf; top holds both — leaf is one company below top's two
    nodes = [_node("mid1", "root", 1), _node("mid2", "root", 1), _node("leaf", "mid1", 2)]
    edges = [_edge("root", "mid1"), _edge("root", "mid2"), _edge("mid1", "leaf"), _edge("mid2", "leaf")]
    assert descendant_counts(nodes, edges) == {"mid1": 1, "mid2": 1, "leaf": 0}


def test_a_flat_list_beside_a_sub_group_does_not_double_count():
    # Microsoft's Exhibit 21 names King beside Activision; GLEIF puts King under it
    nodes = [_node("act", "ms", 1), _node("king", "act", 2), _node("candy", "king", 3)]
    edges = [_edge("ms", "act"), _edge("ms", "king"), _edge("ms", "candy"),
             _edge("act", "king"), _edge("king", "candy")]
    assert descendant_counts(nodes, edges) == {"act": 2, "king": 1, "candy": 0}


def test_a_cycle_neither_loops_nor_counts_a_company_as_its_own_descendant():
    nodes = [_node("a", "root", 1), _node("b", "a", 2)]
    edges = [_edge("root", "a"), _edge("a", "b"), _edge("b", "a")]
    assert descendant_counts(nodes, edges) == {"a": 1, "b": 1}


def test_the_root_is_never_a_descendant():
    # a cross-holding back to the root is a real edge, not a subsidiary
    nodes = [_node("a", "root", 1), _node("b", "a", 2)]
    edges = [_edge("root", "a"), _edge("a", "b"), _edge("b", "root")]
    assert descendant_counts(nodes, edges) == {"a": 1, "b": 0}


def test_an_empty_tree_has_no_counts():
    assert descendant_counts([], []) == {}
