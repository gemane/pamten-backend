from app.database import _is_write


def test_detects_create():
    assert _is_write("CREATE (e:Entity {id: $id}) RETURN e")


def test_detects_set():
    assert _is_write("MATCH (e:Entity {id: $id}) SET e.name = $name RETURN e")


def test_detects_merge_delete_detach_remove_drop():
    for clause in ["MERGE", "DELETE", "DETACH DELETE", "REMOVE", "DROP"]:
        assert _is_write(f"MATCH (e) {clause} e"), clause


def test_read_only_match_is_not_a_write():
    assert not _is_write("MATCH (e:Entity {id: $id}) RETURN e")


def test_read_only_contains_search_is_not_a_write():
    query = """
        MATCH (n:Entity)
        WHERE toLower(n.name) CONTAINS $q
        RETURN n
    """
    assert not _is_write(query)


def test_literal_value_matching_a_write_keyword_is_ignored():
    # A company literally named "Delete Corp" must never be inlined as raw
    # text in real call sites (data always goes through $params), but the
    # detector should still ignore it if it ever were.
    query = "MATCH (n:Entity) WHERE n.name = 'Delete Corp' RETURN n"
    assert not _is_write(query)

    query_double_quoted = 'MATCH (n:Entity) WHERE n.name = "Merge Industries" RETURN n'
    assert not _is_write(query_double_quoted)


def test_comment_mentioning_write_keyword_is_ignored():
    query = """
        // TODO: eventually support DELETE here
        MATCH (n:Entity) RETURN n
    """
    assert not _is_write(query)


# ── _wrap: paths vs plain collections ─────────────────────────────────────────
#
# ArcadeDB serialises a path as an alternating [vertex, edge, vertex, …] list, so
# _wrap turns such a list into a _PathWrapper exposing .nodes/.relationships.
# A list of ONE kind is not a path, and wrapping it produced a _PathWrapper with
# no __iter__ — "'_PathWrapper' object is not iterable" the moment a caller looped
# over it. Both single-kind cases arise in real queries: nodes(path) returns only
# vertices, relationships(path) only edges.

from app.database import _wrap, _PathWrapper, _NodeWrapper  # noqa: E402


def _v(i):
    return {"@cat": "v", "@rid": f"#1:{i}", "id": f"n{i}"}


def _e(i):
    return {"@cat": "e", "@rid": f"#2:{i}", "stake_percent": i}


def test_alternating_vertex_edge_list_is_a_path():
    wrapped = _wrap([_v(1), _e(1), _v(2)])
    assert isinstance(wrapped, _PathWrapper)
    assert [n["id"] for n in wrapped.nodes] == ["n1", "n2"]
    assert len(wrapped.relationships) == 1


def test_vertex_only_list_stays_an_iterable_collection():
    # e.g. collect(DISTINCT node), or nodes(path)
    wrapped = _wrap([_v(1), _v(2)])
    assert not isinstance(wrapped, _PathWrapper)
    assert [n["id"] for n in wrapped] == ["n1", "n2"]


def test_edge_only_list_stays_an_iterable_collection():
    # e.g. relationships(path) — this one used to become a non-iterable _PathWrapper.
    wrapped = _wrap([_e(1), _e(2)])
    assert not isinstance(wrapped, _PathWrapper)
    assert [r["stake_percent"] for r in wrapped] == [1, 2]


def test_single_vertex_list_is_not_a_one_node_path():
    wrapped = _wrap([_v(1)])
    assert not isinstance(wrapped, _PathWrapper)
    assert len(list(wrapped)) == 1


def test_wrapped_documents_drop_arcadedb_metadata():
    node = _wrap(_v(1))
    assert isinstance(node, _NodeWrapper)
    assert "@rid" not in dict(node) and "@cat" not in dict(node)


def test_empty_list_is_just_an_empty_list():
    assert _wrap([]) == []
