"""Tests for the schema/index bootstrap (SQL layer mocked)."""

from unittest.mock import patch

from app.db import schema


def _run(side_effect=None):
    """Patch run_sql, returning the mock that recorded the issued statements."""
    return patch.object(schema, "run_sql", side_effect=side_effect)


def test_creates_every_vertex_type_once():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    for vtype in ("Entity", "Person", "Source", "User"):
        assert f"CREATE VERTEX TYPE {vtype} IF NOT EXISTS" in issued


def test_creates_property_and_index_for_each_entry():
    with _run() as m:
        result = schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    # spot-check the hot-path lookup indexes
    assert any("CREATE INDEX IF NOT EXISTS ON Entity (name_normalized) NOTUNIQUE" == s for s in issued)
    assert any("CREATE PROPERTY Entity.wikidata_id IF NOT EXISTS STRING" == s for s in issued)
    assert result["skipped"] is False
    assert result["failed"] == []


def test_id_and_email_indexes_are_unique():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE INDEX IF NOT EXISTS ON User (email) UNIQUE" in issued
    assert "CREATE INDEX IF NOT EXISTS ON Entity (id) UNIQUE" in issued


def test_is_idempotent_all_ddl_uses_if_not_exists():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    # Every DDL statement is idempotent — VERTEX TYPE, EDGE TYPE, PROPERTY and
    # INDEX all use IF NOT EXISTS, so a re-run (startup, bulk-load rebuild) logs
    # no "already exists" failures. For PROPERTY it must sit BEFORE the type.
    for s in issued:
        assert "IF NOT EXISTS" in s, s
        if s.startswith("CREATE PROPERTY"):
            assert s.endswith("IF NOT EXISTS STRING"), s


def test_continues_and_records_failures():
    # fail only the User.email index; everything else should still run
    def side(stmt, *a, **k):
        if "ON User (email) UNIQUE" in stmt and stmt.startswith("CREATE INDEX"):
            raise RuntimeError("duplicate keys")
    with _run(side_effect=side):
        result = schema.ensure_indexes()
    assert result["skipped"] is False
    assert len(result["failed"]) == 1
    assert "ON User (email) UNIQUE" in result["failed"][0]["stmt"]
    assert result["ok"]  # the rest applied


def test_unreachable_db_is_skipped_without_raising():
    with _run(side_effect=ConnectionError("refused")):
        result = schema.ensure_indexes()
    assert result["skipped"] is True
    # bailed on the very first statement, no exception propagated
    assert result["failed"] == []


def test_the_psc_edge_index_is_declared():
    """The Companies House refresh matches an edge by `psc_self_link`, in batches,
    then updates by the same key. Both work unindexed — by scanning every OWNS edge
    per batch — so nothing *fails* without the index, it just takes hours instead of
    minutes. Asserted on the emitted DDL, because a test that loops over the
    declaration list passes happily when the list is empty."""
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE PROPERTY OWNS.psc_self_link IF NOT EXISTS STRING" in issued
    assert "CREATE INDEX IF NOT EXISTS ON OWNS (psc_self_link) NOTUNIQUE" in issued


def test_an_edge_index_does_not_create_a_vertex_type():
    """`_INDEXES`'s first element drives `CREATE VERTEX TYPE`, which is exactly why
    edge indexes live in their own list. Declaring OWNS in the wrong one would make
    the bootstrap try to create a vertex type shadowing the edge type."""
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE VERTEX TYPE OWNS IF NOT EXISTS" not in issued
    assert "CREATE EDGE TYPE OWNS IF NOT EXISTS" in issued
