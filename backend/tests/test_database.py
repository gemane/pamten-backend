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
