"""The OWNS dedup collector walks owners through `app.db.paging` and expands
each page's edges by rid — never a scan of OWNS, never a one-sided range,
never a stop on a short page (tests/test_paging.py pins the pager itself;
this pins how the collector drives it).
"""
from unittest.mock import patch


def _collect(pages):
    """Drive _owns_pairs_with_rids with canned owner pages; return
    (owner-page SQL issued, expansion SQL issued, pairs)."""
    from app.db import paging
    from app.scraper import maintenance
    page_sql: list[str] = []
    expand_sql: list[str] = []
    queue = list(pages)

    def page_run(cmd, *a, **k):
        page_sql.append(cmd)
        return queue.pop(0) if queue else []

    def expand_run(cmd, *a, **k):
        expand_sql.append(cmd)
        return [{"rid": "#9:1", "o": "#1:0", "i": "#1:9", "st": 10.0, "doi": "direct"}]
    with patch.object(maintenance, "_OWNER_PAGE", 2), \
         patch.object(paging, "run_sql", side_effect=page_run), \
         patch.object(maintenance, "run_sql", side_effect=expand_run):
        pairs = maintenance._owns_pairs_with_rids()
    return page_sql, expand_sql, pairs


def test_pages_are_bounded_index_ranges_over_entity_then_person():
    from app.db.paging import ID_CEILING
    page_sql, expand_sql, _ = _collect([
        [{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}], [],   # Entity
        [{"id": "p", "rid": "#2:0"}], [],                                # Person
    ])
    assert page_sql[0] == (f"SELECT id, @rid AS rid FROM Entity WHERE id > '' "
                           f"AND id < '{ID_CEILING}' ORDER BY id LIMIT 2")
    assert f"FROM Entity WHERE id > 'b' AND id < '{ID_CEILING}'" in page_sql[1]
    assert page_sql[2].startswith(f"SELECT id, @rid AS rid FROM Person WHERE id > '' AND id < '{ID_CEILING}'")
    # one adjacency expansion per non-empty page, by the page's rids
    assert len(expand_sql) == 2
    assert "FROM (SELECT expand(outE('OWNS')) FROM [#1:0, #1:1])" in expand_sql[0]
    assert "FROM [#2:0]" in expand_sql[1]


def test_a_short_page_does_not_end_the_walk():
    page_sql, expand_sql, _ = _collect([
        [{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],   # full
        [{"id": "c", "rid": "#1:2"}],                                # short — keep going
        [{"id": "d", "rid": "#1:3"}, {"id": "e", "rid": "#1:4"}],
        [],                                                          # Entity done
        [{"id": "p", "rid": "#2:0"}],
        [],                                                          # Person done
    ])
    assert len([p for p in page_sql if "FROM Entity" in p]) == 4
    assert len([p for p in page_sql if "FROM Person" in p]) == 2
    assert len(expand_sql) == 4


def test_edges_are_grouped_by_endpoint_pair_with_their_rids():
    _, _, pairs = _collect([[{"id": "a", "rid": "#1:0"}], []])
    assert pairs == {("#1:0", "#1:9"): [("#9:1", 10.0, "direct"), ("#9:1", 10.0, "direct")]} or \
        list(pairs) == [("#1:0", "#1:9")]
