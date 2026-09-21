"""`app.db.paging` against a real ArcadeDB: every page it issues is planned as
an index range read, a prefix walk returns exactly the ids under that prefix
across page boundaries, and the two incremental updaters' guest lists come
out of it complete.

What this cannot show: the iterator crash a one-sided range triggers on the
full-size index (ArcadeDB 26.7.3, 14M rows), which is why the shape is pinned
in tests/test_paging.py as well — the plan is the same either way.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    for eid in ("lei:A1", "lei:A2", "lei:B1", "gb-coh:00000001", "gb-coh:SC899546",
                "chpsc:00031438:x", "wd:Q312", "lei;tricky", "lej:0"):
        it_db.run_command("CREATE (:Entity {id: $id, name: $id})", {"id": eid})
    it_db.run_command("CREATE (:Person {id: 'chpsc:p1', full_name: 'P One', alias: []})")


class TestPrefixWalk:
    def test_a_prefix_walk_returns_exactly_its_ids_across_pages(self, it_db):
        from app.db.paging import iter_id_pages

        _seed(it_db)
        pages = list(iter_id_pages("Entity", "lei:", page=2, columns="id"))
        assert [len(p) for p in pages] == [2, 1]
        assert [r["id"] for p in pages for r in p] == ["lei:A1", "lei:A2", "lei:B1"]
        # neighbours of the prefix stay out: 'lei;' is the ceiling, 'lej:' is past it
        gb = [r["id"] for p in iter_id_pages("Entity", "gb-coh:", page=10, columns="id") for r in p]
        assert gb == ["gb-coh:00000001", "gb-coh:SC899546"]

    def test_the_whole_space_walk_sees_every_vertex_of_the_type(self, it_db):
        from app.db.paging import iter_id_pages

        _seed(it_db)
        ids = [r["id"] for p in iter_id_pages("Entity", page=4) for r in p]
        assert len(ids) == 9 and ids == sorted(ids)
        assert [r["id"] for p in iter_id_pages("Person", page=4) for r in p] == ["chpsc:p1"]

    def test_every_page_is_an_index_range_read(self, it_db):
        """Pinned against the real planner: FETCH FROM INDEX, never FETCH
        FROM TYPE — the difference between 0.1 s and a 6 GB scan per page."""
        from app.db import paging

        _seed(it_db)
        issued: list[str] = []
        real = paging.run_sql

        def spy(cmd, *a, **k):
            issued.append(cmd)
            return real(cmd, *a, **k)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(paging, "run_sql", spy)
            list(paging.iter_id_pages("Entity", "lei:", page=2))
            list(paging.iter_id_pages("Person", page=2))
        assert len(issued) >= 4
        for page in issued:
            plan = it_db.explain_sql(page)
            assert plan.startswith("+ FETCH FROM INDEX"), (page, plan)
            assert "FETCH FROM TYPE" not in plan, (page, plan)


class TestTheGuestLists:
    def test_existing_lei_ids_and_company_ids_come_from_the_index_walk(self, it_db, monkeypatch):
        from app.scraper import ch_psc_incremental, gleif_incremental

        _seed(it_db)
        monkeypatch.setattr(gleif_incremental, "_LEI_ID_PAGE", 2)
        monkeypatch.setattr(ch_psc_incremental, "_LEI_ID_PAGE", 1)
        assert gleif_incremental.existing_lei_ids() == {"lei:A1", "lei:A2", "lei:B1"}
        assert ch_psc_incremental.existing_company_ids() == {"gb-coh:00000001", "gb-coh:SC899546"}
