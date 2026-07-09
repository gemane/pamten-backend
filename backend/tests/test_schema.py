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
    for vtype in ("Entity", "Person", "Location", "Source", "User"):
        assert f"CREATE VERTEX TYPE {vtype} IF NOT EXISTS" in issued


def test_creates_property_and_index_for_each_entry():
    with _run() as m:
        result = schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    # spot-check the hot-path lookup indexes
    assert any("CREATE INDEX IF NOT EXISTS ON Entity (name_normalized) NOTUNIQUE" == s for s in issued)
    assert any("CREATE PROPERTY Entity.wikidata_id STRING" == s for s in issued)
    assert result["skipped"] is False
    assert result["failed"] == []


def test_id_and_email_indexes_are_unique():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    assert "CREATE INDEX IF NOT EXISTS ON User (email) UNIQUE" in issued
    assert "CREATE INDEX IF NOT EXISTS ON Entity (id) UNIQUE" in issued


def test_is_idempotent_vertex_types_and_indexes_use_if_not_exists():
    with _run() as m:
        schema.ensure_indexes()
    issued = [c.args[0] for c in m.call_args_list]
    # VERTEX TYPE and INDEX statements use IF NOT EXISTS; PROPERTY statements do not
    for s in issued:
        if s.startswith("CREATE VERTEX TYPE") or s.startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in s, s
        elif s.startswith("CREATE PROPERTY"):
            assert "IF NOT EXISTS" not in s, s


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
