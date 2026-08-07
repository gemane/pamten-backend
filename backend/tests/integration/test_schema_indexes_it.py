"""Real-ArcadeDB checks that the schema bootstrap creates what queries rely on.

`ensure_indexes()` is fault-tolerant by design: every statement is wrapped in its
own try/except so one failure never aborts the rest, and an unreachable database
is skipped with a warning. That is the right behaviour for a best-effort startup
hook, but it means a malformed DDL statement is swallowed — the app comes up
looking healthy while a query that should hit an index quietly full-scans instead.
Only a real database can tell us the index is actually there.
"""
import pytest

from app.db.arcadedb import run_sql
from app.db.schema import _INDEXES

pytestmark = pytest.mark.integration


def _indexed_properties() -> set[tuple[str, str]]:
    """(type, property) pairs ArcadeDB reports as indexed."""
    found: set[tuple[str, str]] = set()
    for row in run_sql("SELECT name FROM schema:indexes"):
        name = row.get("name") or ""
        # ArcadeDB names indexes "<Type>[<prop>]" (bucket suffixes may follow).
        if "[" in name and "]" in name:
            vtype = name.split("[", 1)[0].split("_")[0]
            prop = name.split("[", 1)[1].split("]", 1)[0]
            found.add((vtype, prop))
    return found


def test_country_and_type_are_indexed(it_db):
    """The map's drill-down filters Entity.country on every request, and the
    country-scoped search filters it too. Unindexed, each one scans every
    Entity — ~3M rows in the real database."""
    indexed = _indexed_properties()
    assert ("Entity", "country") in indexed
    assert ("Entity", "type") in indexed


def test_every_declared_index_actually_exists(it_db):
    """Catches the swallowed-DDL failure mode for the whole list at once, so a
    future addition with a typo cannot pass unnoticed."""
    indexed = _indexed_properties()
    missing = [(v, p) for v, p, _ in _INDEXES if (v, p) not in indexed]
    assert not missing, f"declared but not created: {missing}"


def test_a_country_filter_returns_the_right_rows(it_db):
    """An index that exists but is not used by the query would still be a
    regression, so exercise the actual filter."""
    for i, country in enumerate(["DE", "DE", "GB"]):
        run_sql(
            "INSERT INTO Entity SET id = :id, name = :n, country = :c, type = 'company'",
            {"id": f"e{i}", "n": f"Co {i}", "c": country},
        )
    rows = run_sql("SELECT count(*) AS n FROM Entity WHERE country = 'DE'")
    assert rows[0]["n"] == 2
