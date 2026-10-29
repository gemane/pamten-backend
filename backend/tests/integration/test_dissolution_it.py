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


def test_a_scrape_writing_onto_a_company_known_dissolved_leaves_nothing_open(it_db):
    """The rebuild of 2026-10-27 replayed the SEC scrapes after the chain's
    dissolution step: Altaba's Exhibit 21 came back open. The outermost scrape
    scope now applies the rule to every company it touched."""
    from app.scraper.graph_writer import _upsert_entity_by_name, _with_autodedup
    from app.scraper.sec_writer import _upsert_owns_sec
    it_db.run_command("CREATE (:Entity {id:'sec:0001011006', name:'Altaba', name_normalized:'altaba', "
                      "sec_cik:'0001011006', dissolved_on:'2019-10-04'})")

    @_with_autodedup
    def ex21_like():
        owner = _upsert_entity_by_name("Altaba", cik="0001011006", source_id="sec-edgar")
        sub = _upsert_entity_by_name("Altaba Holdings Hong Kong Limited", source_id="sec-edgar")
        _upsert_owns_sec(owner_id=owner, owned_id=sub, source_id="sec-edgar", stake_percent=100,
                         ownership_type="controlling", file_date="2019-03-01",
                         filing_type="EX-21", read_from="table",
                         source_url="https://www.sec.gov/Archives/edgar/data/1011006/x.htm")
        return {"status": "ok"}

    res = ex21_like()
    assert res["dissolution"] == {"closed": 1}
    e = it_db.run_sql("SELECT until, until_reason FROM OWNS WHERE @out.id = 'sec:0001011006'")
    assert [dict(r) for r in e] == [{"until": "2019-10-04", "until_reason": "dissolution"}]
    # the filing's claim is left as stated: it named no end
    assert [c["until"] for c in it_db.run_sql(
        "SELECT until FROM Claim WHERE kind = 'owns' AND from_id = 'sec:0001011006'")] == [None]
    # A re-scrape: the SEC writer's update states the filing's own `until`
    # (none), the scope closes the edge again — the end state is the same.
    assert ex21_like()["dissolution"] == {"closed": 1}
    e = it_db.run_sql("SELECT until, until_reason FROM OWNS WHERE @out.id = 'sec:0001011006'")
    assert [dict(r) for r in e] == [{"until": "2019-10-04", "until_reason": "dissolution"}]


def test_a_runner_that_only_resolves_its_company_leaves_nothing_open(it_db):
    """`run_sec_ex21` resolves its parent (`resolve_best_entity`) and never
    upserts it, so the parent was never touched: the 2026-10-28 rebuild left
    Altaba's 78 Exhibit 21 holdings open. Since 2026-10-29 every edge writer
    names its parties to the scope (`_record_edge_parties`)."""
    from app.scraper.graph_writer import _upsert_entity_by_name, _with_autodedup
    from app.scraper.sec_writer import _upsert_owns_sec
    it_db.run_command("CREATE (:Entity {id:'sec:0001011006', name:'Altaba', name_normalized:'altaba', "
                      "sec_cik:'0001011006', dissolved_on:'2019-10-04'})")

    @_with_autodedup
    def ex21_run():
        sub = _upsert_entity_by_name("Altaba Holdings Hong Kong Limited", source_id="sec-edgar")
        _upsert_owns_sec(owner_id="sec:0001011006", owned_id=sub, source_id="sec-edgar", stake_percent=100,
                         ownership_type="controlling", file_date="2019-03-01",
                         filing_type="EX-21", read_from="table",
                         source_url="https://www.sec.gov/Archives/edgar/data/1011006/x.htm")
        return {"status": "ok"}

    assert ex21_run()["dissolution"] == {"closed": 1}
    e = it_db.run_sql("SELECT until, until_reason FROM OWNS WHERE @out.id = 'sec:0001011006'")
    assert [dict(r) for r in e] == [{"until": "2019-10-04", "until_reason": "dissolution"}]


def _owns(it_db, a, b):
    rows = [dict(r) for r in it_db.run_sql(
        "SELECT source_id, until, until_reason FROM OWNS WHERE @out.id = :a AND @in.id = :b", {"a": a, "b": b})]
    return sorted(rows, key=lambda r: (r["until"] is None, r["until"] or ""))


def _seed_gone(it_db):
    it_db.run_command("CREATE (:Entity {id:'gone', name:'Gone Corp', name_normalized:'gone corp', "
                      "dissolved_on:'2019-10-04'})")
    it_db.run_command("CREATE (:Entity {id:'sub', name:'Sub', name_normalized:'sub'})")


def _sec_read(file_date):
    from app.scraper.graph_writer import _record_touched_entity, _with_autodedup
    from app.scraper.sec_writer import _upsert_owns_sec

    @_with_autodedup
    def scrape():
        _record_touched_entity("gone")
        _upsert_owns_sec(owner_id="gone", owned_id="sub", source_id="sec-edgar", stake_percent=100,
                         ownership_type="controlling", file_date=file_date, filing_type="EX-21")
        return {"status": "ok"}
    return scrape()


def test_a_filing_dated_after_the_end_is_its_own_spell(it_db):
    """The rule leaves an edge the source dates after the dissolution open
    (its error, reported); the writer does not fold it into the closed spell."""
    _seed_gone(it_db)
    _sec_read("2019-03-01")
    _sec_read("2020-02-01")
    assert _owns(it_db, "gone", "sub") == [
        {"source_id": "sec-edgar", "until": "2019-10-04", "until_reason": "dissolution"},
        {"source_id": "sec-edgar", "until": None, "until_reason": None}]


def test_another_sources_closed_spell_is_shared_not_drawn_again(it_db):
    _seed_gone(it_db)
    it_db.run_command("MATCH (a:Entity {id:'gone'}), (b:Entity {id:'sub'}) CREATE (a)-[:OWNS "
                      "{source_id:'gleif', credibility_score:92, until:'2019-10-04', "
                      "until_reason:'dissolution'}]->(b)")
    _sec_read("2019-03-01")
    _sec_read("2019-03-01")
    rows = _owns(it_db, "gone", "sub")
    assert len(rows) == 1 and rows[0]["until"] == "2019-10-04" and rows[0]["until_reason"] == "dissolution"
