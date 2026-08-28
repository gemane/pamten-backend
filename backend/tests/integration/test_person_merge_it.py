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

    # Blank bio field backfilled from the dup; wikidata_id retained; the dup's
    # name is captured as an alias so the kept person stays findable by it.
    keep = it_db.run_command("MATCH (p:Person {id:'keep'}) RETURN p.wikidata_id AS w, p.description AS d, p.alias AS a")[0]
    assert keep["w"] == "Q4934"
    assert keep["d"] == "SEC filer"     # keep's blank description filled from dup
    assert "Page Lawrence" in (keep["a"] or [])   # dup's name is now an alias
    assert "Larry Page" not in (keep["a"] or [])  # not the kept person's own name


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


# ── The fifth edge-recreate block (found 2026-08-28 validating a live dedup) ──
# merge_person_records kept its own list of 11 of the 25 OWNS properties and
# migrated no claims: the audit behind the edge schema found four recreate
# blocks, all in maintenance.py, and missed this one in the router.

def test_a_person_merge_carries_every_owns_property(it_db):
    from app.routers.persons import merge_person_records
    from app.scraper.edge_schema import OWNS_PROPS

    sample = {"stake_percent": 8.05, "voting_power_pct": 51.9,
              "ownership_type": "minority", "since": "2020-01-01", "until": None,
              "until_reason": "withdrawn", "source_id": "sec",
              "credibility_score": 98, "source_url": "https://example.test/f",
              "source_date": "2026-01-02", "last_scraped_at": "2026-08-28T00:00Z",
              "interest_types": ["voting-rights"], "direct_or_indirect": "direct",
              "psc_self_link": "/company/x/psc/1", "share_class": "Common Stock",
              "shares": 159121937, "shares_outstanding": 1965328900,
              "voting_shares": 1020598157, "stale": False, "shortcut": False,
              "also_ultimate": True, "ultimate_since": "2019-05-05",
              "ultimate_until": None, "value_usd": 1234.5, "file_date": "2026-01-02"}
    assert set(sample) == set(OWNS_PROPS), "keep the fixture in step with the schema"

    it_db.run_command("CREATE (:Person {id:'keep', full_name:'Warren Buffett', alias:[]})")
    it_db.run_command("CREATE (:Person {id:'dup', full_name:'Warren E Buffett', alias:[]})")
    it_db.run_command("CREATE (:Entity {id:'brk', name:'Berkshire Hathaway', type:'company'})")
    assigns = ", ".join(f"{k}: ${k}" for k in sample)
    it_db.run_command(
        f"MATCH (p:Person {{id:'dup'}}), (e:Entity {{id:'brk'}}) "
        f"CREATE (p)-[:OWNS {{{assigns}}}]->(e)", sample)

    merge_person_records("keep", "dup")

    rows = it_db.run_command(
        "MATCH (:Person {id:'keep'})-[r:OWNS]->(:Entity {id:'brk'}) RETURN "
        + ", ".join(f"r.{p} AS {p}" for p in OWNS_PROPS))
    assert len(rows) == 1
    lost = [p for p in OWNS_PROPS
            if sample[p] is not None and rows[0].get(p) != sample[p]]
    assert not lost, f"the merge dropped {lost}"


def test_a_person_merge_takes_the_claims_with_it(it_db):
    from app.claims import KIND_OWNS, claims_for, record_claim
    from app.routers.persons import merge_person_records

    it_db.run_command("CREATE (:Person {id:'keep', full_name:'Larry Page', alias:[]})")
    it_db.run_command("CREATE (:Person {id:'dup', full_name:'Page Lawrence', alias:[]})")
    it_db.run_command("CREATE (:Entity {id:'goog', name:'Alphabet Inc.', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'dup'}), (e:Entity {id:'goog'}) "
                      "CREATE (p)-[:OWNS {source_id:'sec'}]->(e)")
    record_claim(kind=KIND_OWNS, from_id="dup", to_id="goog", source_id="sec",
                 stake_percent=6.1, credibility_score=98)

    merge_person_records("keep", "dup")

    assert claims_for(from_id="dup", to_id="goog") == [], "left pointing at a deleted node"
    moved = claims_for(from_id="keep", to_id="goog")
    assert len(moved) == 1 and moved[0]["stake_percent"] == 6.1


def test_a_person_merge_keeps_voting_group_membership(it_db):
    """RELATED_TO carries `last_scraped_at` too — it was one of three properties
    the hand-written list knew about, and the schema knows all of them."""
    from app.routers.persons import merge_person_records

    it_db.run_command("CREATE (:Person {id:'keep', full_name:'Jorge Paulo Lemann', alias:[]})")
    it_db.run_command("CREATE (:Person {id:'dup', full_name:'Lemann Jorge Paulo', alias:[]})")
    it_db.run_command("CREATE (:Entity {id:'grp', name:'Voting group', type:'voting_group'})")
    it_db.run_command(
        "MATCH (p:Person {id:'dup'}), (g:Entity {id:'grp'}) CREATE (p)-[:RELATED_TO "
        "{relation:'group_member', source_id:'sec', last_scraped_at:'2026-08-28T00:00Z'}]->(g)")

    merge_person_records("keep", "dup")

    rows = it_db.run_command(
        "MATCH (:Person {id:'keep'})-[r:RELATED_TO]->(:Entity {id:'grp'}) "
        "RETURN r.relation AS rel, r.source_id AS sid, r.last_scraped_at AS seen")
    assert len(rows) == 1
    assert rows[0]["rel"] == "group_member" and rows[0]["sid"] == "sec"
    assert rows[0]["seen"] == "2026-08-28T00:00Z"
