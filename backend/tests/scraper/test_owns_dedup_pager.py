"""The OWNS dedup collector walks owners through `app.db.paging`, expands each
page's edges by rid, and STREAMS: pairs are grouped per page (a duplicate is
two edges from one owner, so they are always on the same page) and losers are
deleted as pages come in. The global dict this used to build was 1.34 GB at
2.56M pairs on the full import, heading past the 8 GB box.
(tests/test_paging.py pins the pager itself; this pins how the collector
drives it.)
"""
import inspect
from unittest.mock import patch


def _drive(pages, fn, batch_size=None, edges_for=None):
    """Run `fn` with canned owner pages. Returns (owner-page SQL, expansion SQL,
    delete scripts, the interleaved call order, fn's result)."""
    from app.db import paging
    from app.scraper import maintenance
    page_sql: list[str] = []
    expand_sql: list[str] = []
    deletes: list[str] = []
    order: list[str] = []
    queue = list(pages)

    def page_run(cmd, *a, **k):
        page_sql.append(cmd)
        order.append("page")
        return queue.pop(0) if queue else []

    def expand_run(cmd, *a, **k):
        expand_sql.append(cmd)
        order.append("expand")
        return edges_for(cmd) if edges_for else [
            {"rid": "#9:1", "o": "#1:0", "i": "#1:9", "st": 10.0, "doi": "direct"}]

    def delete_run(script, *a, **k):
        deletes.append(script)
        order.append("delete")
        return []
    with patch.object(maintenance, "_OWNER_PAGE", 2), \
         patch.object(paging, "run_sql", side_effect=page_run), \
         patch.object(maintenance, "run_sql", side_effect=expand_run), \
         patch.object(maintenance, "run_sqlscript", side_effect=delete_run):
        result = fn() if batch_size is None else fn(batch_size=batch_size)
    return page_sql, expand_sql, deletes, order, result


def test_the_collector_is_a_generator_not_an_accumulator():
    from app.scraper.maintenance import _owns_pairs_by_page
    assert inspect.isgeneratorfunction(_owns_pairs_by_page)


def test_pages_are_bounded_index_ranges_over_entity_then_person():
    from app.db.paging import ID_CEILING
    from app.scraper.maintenance import count_duplicate_owns_edges
    page_sql, expand_sql, _, _, res = _drive([
        [{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}], [],   # Entity
        [{"id": "p", "rid": "#2:0"}], [],                                # Person
    ], count_duplicate_owns_edges)
    assert page_sql[0] == (f"SELECT id, @rid AS rid FROM Entity WHERE id > '' "
                           f"AND id < '{ID_CEILING}' ORDER BY id LIMIT 2")
    assert f"FROM Entity WHERE id > 'b' AND id < '{ID_CEILING}'" in page_sql[1]
    assert page_sql[2].startswith(f"SELECT id, @rid AS rid FROM Person WHERE id > '' AND id < '{ID_CEILING}'")
    # one adjacency expansion per non-empty page, by the page's rids
    assert len(expand_sql) == 2
    assert "FROM (SELECT expand(outE('OWNS')) FROM [#1:0, #1:1])" in expand_sql[0]
    assert "FROM [#2:0]" in expand_sql[1]
    assert res == {"active_edges": 2, "distinct_pairs": 2, "duplicate_pairs": 0, "redundant_edges": 0,
                   "closed_duplicate_pairs": 0}


def test_a_short_page_does_not_end_the_walk():
    from app.scraper.maintenance import count_duplicate_owns_edges
    page_sql, expand_sql, _, _, _ = _drive([
        [{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],   # full
        [{"id": "c", "rid": "#1:2"}],                                # short — keep going
        [{"id": "d", "rid": "#1:3"}, {"id": "e", "rid": "#1:4"}],
        [],                                                          # Entity done
        [{"id": "p", "rid": "#2:0"}],
        [],                                                          # Person done
    ], count_duplicate_owns_edges)
    assert len([p for p in page_sql if "FROM Entity" in p]) == 4
    assert len([p for p in page_sql if "FROM Person" in p]) == 2
    assert len(expand_sql) == 4


def _edges_by_page(cmd):
    """Page 1's owners #1:0/#1:1 each own #1:9 twice; page 2's #1:2 owns it once."""
    if "[#1:0, #1:1]" in cmd:
        return [{"rid": "#9:1", "o": "#1:0", "i": "#1:9", "st": 10.0, "doi": "direct"},
                {"rid": "#9:2", "o": "#1:0", "i": "#1:9", "st": 5.0, "doi": None},
                {"rid": "#9:3", "o": "#1:1", "i": "#1:9", "st": None, "doi": "indirect"},
                {"rid": "#9:4", "o": "#1:1", "i": "#1:9", "st": None, "doi": "direct"}]
    return [{"rid": "#9:5", "o": "#1:2", "i": "#1:9", "st": 1.0, "doi": None}]


def test_counts_stream_across_pages():
    from app.scraper.maintenance import count_duplicate_owns_edges
    _, _, _, _, res = _drive([[{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],
                              [{"id": "c", "rid": "#1:2"}], [], []],
                             count_duplicate_owns_edges, edges_for=_edges_by_page)
    assert res == {"active_edges": 5, "distinct_pairs": 3, "duplicate_pairs": 2, "redundant_edges": 2,
                   "closed_duplicate_pairs": 0}


def test_losers_are_deleted_as_pages_come_in_not_after_the_walk():
    """batch_size=1: page 1's two losers must be deleted BEFORE page 2 is even
    fetched — the whole point of streaming."""
    from app.scraper.maintenance import deduplicate_owns_edges
    _, _, deletes, order, res = _drive([[{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],
                                        [{"id": "c", "rid": "#1:2"}], [], []],
                                       deduplicate_owns_edges, batch_size=1, edges_for=_edges_by_page)
    assert res == {"duplicates_removed": 2, "pairs_cleaned": 2, "closed_pairs_cleaned": 0, "survivors_folded": 0}
    # the survivor is the larger stake (#9:1) and the direct-flagged twin (#9:4)
    assert deletes == ["DELETE FROM #9:2", "DELETE FROM #9:3"]
    assert order.index("delete") < order.index("page", order.index("expand") + 1)


def _edges_with_history(cmd):
    """Page 1: #1:0 → #1:9 has one active edge and TWO copies of a holding that
    ended 2020-05-01 (a bulk re-import beside the old edge), plus one that ended
    2012-01-01 — a separate interval, not a duplicate. #1:1 → #1:9 ended twice
    on different days. Page 2: a plain active edge."""
    if "[#1:0, #1:1]" in cmd:
        return [{"rid": "#9:1", "o": "#1:0", "i": "#1:9", "st": 10.0, "doi": None, "u": None},
                {"rid": "#9:2", "o": "#1:0", "i": "#1:9", "st": 10.0, "doi": None, "u": "2020-05-01"},
                {"rid": "#9:3", "o": "#1:0", "i": "#1:9", "st": 10.0, "doi": None, "u": "2020-05-01"},
                {"rid": "#9:4", "o": "#1:0", "i": "#1:9", "st": 10.0, "doi": None, "u": "2012-01-01"},
                {"rid": "#9:5", "o": "#1:1", "i": "#1:9", "st": None, "doi": None, "u": "2018-01-01"},
                {"rid": "#9:6", "o": "#1:1", "i": "#1:9", "st": None, "doi": None, "u": "2022-01-01"}]
    return [{"rid": "#9:7", "o": "#1:2", "i": "#1:9", "st": 1.0, "doi": None, "u": None}]


_PAGES_WITH_HISTORY = [[{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],
                       [{"id": "c", "rid": "#1:2"}], [], []]


def test_the_expansion_reads_every_edge_and_groups_closed_ones_by_end_date():
    """A closed holding re-imported beside its old copy is a duplicate just like
    an active twin; two holdings that ended on different days are history."""
    from app.scraper.maintenance import count_duplicate_owns_edges
    _, expand_sql, _, _, res = _drive(_PAGES_WITH_HISTORY, count_duplicate_owns_edges,
                                      edges_for=_edges_with_history)
    assert "WHERE until IS NULL" not in expand_sql[0] and "until AS u" in expand_sql[0]
    assert res == {"active_edges": 2, "distinct_pairs": 2,      # the current graph
                   "duplicate_pairs": 1, "redundant_edges": 1,   # the 2020-05-01 twins only
                   "closed_duplicate_pairs": 1}


def test_a_closed_twin_is_folded_away_and_distinct_intervals_stay():
    from app.scraper.maintenance import deduplicate_owns_edges
    _, _, deletes, _, res = _drive(_PAGES_WITH_HISTORY, deduplicate_owns_edges,
                                   batch_size=2000, edges_for=_edges_with_history)
    assert res == {"duplicates_removed": 1, "pairs_cleaned": 1, "closed_pairs_cleaned": 1,
                   "survivors_folded": 0}
    assert deletes == ["DELETE FROM #9:3"]       # #9:2 survives; #9:4/#9:5/#9:6 untouched


def test_largest_owners_counts_current_subsidiaries_only():
    """The digest's count must not grow with a holder's past: #1:0's three
    closed edges to #1:9 are one current subsidiary, #1:1's two are none."""
    from app.scraper.maintenance import largest_owners

    def expand_or_names(cmd, *a, **k):
        if "SELECT @rid AS rid, id, name" in cmd:
            return [{"rid": "#1:0", "id": "a", "name": "A"}, {"rid": "#1:2", "id": "c", "name": "C"}]
        return _edges_with_history(cmd)
    _, _, _, _, res = _drive(_PAGES_WITH_HISTORY, lambda: largest_owners(0),
                             edges_for=expand_or_names)
    assert [(r["id"], r["subsidiaries"]) for r in res] == [("a", 1), ("c", 1)]


def test_deletes_are_batched_and_flushed_at_the_end():
    from app.scraper.maintenance import deduplicate_owns_edges
    _, _, deletes, _, res = _drive([[{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],
                                    [{"id": "c", "rid": "#1:2"}], [], []],
                                   deduplicate_owns_edges, batch_size=2000, edges_for=_edges_by_page)
    assert res["duplicates_removed"] == 2
    assert deletes == ["DELETE FROM #9:2;DELETE FROM #9:3"]   # under the batch: one script at the end
