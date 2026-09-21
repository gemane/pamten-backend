"""The OWNS dedup collector's owner pages: bounded index reads that stop only
on an empty page.

Three properties of the page query were learned on the 34 GB sizing database
and are invisible on a small one (tests/integration/test_dedup_owns_edges_it.py
pins the plan against the real planner; the iterator bug below cannot be
reproduced on a test database at all):

* a lower bound from the FIRST page on — without a predicate `ORDER BY id
  LIMIT n` is planned as FETCH FROM TYPE, a full scan (6.4 GB, timed out);
* an upper bound too — ArcadeDB 26.7.3 answered every one-sided ascending
  range over that index with `NullPointerException: "convertedKeys" is null`;
  the two-sided range read 5,000 rows in 0.1–0.3 s;
* a short page is not the end — `LIMIT` counts index entries including stale
  ones, and the first real page held 3,664 of 5,000 rows.
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
        pairs = maintenance._owns_pairs_with_rids()
    return [c for c in issued if c.startswith("SELECT id, @rid AS rid FROM")], pairs


def test_every_page_is_bounded_on_both_sides_from_the_first():
    from app.scraper.maintenance import _ID_CEILING
    pages, _ = _collect([[{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}], []])
    assert pages[0] == (f"SELECT id, @rid AS rid FROM Entity WHERE id > '' "
                        f"AND id < '{_ID_CEILING}' ORDER BY id LIMIT 2")
    assert f"WHERE id > 'b' AND id < '{_ID_CEILING}'" in pages[1]
    # Person starts from the empty string again, bounded the same way
    assert any(p.startswith(f"SELECT id, @rid AS rid FROM Person WHERE id > '' AND id < '{_ID_CEILING}' ")
               for p in pages)
    assert all(f"AND id < '{_ID_CEILING}' ORDER BY id LIMIT 2" in p for p in pages)


def test_the_ceiling_sorts_after_every_id_shape():
    from app.scraper.maintenance import _ID_CEILING
    for sample in ("lei:5493003BZYYYCDIO0R13", "gb-coh:SC899546", "chpsc:NI018146:RA0LFEayIFQJ",
                   "wd:Q312", "f47ac10b-58cc-4372-a567-0e02b2c3d479", "~tilde", "zzz"):
        assert sample < _ID_CEILING


def test_a_short_page_does_not_end_the_walk():
    """Only an empty page does: LIMIT counts stale index entries, so a page
    can come back short long before the index is exhausted."""
    pages, _ = _collect([
        [{"id": "a", "rid": "#1:0"}, {"id": "b", "rid": "#1:1"}],   # full
        [{"id": "c", "rid": "#1:2"}],                                # short — keep going
        [{"id": "d", "rid": "#1:3"}, {"id": "e", "rid": "#1:4"}],   # more after it
        [],                                                          # Entity done
        [{"id": "p", "rid": "#2:0"}],                                # Person: short
        [],                                                          # Person done
    ])
    entity_pages = [p for p in pages if "FROM Entity" in p]
    person_pages = [p for p in pages if "FROM Person" in p]
    assert len(entity_pages) == 4 and len(person_pages) == 2
    assert "WHERE id > 'c' " in entity_pages[2]
    assert "WHERE id > 'e' " in entity_pages[3]


def test_a_quote_in_the_last_id_is_escaped():
    pages, _ = _collect([[{"id": "a", "rid": "#1:0"}, {"id": "o'brien", "rid": "#1:1"}], []])
    assert "WHERE id > 'o\\'brien' " in pages[1]
