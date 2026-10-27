"""Real-ArcadeDB: an entity has an end (app.dissolution, 2026-10-25) — the
Wikidata writer stores the day and marks the company inactive, nothing of a
dissolved company stays open, the heal catches an older database, the
timeline tells it, and the server accepts the new seat-end reason.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _edges(it_db, cid):
    out = {}
    for edge in ("OWNS", "HAS_ROLE"):
        for r in it_db.run_sql(
                f"SELECT @out.id AS a, @in.id AS b, role, until, until_reason, until_source_url FROM {edge} "
                "WHERE @out.id = :c OR @in.id = :c", {"c": cid}):
            out[(r["a"], r["b"], r.get("role"))] = r
    return out


def _seed(it_db):
    it_db.run_command("CREATE (:Entity {id:'x', name:'Gone Corp', name_normalized:'gone corp', "
                      "dissolved_on:'2023-03-15', wikidata_id:'Q9'})")
    for eid, name in (("p", "Parent"), ("s", "Sub"), ("t", "Late Sub")):
        it_db.run_command("CREATE (:Entity {id:$id, name:$n, name_normalized:$n})", {"id": eid, "n": name})
    it_db.run_command("CREATE (:Person {id:'ceo', full_name:'Ann Chief'})")
    it_db.run_command("CREATE (:Person {id:'fdr', full_name:'Fred Founder'})")
    it_db.run_command("MATCH (a:Entity {id:'p'}), (b:Entity {id:'x'}) "
                      "CREATE (a)-[:OWNS {since:'2010-01-01', until:null, source_id:'gleif'}]->(b)")
    it_db.run_command("MATCH (a:Entity {id:'x'}), (b:Entity {id:'s'}) "
                      "CREATE (a)-[:OWNS {until:null, source_id:'gleif'}]->(b)")
    it_db.run_command("MATCH (a:Entity {id:'x'}), (b:Entity {id:'t'}) "
                      "CREATE (a)-[:OWNS {since:'2024-01-01', until:null, source_id:'gleif'}]->(b)")
    it_db.run_command("MATCH (p:Person {id:'ceo'}), (e:Entity {id:'x'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', since:'2015-01-01', source_id:'wikidata'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'fdr'}), (e:Entity {id:'x'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'Founder', source_id:'wikidata'}]->(e)")
    it_db.run_command("CREATE (:Claim {claim_key:'k1', kind:'role', from_id:'ceo', to_id:'x', source_id:'wikidata', role:'CEO'})")


def test_nothing_of_a_dissolved_company_stays_open(it_db):
    from app.dissolution import end_at_dissolution
    _seed(it_db)
    assert end_at_dissolution("x") == {"closed": 3, "founders_kept": 1, "after_dissolution": 1}
    e = _edges(it_db, "x")
    for key in (("p", "x", None), ("x", "s", None), ("ceo", "x", "CEO")):
        assert e[key]["until"] == "2023-03-15" and e[key]["until_reason"] == "dissolution", key
        assert e[key]["until_source_url"] == "https://www.wikidata.org/wiki/Q9"
    assert e[("x", "t", None)]["until"] is None            # dated after the end: the source's error, reported
    assert e[("fdr", "x", "Founder")]["until"] is None      # a founder is not a seat that ends
    # the source's claim is left as stated: it named no end
    assert it_db.run_sql("SELECT until FROM Claim WHERE claim_key = 'k1'")[0]["until"] is None
    assert end_at_dissolution("x")["closed"] == 0           # idempotent


def test_the_wikidata_writer_stores_the_day_and_applies_the_rule(it_db):
    from app.scraper.runner import _upsert_entity
    it_db.run_command("CREATE (:Entity {id:'owner', name:'Owner', name_normalized:'owner'})")
    gid = _upsert_entity("Compaq", None, None, None, "Q1", source_id="wikidata")
    row = it_db.run_sql("SELECT active, dissolved_on FROM Entity WHERE id = :id", {"id": gid})[0]
    assert row.get("active") is None and row.get("dissolved_on") is None     # a knowledge base asserts neither
    it_db.run_command("MATCH (a:Entity {id:'owner'}), (b:Entity {id:$g}) "
                      "CREATE (a)-[:OWNS {since:'1999-01-01', until:null, source_id:'wikidata'}]->(b)", {"g": gid})
    _upsert_entity("Compaq", None, None, None, "Q1", source_id="wikidata", dissolved_on="2002-00-00")
    row = it_db.run_sql("SELECT active, dissolved_on FROM Entity WHERE id = :id", {"id": gid})[0]
    assert row["active"] is False and row["dissolved_on"] == "2002-00-00"      # the statement's precision
    held = it_db.run_sql("SELECT until, until_reason FROM OWNS WHERE @in.id = :g", {"g": gid})[0]
    assert (held["until"], held["until_reason"]) == ("2002-00-00", "dissolution")
    # fill-only: a later, different date does not move it
    _upsert_entity("Compaq", None, None, None, "Q1", source_id="wikidata", dissolved_on="2003-01-01")
    assert it_db.run_sql("SELECT dissolved_on FROM Entity WHERE id = :id", {"id": gid})[0]["dissolved_on"] == "2002-00-00"


def test_the_heal_closes_what_was_written_before_the_rule(it_db):
    from app.dissolution import heal_ends_at_dissolution
    _seed(it_db)
    it_db.run_command("CREATE (:Entity {id:'live', name:'Live Co', name_normalized:'live co'})")
    it_db.run_command("MATCH (a:Entity {id:'live'}), (b:Entity {id:'s'}) "
                      "CREATE (a)-[:OWNS {until:null, source_id:'gleif'}]->(b)")
    dry = heal_ends_at_dissolution(apply=False)
    assert (dry["companies"], dry["closed"], dry["founders_kept"], dry["after_dissolution"]) == (1, 3, 1, 1)
    assert dry["lines"] == ["Gone Corp († 2023-03-15): 3 closed, 1 founders kept, 1 dated after the dissolution"]
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS WHERE until IS NOT NULL")[0]["n"] == 0
    res = heal_ends_at_dissolution(apply=True)
    assert res["closed"] == 3
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS WHERE until_reason = 'dissolution'")[0]["n"] == 2
    assert it_db.run_sql("SELECT until FROM OWNS WHERE @out.id = 'live'")[0]["until"] is None
    assert heal_ends_at_dissolution(apply=True)["closed"] == 0


def test_the_timeline_ends_with_the_dissolution(it_db):
    from app.routers.relationships import ownership_history_of
    _seed(it_db)
    events, _ = ownership_history_of("x")
    end = [e for e in events if e["kind"] == "dissolved"]
    assert end == [{"kind": "dissolved", "since": "2023-03-15", "until": None, "active": False, "party": None}]
    assert events[0]["kind"] in ("dissolved", "ownership_out"), "dated events newest first; the end is among the last days"
    events, _ = ownership_history_of("p")
    assert not [e for e in events if e["kind"] == "dissolved"]


def test_the_server_accepts_the_seat_end_reason(it_db):
    it_db.run_command("CREATE (:Person {id:'q', full_name:'Q'})")
    it_db.run_command("CREATE (:Entity {id:'e', name:'E', name_normalized:'e'})")
    it_db.run_command("MATCH (p:Person {id:'q'}), (e:Entity {id:'e'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', until:'2020-01-01', until_reason:'dissolution'}]->(e)")
    with pytest.raises(Exception) as exc:
        it_db.run_command("MATCH (p:Person {id:'q'}), (e:Entity {id:'e'}) "
                          "CREATE (p)-[:HAS_ROLE {role:'CFO', until:'2020-01-01', until_reason:'left'}]->(e)")
    assert "does not match" in str(exc.value)
    with pytest.raises(Exception):
        it_db.run_command("CREATE (:Entity {id:'bad', name:'Bad', dissolved_on:'15/03/2023'})")   # the date shape
