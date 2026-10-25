"""Real-ArcadeDB: the twin heals and the writers' fold (app.field_twins).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_the_heals_bring_an_older_database_to_one_field(it_db):
    from app.field_twins import heal_all

    # the three person shapes the 2026-10-25 survey found, and an entity twin
    it_db.run_command("CREATE (:Person {id:'chpsc:01:a', full_name:'A', nationality:'GB', nationalities:[]})")
    it_db.run_command("CREATE (:Person {id:'p2', full_name:'B', nationality:'US', nationalities:['US','CA'], source_id:'wikidata'})")
    it_db.run_command("CREATE (:Person {id:'p3', full_name:'C', nationalities:['DE'], source_id:'sec-edgar'})")
    it_db.run_command("CREATE (:Entity {id:'e1', name:'E1', name_normalized:'e1', country:'NL', countries:[]})")
    it_db.run_command("CREATE (:Entity {id:'e2', name:'E2', name_normalized:'e2', country:'US', countries:['US']})")
    # an empty list where no source named a country: absent after the heal
    it_db.run_command("CREATE (:Person {id:'p4', full_name:'D', nationalities:[], source_id:'sec-edgar'})")
    it_db.run_command("CREATE (:Entity {id:'e3', name:'E3', name_normalized:'e3', countries:[]})")
    it_db.run_command("MATCH (a:Entity {id:'e1'}), (b:Entity {id:'e2'}) CREATE (a)-[:OWNS {until:null}]->(b)")
    it_db.run_command("MATCH (a:Entity {id:'e2'}), (b:Entity {id:'e1'}) CREATE (a)-[:OWNS {until:null, stale:true}]->(b)")
    # `OWNS.stale` defaults to false since #430, so a fresh edge is never unset;
    # the older database this heal exists for is made by clearing the flag after
    # the write (an UPDATE to null leaves null — the default applies to new rows)
    it_db.run_command("MATCH (a:Entity {id:'e1'})-[r:OWNS]->(b:Entity {id:'e2'}) SET r.stale = null")
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS WHERE stale IS NULL")[0]["n"] == 1

    dry = heal_all(apply=False)
    # the dry run counts every empty list as it stands — the two the fold will
    # fill and the two nothing names; the apply below runs the fold first
    assert dry["empty_lists"] == {"applied": False, "Person.nationalities": 2, "Entity.countries": 2}
    assert dry["nationalities"]["dropped"] == 2 and dry["nationalities"]["folded"] == 1
    assert dry["countries"]["fixed"] == 1 and dry["person_sources"]["stamped"] == 1 and dry["stale_flags"]["fixed"] == 1
    assert it_db.run_sql("SELECT nationality FROM Person WHERE id = 'chpsc:01:a'")[0]["nationality"] == "GB"   # untouched

    res = heal_all(apply=True)
    assert res["nationalities"]["dropped"] == 2 and res["countries"]["fixed"] == 1
    assert res["person_sources"]["stamped"] == 1 and res["stale_flags"]["fixed"] == 1
    rows = {r["id"]: r for r in it_db.run_sql("SELECT id, nationalities, source_id FROM Person")}
    assert rows["chpsc:01:a"]["nationalities"] == ["GB"] and rows["chpsc:01:a"]["source_id"] == "uk-psc"
    assert rows["p2"]["nationalities"] == ["US", "CA"] and rows["p3"]["nationalities"] == ["DE"]
    assert it_db.run_sql("SELECT count(*) AS n FROM Person WHERE nationality IS NOT NULL")[0]["n"] == 0
    assert it_db.run_sql("SELECT countries FROM Entity WHERE id = 'e1'")[0]["countries"] == ["NL"]
    assert res["empty_lists"]["Person.nationalities"] == 1 and res["empty_lists"]["Entity.countries"] == 1
    assert "nationalities" not in it_db.run_sql("SELECT FROM Person WHERE id = 'p4'")[0]
    assert "countries" not in it_db.run_sql("SELECT FROM Entity WHERE id = 'e3'")[0]
    assert it_db.run_sql("SELECT FROM Entity WHERE id = 'e2'")[0]["countries"] == ["US"]      # a filled list stays
    stale = it_db.run_sql("SELECT stale, count(*) AS n FROM OWNS GROUP BY stale")
    assert sorted((r["stale"], r["n"]) for r in stale) == [(False, 1), (True, 1)]
    # idempotent
    again = heal_all(apply=True)
    assert again["nationalities"]["dropped"] == 0 and again["countries"]["fixed"] == 0
    assert again["person_sources"]["stamped"] == 0 and again["stale_flags"]["fixed"] == 0
    assert again["empty_lists"]["Person.nationalities"] == 0


def test_the_writers_fold_the_scalar_into_the_list(it_db):
    from app.scraper.runner import _upsert_person, _upsert_entity

    pid = _upsert_person("Larry Page", nationality="US", description="", wikidata_id="Q1")
    row = it_db.run_sql("SELECT nationalities, nationality FROM Person WHERE id = :id", {"id": pid})[0]
    assert row["nationalities"] == ["US"] and row.get("nationality") is None
    # a later source with a list adds, a blank-fill never blanks
    _upsert_person("Larry Page", nationality=None, description="", wikidata_id="Q1", nationalities=["CA"])
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["US"]

    eid = _upsert_entity("Heineken N.V.", "company", "NL", None, None, "", "Q2",
                         countries=[], source_id="wikidata")
    assert it_db.run_sql("SELECT countries FROM Entity WHERE id = :id", {"id": eid})[0]["countries"] == ["NL"]


def test_the_writers_leave_an_unknown_list_absent(it_db):
    """What no source stated is absent, not `[]` — on the Wikidata person and
    entity writers, the name-keyed person writer and the PSC person."""
    from app.scraper.graph_writer import _upsert_person_by_name
    from app.scraper.runner import _upsert_entity, _upsert_person

    pid = _upsert_person("Nobody Known", nationality=None, description="", wikidata_id="Q9")
    assert "nationalities" not in it_db.run_sql("SELECT FROM Person WHERE id = :id", {"id": pid})[0]
    _upsert_person("Nobody Known", nationality="FR", description="", wikidata_id="Q9")      # learned later: filled
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["FR"]
    _upsert_person("Nobody Known", nationality=None, description="", wikidata_id="Q9")      # a blank never blanks
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["FR"]

    nid = _upsert_person_by_name("Jane Form", source_id="sec-edgar")
    assert "nationalities" not in it_db.run_sql("SELECT FROM Person WHERE id = :id", {"id": nid})[0]

    eid = _upsert_entity("Stateless Ltd", "company", None, None, None, "", "Q8", countries=[], source_id="wikidata")
    assert "countries" not in it_db.run_sql("SELECT FROM Entity WHERE id = :id", {"id": eid})[0]
    _upsert_entity("Stateless Ltd", "company", "IE", None, None, "", "Q8", source_id="wikidata")
    assert it_db.run_sql("SELECT countries FROM Entity WHERE id = :id", {"id": eid})[0]["countries"] == ["IE"]
