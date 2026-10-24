"""Real-ArcadeDB: the founded / incorporated_on heal (app.incorporation).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_the_heal_renames_and_clears_only_the_copied_year(it_db):
    from app.incorporation import heal

    it_db.run_sql("INSERT INTO Entity SET id = 'barclays', name = 'B', founded = 1690, founded_date = '1896-07-20', wikidata_id = 'Q245343'")
    it_db.run_sql("INSERT INTO Entity SET id = 'plain', name = 'P', founded = 2013, founded_date = '2013-03-05'")
    it_db.run_sql("INSERT INTO Entity SET id = 'wd', name = 'W', founded = 2013, founded_date = '2013-03-05', wikidata_id = 'Q1'")
    it_db.run_sql("INSERT INTO Entity SET id = 'done', name = 'D', incorporated_on = '2001-02-03', founded = 2001")
    it_db.run_sql("INSERT INTO Entity SET id = 'wiki-only', name = 'O', founded = 1975")

    dry = heal(apply=False)
    assert (dry["renamed"], dry["cleared_founded"], dry["written"]) == (3, 2, 0)
    assert it_db.run_sql("SELECT founded_date FROM Entity WHERE id = 'plain'")[0]["founded_date"] == "2013-03-05"

    res = heal(apply=True)
    assert (res["renamed"], res["dropped_old_field"], res["cleared_founded"], res["written"]) == (3, 3, 2, 4)
    rows = {r["id"]: r for r in it_db.run_sql("SELECT id, founded, founded_date, incorporated_on FROM Entity")}
    assert rows["barclays"]["incorporated_on"] == "1896-07-20" and rows["barclays"]["founded"] == 1690
    assert rows["plain"]["incorporated_on"] == "2013-03-05" and rows["plain"]["founded"] is None
    assert rows["wd"]["incorporated_on"] == "2013-03-05" and rows["wd"]["founded"] == 2013
    assert rows["done"]["founded"] is None and rows["done"]["incorporated_on"] == "2001-02-03"
    assert rows["wiki-only"]["founded"] == 1975
    assert all(r["founded_date"] is None for r in rows.values())
    assert heal(apply=True)["written"] == 0                       # idempotent
