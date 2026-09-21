"""The bulk-load index cycle against a real ArcadeDB: drop, load, rebuild.

The unit tests prove the rebuild issues one long CREATE per dropped index and
survives a failure; this proves the statements are ones ArcadeDB accepts, that
the catalog reads back complete afterwards, and that /search's CONTAINSTEXT
finds a row written while the search index was down. The first full import
ended with 12 of 15 indexes missing — a shape only a real catalog can show.
"""
import pytest

pytestmark = pytest.mark.integration


def _catalog(it_db) -> set[str]:
    return {r["name"] for r in it_db.run_sql("SELECT name FROM schema:indexes")}


class TestDropLoadRebuild:
    def test_the_catalog_is_whole_again_and_search_works(self, it_db):
        from app.scraper.bulk_import import (_bulk_load_secondary_indexes,
                                             _drop_secondary_indexes, _rebuild_indexes)

        dropped = set(_bulk_load_secondary_indexes())
        assert dropped <= _catalog(it_db), "the bootstrap should have created them all"

        _drop_secondary_indexes()
        assert not (dropped & _catalog(it_db))

        # rows written while the indexes are down — what a bulk load does
        it_db.run_command(
            "CREATE (:Entity {id:'e1', name:'Nestlé S.A.', name_normalized:'nestle', "
            "search_text:'Nestlé S.A. nestle', country:'CH'})")
        it_db.run_command(
            "CREATE (:Person {id:'p1', full_name:'Ulf Mark Schneider', "
            "search_text:'Ulf Mark Schneider', alias:['U. M. Schneider']})")

        res = _rebuild_indexes()

        assert res["failed"] == []
        assert set(res["ok"]) == dropped
        assert dropped <= _catalog(it_db)
        # the rebuilt search index covers the rows loaded while it was down
        hits = it_db.run_sql("SELECT id FROM Entity WHERE search_text CONTAINSTEXT 'nestle'")
        assert [h["id"] for h in hits] == ["e1"]
        hits = it_db.run_sql("SELECT id FROM Person WHERE search_text CONTAINSTEXT 'schneider'")
        assert [h["id"] for h in hits] == ["p1"]
        # and a rebuilt LSM index answers an indexed lookup
        rows = it_db.run_sql("SELECT id FROM Person WHERE full_name = 'Ulf Mark Schneider'")
        assert [r["id"] for r in rows] == ["p1"]

    def test_running_it_twice_is_harmless(self, it_db):
        from app.scraper.bulk_import import _drop_secondary_indexes, _rebuild_indexes

        _drop_secondary_indexes()
        first = _rebuild_indexes()
        second = _rebuild_indexes()
        assert first["failed"] == [] and second["failed"] == []
        # nothing was dropped the second time, so nothing needed rebuilding
        assert second["ok"] == []
