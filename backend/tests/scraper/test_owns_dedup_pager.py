"""The OWNS dedup collector's owner pages are index reads from the first page on.

`SELECT … FROM Entity ORDER BY id LIMIT n` with no predicate is planned by
ArcadeDB as FETCH FROM TYPE — a full scan — while `WHERE id > ''` is FETCH FROM
INDEX (tests/integration/test_dedup_owns_edges_it.py proves that against the
real planner). On the sizing box the predicate-less first page scanned 6.4 GB
and timed out before the walk had begun.
"""
from unittest.mock import patch


def _collect(pages):
    """Drive _owns_pairs_with_rids with canned owner pages; return the SQL issued."""
    from app.scraper import maintenance
    issued: list[str] = []
    queue = list(pages)

    def run_sql(cmd, *a, **k):
        issued.append(cmd)
        if cmd.startswith("SELECT id, @rid AS rid FROM"):
            return queue.pop(0) if queue else []
        return []
    with patch.object(maintenance, "_OWNER_PAGE", 2), \
         patch.object(maintenance, "run_sql", side_effect=run_sql):
        maintenance._owns_pairs_with_rids()
    return [c for c in issued if c.startswith("SELECT id, @rid AS rid FROM")]


def test_the_first_page_carries_the_predicate_too():
    pages = _collect([[{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],
                      [{"id": "c", "rid": "#1:2"}]])
    assert pages[0].startswith("SELECT id, @rid AS rid FROM Entity WHERE id > '' ORDER BY id")
    assert "WHERE id > 'b'" in pages[1]
    # Person starts from the empty string again
    assert any(p.startswith("SELECT id, @rid AS rid FROM Person WHERE id > '' ") for p in pages)
    assert all("WHERE id > '" in p for p in pages)


def test_a_quote_in_the_last_id_is_escaped():
    pages = _collect([[{"id": "o'brien", "rid": "#1:0"}, {"id": "p", "rid": "#1:1"}],
                      [{"id": "q", "rid": "#1:2"}]])
    assert "WHERE id > 'p'" in pages[1]
    pages = _collect([[{"id": "a", "rid": "#1:0"}, {"id": "o'brien", "rid": "#1:1"}], []])
    assert "WHERE id > 'o\\'brien'" in pages[1]
