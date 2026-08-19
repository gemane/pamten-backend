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
from app.db.schema import _EDGE_INDEXES, _INDEXES

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


def test_every_declared_edge_index_actually_exists(it_db):
    """Edge indexes go through a separate code path from the vertex ones.

    `_INDEXES` drives `CREATE VERTEX TYPE`, so an edge cannot be declared there —
    `_EDGE_INDEXES` exists for that, and being a second path it is a second thing
    that can silently fail. `ensure_indexes()` swallows DDL errors by design.
    """
    assert _EDGE_INDEXES, "nothing declared — this test would pass on an empty list"
    indexed = _indexed_properties()
    missing = [(e, p) for e, p, _ in _EDGE_INDEXES if (e, p) not in indexed]
    assert not missing, f"declared but not created: {missing}"


def test_a_psc_edge_is_findable_by_its_link(it_db):
    """The query the Companies House refresh is built on.

    It matches a changed snapshot record to its edge with
    `WHERE psc_self_link IN :links`, in batches of ~1000, then updates by the same
    key. Both need the property queryable on an *edge* type — which is not
    something ArcadeDB's SQL can do through an edge's endpoints, so this is the
    mechanism the whole design depends on. Unindexed it still answers, by scanning
    every OWNS edge per batch; the index is what makes a nightly run minutes
    rather than hours.
    """
    run_sql("INSERT INTO Person SET id = 'p-psc', full_name = 'Ann Owner'")
    for i, link in enumerate(["/company/1/psc/individual/aaa", "/company/2/psc/individual/bbb"]):
        run_sql("INSERT INTO Entity SET id = :id, name = :n", {"id": f"co{i}", "n": f"Co {i}"})
        run_sql("CREATE EDGE OWNS FROM (SELECT FROM Person WHERE id = 'p-psc') "
                "TO (SELECT FROM Entity WHERE id = :cid) "
                "SET psc_self_link = :link, stake_percent = :pct",
                {"cid": f"co{i}", "link": link, "pct": 75 - i * 50})

    hit = run_sql("SELECT psc_self_link FROM OWNS WHERE psc_self_link IN :links",
                  {"links": ["/company/1/psc/individual/aaa", "/company/9/psc/individual/zzz"]})
    assert [r["psc_self_link"] for r in hit] == ["/company/1/psc/individual/aaa"]

    # …and an update keyed on it touches exactly that edge, not its sibling.
    run_sql("UPDATE OWNS SET until = '2026-01-31' WHERE psc_self_link = :link",
            {"link": "/company/1/psc/individual/aaa"})
    rows = run_sql("SELECT psc_self_link, until FROM OWNS ORDER BY psc_self_link")
    assert [(r["psc_self_link"], r["until"]) for r in rows] == [
        ("/company/1/psc/individual/aaa", "2026-01-31"),
        ("/company/2/psc/individual/bbb", None),
    ]
