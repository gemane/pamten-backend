"""A person merge re-keys the survivor, against a real ArcadeDB (findings
item 39). Wikidata wrote `wd:Q912811` "Brian Moynihan"; EDGAR spelled him
"Brian T Moynihan" and the SEC writer created `sec:0001195071`. The auto-dedup
folds the SEC twin into the Wikidata node, which takes the CIK — and, since
2026-11-04, the `sec:` id, as a `wd:` person met under the SAME spelling has
had since 2026-10-30. The forwarding stays one hop: the twin's own row is
deleted by the rename (its id is live again), `wd:…→sec:…` is written."""
import pytest

from app.database import db
from app.db.arcadedb import run_sql
from app.merged_ids import resolve_current_id
from app.routers.persons import deduplicate_high_confidence, merge_person_records, merge_persons
from app.models.person import PersonMergeRequest

pytestmark = pytest.mark.integration

WD, SEC, BOFA = "wd:Q912811", "sec:0001195071", "lei:9DJT3UXIJIZJI4WXO774"


def _twins(it_db):
    it_db.run_command(f"CREATE (:Person {{id:'{WD}', full_name:'Brian Moynihan', wikidata_id:'Q912811', "
                      "source_id:'wikidata', aliases:['Brian T. Moynihan']})")
    it_db.run_command(f"CREATE (:Person {{id:'{SEC}', full_name:'Brian T Moynihan', sec_cik:'0001195071', "
                      "source_id:'sec-edgar', aliases:[]})")
    it_db.run_command(f"CREATE (:Entity {{id:'{BOFA}', name:'BANK OF AMERICA CORPORATION', type:'company', "
                      "name_normalized:'bank of america'})")
    it_db.run_command(f"MATCH (p:Person {{id:'{WD}'}}), (e:Entity {{id:'{BOFA}'}}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', source_id:'wikidata'}]->(e)")
    it_db.run_command(f"MATCH (p:Person {{id:'{SEC}'}}), (e:Entity {{id:'{BOFA}'}}) "
                      "CREATE (p)-[:OWNS {stake_percent:0.01, filing_type:'4', source_id:'sec-edgar'}]->(e)")


def _person(pid):
    return [dict(r) for r in run_sql("SELECT id, wikidata_id, sec_cik, full_name FROM Person WHERE id = :i", {"i": pid})]


def test_the_wikidata_survivor_that_takes_a_cik_becomes_sec(it_db):
    _twins(it_db)
    assert merge_person_records(keep=WD, dup=SEC) == SEC
    assert _person(WD) == []
    assert _person(SEC) == [{"id": SEC, "wikidata_id": "Q912811", "sec_cik": "0001195071", "full_name": "Brian Moynihan"}]
    # both relationships hang off the one node
    rows = it_db.run_command(f"MATCH (p:Person {{id:'{SEC}'}})-[r]->(e:Entity) RETURN type(r) AS t ORDER BY t")
    assert [r["t"] for r in rows] == ["HAS_ROLE", "OWNS"]


def test_the_forwarding_is_one_hop_and_the_live_id_is_not_redirected(it_db):
    _twins(it_db)
    merge_person_records(keep=WD, dup=SEC)
    with db.get_session() as session:
        assert resolve_current_id(session, WD) == SEC
        # the twin's id is the survivor's id now: no row may forward it away
        assert resolve_current_id(session, SEC) is None
    assert [dict(r) for r in run_sql("SELECT old_id, new_id FROM MergedId")] == [{"old_id": WD, "new_id": SEC}]


def test_the_auto_dedup_reports_the_id_the_person_lives_under(it_db):
    _twins(it_db)
    res = deduplicate_high_confidence(apply=True)
    assert res["merged_count"] == 1
    assert res["merged"][0]["keep_id"] == SEC
    assert _person(WD) == [] and len(_person(SEC)) == 1


def test_the_merge_endpoint_answers_with_the_new_id(it_db):
    _twins(it_db)
    out = merge_persons(PersonMergeRequest(keep_id=WD, dup_id=SEC), _={"role": "contributor"})
    assert out["keep_id"] == SEC and out["removed_id"] == SEC


def test_a_survivor_without_a_hard_id_keeps_its_id(it_db):
    it_db.run_command("CREATE (:Person {id:'keep-1', full_name:'Larry Page'})")
    it_db.run_command("CREATE (:Person {id:'dup-1', full_name:'Page Lawrence'})")
    assert merge_person_records(keep="keep-1", dup="dup-1") == "keep-1"
    with db.get_session() as session:
        assert resolve_current_id(session, "dup-1") == "keep-1"


def test_a_sec_survivor_absorbing_a_wikidata_twin_keeps_sec(it_db):
    """The other way round: the CIK already keys the survivor, the QID ranks below."""
    _twins(it_db)
    assert merge_person_records(keep=SEC, dup=WD) == SEC
    assert _person(SEC) == [{"id": SEC, "wikidata_id": "Q912811", "sec_cik": "0001195071", "full_name": "Brian T Moynihan"}]
