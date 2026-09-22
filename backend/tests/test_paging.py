"""`app.db.paging` — the id-index walk that survives a full-size database.

The three properties pinned here were learned on the 34 GB sizing database
and cannot be reproduced on a test one (see the module docstring); the
integration test pins the query PLAN against the real planner, this pins the
SHAPE and the walk's control flow.
"""
from unittest.mock import patch

import pytest

from app.db import paging
from app.db.paging import ID_CEILING, id_page_sql, iter_id_pages, prefix_ceiling


class TestPrefixCeiling:
    def test_bumps_the_last_character(self):
        assert prefix_ceiling("lei:") == "lei;"
        assert prefix_ceiling("gb-coh:") == "gb-coh;"
        assert prefix_ceiling("chpsc:") == "chpsc;"

    def test_the_empty_prefix_is_the_whole_space(self):
        assert prefix_ceiling("") == ID_CEILING

    def test_every_id_shape_sorts_below_its_ceiling(self):
        for prefix, sample in (("lei:", "lei:5493003BZYYYCDIO0R13"), ("gb-coh:", "gb-coh:SC899546"),
                               ("chpsc:", "chpsc:NI018146:RA0LFEayIFQJ"), ("", "wd:Q312"),
                               ("", "f47ac10b-58cc-4372-a567-0e02b2c3d479"), ("", "~tilde")):
            assert prefix < sample < prefix_ceiling(prefix) or prefix == "" and sample < ID_CEILING
        # and a neighbouring prefix is outside the range
        assert not ("lei:" < "lej:0" < prefix_ceiling("lei:"))


class TestPageSql:
    def test_both_bounds_and_order_on_every_page(self):
        sql = id_page_sql("Entity", "lei:", "lei;", 5000)
        assert sql == ("SELECT id, @rid AS rid FROM Entity WHERE id > 'lei:' AND id < 'lei;' "
                       "ORDER BY id LIMIT 5000")

    def test_columns_are_the_callers(self):
        assert id_page_sql("Person", "", ID_CEILING, 10, columns="id").startswith("SELECT id FROM Person")

    def test_quotes_and_backslashes_in_the_cursor_are_escaped(self):
        sql = id_page_sql("Entity", "o'brien", "x\\y", 1)
        assert "WHERE id > 'o\\'brien' AND id < 'x\\\\y'" in sql


def _walk(pages, **kw):
    issued: list[str] = []
    queue = list(pages)

    def run_sql(cmd, *a, **k):
        issued.append(cmd)
        return queue.pop(0) if queue else []
    with patch.object(paging, "run_sql", side_effect=run_sql):
        out = list(iter_id_pages("Entity", **kw))
    return issued, out


class TestIterIdPages:
    def test_starts_at_the_prefix_and_advances_the_cursor(self):
        issued, out = _walk([[{"id": "lei:a"}, {"id": "lei:b"}], [{"id": "lei:c"}], []],
                            prefix="lei:", page=2, columns="id")
        assert issued[0] == "SELECT id FROM Entity WHERE id > 'lei:' AND id < 'lei;' ORDER BY id LIMIT 2"
        assert "WHERE id > 'lei:b' AND id < 'lei;'" in issued[1]
        assert "WHERE id > 'lei:c' AND id < 'lei;'" in issued[2]
        assert out == [[{"id": "lei:a"}, {"id": "lei:b"}], [{"id": "lei:c"}]]

    def test_a_short_page_does_not_end_the_walk(self):
        """LIMIT counts stale index entries: a page can be short long before
        the index ends. Only an empty page ends it."""
        issued, out = _walk([[{"id": "a"}], [{"id": "b"}, {"id": "c"}], [{"id": "d"}], []], page=2)
        assert len(issued) == 4 and [len(p) for p in out] == [1, 2, 1]

    def test_the_whole_space_is_bounded_by_the_ceiling(self):
        issued, _ = _walk([[]])
        assert f"WHERE id > '' AND id < '{ID_CEILING}'" in issued[0]

    def test_no_page_is_ever_one_sided(self):
        issued, _ = _walk([[{"id": "a"}], [{"id": "b"}], []], page=1)
        assert all(" AND id < '" in c and "WHERE id > '" in c for c in issued)

    def test_an_empty_first_page_yields_nothing_and_asks_once(self):
        issued, out = _walk([[]], prefix="zz:")
        assert out == [] and len(issued) == 1

    def test_the_cursor_needs_the_id_column(self):
        with pytest.raises(KeyError):
            _walk([[{"rid": "#1:0"}], []], columns="@rid AS rid")
