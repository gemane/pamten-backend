"""The one-off model heals of 2026-10-24 (app.scraper.maintenance): the typeless
entities and the never-true `verified` flag. The id-index walk is faked; the
real walk is covered by tests/integration/test_id_paging_it.py."""
from unittest.mock import patch

from app.scraper import maintenance as m


def _pages(rows, size=2):
    return [rows[i:i + size] for i in range(0, len(rows), size)]


class TestHealEntityTypes:
    ROWS = [{"id": "lei:1", "rid": "#1:0", "type": None},
            {"id": "lei:2", "rid": "#1:1", "type": "fund"},
            {"id": "lei:3", "rid": "#1:2", "type": ""},
            {"id": "wd:4", "rid": "#1:3", "type": "company"},
            {"id": "x:5", "rid": "#1:4", "type": None}]

    def test_only_the_typeless_become_company(self):
        writes = []
        with patch("app.db.paging.iter_id_pages", lambda label, **k: iter(_pages(self.ROWS))), \
             patch.object(m, "run_sql", side_effect=lambda sql, *a, **k: writes.append(sql) or []):
            res = m.heal_entity_types(apply=True)
        assert res == {"scanned": 5, "matched": 3, "updated": 3, "applied": True}
        # one write per page that had a hit (pages of two here), by @rid
        assert writes == ["UPDATE [#1:0] SET type = 'company'",
                          "UPDATE [#1:2] SET type = 'company'",
                          "UPDATE [#1:4] SET type = 'company'"]

    def test_a_dry_run_counts_and_writes_nothing(self):
        with patch("app.db.paging.iter_id_pages", lambda label, **k: iter(_pages(self.ROWS))), \
             patch.object(m, "run_sql") as run_sql:
            res = m.heal_entity_types(apply=False)
        assert res["matched"] == 3 and res["updated"] == 0 and res["applied"] is False
        run_sql.assert_not_called()

    def test_the_walk_asks_for_the_rid_and_the_type(self):
        seen = {}

        def fake_pages(label, **k):
            seen.update(label=label, **k)
            return iter([])
        with patch("app.db.paging.iter_id_pages", fake_pages):
            m.heal_entity_types(apply=True)
        assert seen["label"] == "Entity" and seen["columns"] == "id, @rid AS rid, type"


class TestDropVerifiedFlag:
    def test_both_labels_lose_the_property_where_it_is_set(self):
        rows = {"Entity": [{"id": "e1", "rid": "#2:0", "verified": False},
                           {"id": "e2", "rid": "#2:1", "verified": None}],
                "Person": [{"id": "p1", "rid": "#3:0", "verified": True}]}
        writes = []
        with patch("app.db.paging.iter_id_pages", lambda label, **k: iter(_pages(rows[label]))), \
             patch.object(m, "run_sql", side_effect=lambda sql, *a, **k: writes.append(sql) or []):
            res = m.drop_verified_flag(apply=True)
        assert res["Entity"]["matched"] == 1 and res["Person"]["matched"] == 1
        assert writes == ["UPDATE [#2:0] REMOVE verified", "UPDATE [#3:0] REMOVE verified"]
