"""
Real-ArcadeDB integration test for merging duplicate persons: the duplicate's
relationships (with their edge properties) must move onto the kept person, blank
bio fields must backfill, and the duplicate must be gone. Exercises the
CREATE ... SET nr += properties(r) / DETACH DELETE Cypher the mocks can't check.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_merge_rehomes_edges_and_backfills_then_deletes_dup(it_db):
    from app.routers.persons import merge_persons
    from app.routers.search import get_person_profile
    from app.models.person import PersonMergeRequest

    # keep = canonical (Wikidata) with a wikidata_id but no edges.
    it_db.run_command("CREATE (:Person {id:'keep', full_name:'Larry Page', wikidata_id:'Q4934', description:''})")
    # dup = SEC artifact: no wikidata_id, but holds the real ownership fact.
    it_db.run_command("CREATE (:Person {id:'dup', full_name:'Page Lawrence', description:'SEC filer'})")
    it_db.run_command("CREATE (:Entity {id:'alphabet', name:'Alphabet Inc.', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'dup'}), (e:Entity {id:'alphabet'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 6.12, ownership_type:'minority'}]->(e)")

    merge_persons(PersonMergeRequest(keep_id="keep", dup_id="dup"), _={"role": "contributor"})

    # The duplicate is gone.
    assert it_db.run_command("MATCH (p:Person {id:'dup'}) RETURN p.id AS id") == []

    # The ownership fact now hangs off the kept person, with its stake preserved.
    prof = get_person_profile("keep")
    holdings = {(h["entity"]["name"], h["relationship"]["stake_percent"]) for h in prof["holdings"]}
    assert ("Alphabet Inc.", 6.12) in holdings

    # Blank bio field backfilled from the dup; wikidata_id retained.
    keep = it_db.run_command("MATCH (p:Person {id:'keep'}) RETURN p.wikidata_id AS w, p.description AS d")[0]
    assert keep["w"] == "Q4934"
    assert keep["d"] == "SEC filer"     # keep's blank description filled from dup


def test_merge_folds_onto_existing_edge_and_backfills_stake(it_db):
    """keep already owns the company (blank stake); dup owns it with a real stake.
    Merge must fold onto the single existing edge and backfill the stake — not
    create a duplicate. (This is the shape used to repair a bad earlier merge.)"""
    from app.routers.persons import merge_persons
    from app.routers.search import get_person_profile
    from app.models.person import PersonMergeRequest

    it_db.run_command("CREATE (:Person {id:'keep', full_name:'Larry Page'})")
    it_db.run_command("CREATE (:Person {id:'dup',  full_name:'Page Lawrence'})")
    it_db.run_command("CREATE (:Entity {id:'alphabet', name:'Alphabet Inc.', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'keep'}), (e:Entity {id:'alphabet'}) CREATE (p)-[:OWNS {}]->(e)")           # blank
    it_db.run_command("MATCH (p:Person {id:'dup'}),  (e:Entity {id:'alphabet'}) CREATE (p)-[:OWNS {stake_percent: 6.12, ownership_type:'minority'}]->(e)")

    merge_persons(PersonMergeRequest(keep_id="keep", dup_id="dup"), _={"role": "contributor"})

    holdings = get_person_profile("keep")["holdings"]
    assert len(holdings) == 1                                   # folded, not duplicated
    assert holdings[0]["relationship"]["stake_percent"] == 6.12  # blank backfilled
    assert holdings[0]["relationship"]["ownership_type"] == "minority"


def test_merge_same_id_rejected(it_db):
    from app.routers.persons import merge_persons
    from app.models.person import PersonMergeRequest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        merge_persons(PersonMergeRequest(keep_id="x", dup_id="x"), _={"role": "contributor"})
    assert exc.value.status_code == 400


def test_merge_missing_person_404(it_db):
    from app.routers.persons import merge_persons
    from app.models.person import PersonMergeRequest
    from fastapi import HTTPException

    it_db.run_command("CREATE (:Person {id:'solo', full_name:'Solo'})")
    with pytest.raises(HTTPException) as exc:
        merge_persons(PersonMergeRequest(keep_id="solo", dup_id="ghost"), _={"role": "contributor"})
    assert exc.value.status_code == 404
