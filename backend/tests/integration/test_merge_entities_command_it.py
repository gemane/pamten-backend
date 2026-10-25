"""Real-ArcadeDB: `manage.py merge-entities` folds one entity into another by
hand — the survivor's values win, its gaps fill from the loser, edges and
claims move, a forwarding address is left, and a keep-separate mark refuses it.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    it_db.run_command("CREATE (:Entity {id:'sec:tv', name:'GRUPO TELEVISA, S.A.B.', name_normalized:'grupo televisa sab', "
                      "country:'MX', sec_cik:'0000912892', type:'company', description:'from EDGAR'})")
    it_db.run_command("CREATE (:Entity {id:'lei:tv', name:'GRUPO TELEVISA S A B', name_normalized:'grupo televisa sab', "
                      "country:'MX', lei_id:'549300O4AF0C816ILN62', legal_form:'Sociedad Anonima Bursatil', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'sub', name:'izzi', name_normalized:'izzi', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'p', full_name:'Francisco Valim'})")
    it_db.run_command("MATCH (a:Entity {id:'lei:tv'}), (b:Entity {id:'sub'}) CREATE (a)-[:OWNS {source_id:'gleif', stake_percent: 100, until:null}]->(b)")
    it_db.run_command("MATCH (p:Person {id:'p'}), (e:Entity {id:'sec:tv'}) CREATE (p)-[:HAS_ROLE {role:'Officer', source_id:'sec-edgar'}]->(e)")
    it_db.run_sql("INSERT INTO Claim SET claim_key = 'c1', kind = 'owns', from_id = 'lei:tv', to_id = 'sub', source_id = 'gleif'")


def test_dry_run_reports_and_apply_folds_the_loser_into_the_survivor(it_db):
    from app.scraper.maintenance import merge_entities_by_id
    _seed(it_db)
    res = merge_entities_by_id("sec:tv", "lei:tv")
    assert res["applied"] is False and res["edges"] == 1 and res["claims"] == 1
    assert "lei_id" in res["carried"] and "legal_form" in res["carried"] and "name" not in res["carried"]
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 3        # a dry run changes nothing

    res = merge_entities_by_id("sec:tv", "lei:tv", apply=True)
    assert res["applied"] is True and res["migrated"] >= 1
    [keep] = it_db.run_sql("SELECT id, name, sec_cik, lei_id, legal_form, description, aliases FROM Entity WHERE id = 'sec:tv'")
    assert keep["name"] == "GRUPO TELEVISA, S.A.B." and keep["sec_cik"] == "0000912892"     # the survivor's own
    assert keep["lei_id"] == "549300O4AF0C816ILN62" and keep["legal_form"] == "Sociedad Anonima Bursatil"  # the loser's filled
    assert "GRUPO TELEVISA S A B" in (keep["aliases"] or [])
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE id = 'lei:tv'")[0]["n"] == 0
    assert it_db.run_sql("SELECT @out.id AS o FROM OWNS")[0]["o"] == "sec:tv"                 # the edge moved
    assert it_db.run_sql("SELECT from_id FROM Claim")[0]["from_id"] == "sec:tv"               # the claim re-keyed
    assert it_db.run_sql("SELECT new_id FROM MergedId WHERE old_id = 'lei:tv'")[0]["new_id"] == "sec:tv"
    assert it_db.run_sql("SELECT kind, dup_id FROM MergeLog")[0]["dup_id"] == "lei:tv"


def test_a_keep_separate_mark_and_a_wrong_id_are_refused(it_db):
    from app.scraper.maintenance import merge_entities_by_id
    _seed(it_db)
    it_db.run_command("MATCH (a:Entity {id:'sec:tv'}), (b:Entity {id:'lei:tv'}) CREATE (a)-[:NOT_DUPLICATE {at:'2026-10-25T00:00:00+00:00'}]->(b)")
    with pytest.raises(ValueError, match="keep-separate"):
        merge_entities_by_id("sec:tv", "lei:tv", apply=True)
    with pytest.raises(ValueError, match="no such entity: nope"):
        merge_entities_by_id("sec:tv", "nope")
    with pytest.raises(ValueError, match="same"):
        merge_entities_by_id("sec:tv", "sec:tv")
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 3
