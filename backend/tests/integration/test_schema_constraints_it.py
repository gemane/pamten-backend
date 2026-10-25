"""Real-ArcadeDB: the rules the bootstrap declares beyond a property's type
(`schema.constraints()`), and what the server does with them.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _rules(it_db):
    return {(t["name"], p["name"]): p
            for t in it_db.run_sql("SELECT name, properties FROM schema:types")
            for p in (t.get("properties") or [])}


def _refused(it_db, cypher, params=None):
    with pytest.raises(Exception) as exc:
        it_db.run_command(cypher, params or {})
    return str(exc.value)


def test_the_bootstrap_declares_the_rules_once(it_db):
    from app.db.schema import ensure_indexes
    rules = _rules(it_db)                       # it_db ran ensure_indexes()
    assert rules[("Entity", "id")]["mandatory"] is True and rules[("Entity", "id")]["notNull"] is True
    assert rules[("Entity", "country")]["regexp"] == "^[A-Z]{2}$"
    assert rules[("Entity", "type")]["regexp"].startswith("^(company|")
    assert rules[("OWNS", "stake_percent")]["min"] == "0" and rules[("OWNS", "stake_percent")]["max"] == "100"
    assert rules[("OWNS", "read_from")]["regexp"] == "^(field|table|form|layout|prose|narrative)$"
    assert rules[("OWNS", "stale")]["default"] == "false"
    assert rules[("OWNS", "shortcut")]["default"] == "false"
    assert rules[("Entity", "is_nominee")]["default"] == "false"
    assert rules[("Person", "on_demand_scraped")]["default"] == "false"
    assert rules[("Entity", "active")].get("default") is None    # three-valued on purpose
    assert rules[("Claim", "claim_key")]["mandatory"] is True
    assert rules[("Claim", "source_id")]["mandatory"] is True and rules[("Claim", "source_id")]["notNull"] is True
    assert rules[("OWNS", "since")]["regexp"] == "^[0-9]{4}(-[0-9]{2}){0,2}$"
    assert rules[("Person", "birth_date")]["regexp"] == rules[("OWNS", "since")]["regexp"]
    assert rules[("Claim", "last_seen_at")]["regexp"].startswith("^[0-9]{4}-[0-9]{2}-[0-9]{2}T")
    assert ensure_indexes().get("complete") is True          # nothing left to issue


def test_the_server_refuses_what_breaks_a_rule_and_leaves_nulls_alone(it_db):
    ok = "CREATE (:Entity {id:'a', name:'A', name_normalized:'a', country:'US', type:'company'})"
    it_db.run_command(ok)
    # nulls pass every rule but notnull: a country unknown is allowed
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', name_normalized:'b', country:null})")
    assert "does not match the regular expression" in _refused(
        it_db, "CREATE (:Entity {id:'c', name:'C', name_normalized:'c', country:'USA'})")
    assert "does not match the regular expression" in _refused(
        it_db, "CREATE (:Entity {id:'d', name:'D', name_normalized:'d', type:'bank'})")
    assert "mandatory" in _refused(it_db, "CREATE (:Entity {name:'no id', name_normalized:'no id'})")
    it_db.run_command("MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) "
                      "CREATE (a)-[:OWNS {stake_percent:20, ownership_type:'minority', read_from:'field', until:null}]->(b)")
    assert "greater than 100" in _refused(
        it_db, "MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) CREATE (a)-[:OWNS {stake_percent:120, until:null}]->(b)")
    assert "does not match" in _refused(
        it_db, "MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) CREATE (a)-[:OWNS {read_from:'guess', until:null}]->(b)")
    assert "does not match" in _refused(
        it_db, "MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) CREATE (a)-[:OWNS {ownership_type:'owner', until:null}]->(b)")
    # the flag's default: a new edge is not stale unless said so
    row = it_db.run_sql("SELECT stale, shortcut, also_ultimate, stake_percent FROM OWNS")[0]
    assert row["stale"] is False and row["stake_percent"] == 20.0
    assert row["shortcut"] is False and row["also_ultimate"] is False
    # a node too: an entity created without the flags
    it_db.run_command("CREATE (:Entity {id:'plain', name:'Plain Co', type:'company'})")
    row = it_db.run_sql("SELECT is_nominee, on_demand_scraped, active FROM Entity WHERE id = 'plain'")[0]
    assert row["is_nominee"] is False and row["on_demand_scraped"] is False and row["active"] is None
    # a 13D filing group is its own vertex type (2026-10-26): no Entity carries that type any more
    assert "does not match" in _refused(it_db, "CREATE (:Entity {id:'vg', name:'Voting group', type:'voting_group'})")
    it_db.run_command("CREATE (:VotingGroup {id:'vg', name:'Voting group · 2 parties', type:'voting_group'})")
    assert "does not match" in _refused(it_db, "CREATE (:VotingGroup {id:'vg2', name:'X', type:'company'})")
    assert "mandatory" in _refused(it_db, "CREATE (:Claim {kind:'owns', from_id:'a', to_id:'b', source_id:'s'})")
    assert "does not match" in _refused(
        it_db, "CREATE (:Claim {claim_key:'k', kind:'ownership', from_id:'a', to_id:'b', source_id:'s'})")
    # a claim names its source
    assert "mandatory" in _refused(it_db, "CREATE (:Claim {claim_key:'k2', kind:'owns', from_id:'a', to_id:'b'})")


def test_a_date_keeps_its_shape_at_any_precision_and_a_timestamp_its_offset(it_db):
    it_db.run_command("CREATE (:Entity {id:'a', name:'A', name_normalized:'a'})")
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', name_normalized:'b'})")
    # the precisions the sources state — a Wikidata year, a PSC birth month
    for ok in ("2019-03-04", "2019-00-00", "2019-03", "2019"):
        it_db.run_command("MATCH (a:Entity {id:'a'}) SET a.incorporated_on = $d", {"d": ok})
        assert it_db.run_sql("SELECT incorporated_on AS d FROM Entity WHERE id = 'a'")[0]["d"] == ok
    for bad in ("4 March 2019", "03/04/2019", "2019-3-4", "2019-03-04T00:00:00+00:00"):
        assert "does not match" in _refused(it_db, "MATCH (a:Entity {id:'a'}) SET a.incorporated_on = $d", {"d": bad}), bad
    it_db.run_command("MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) "
                      "CREATE (a)-[:OWNS {since:'2020-01-01', source_date:'2026-08-07', until:null}]->(b)")
    assert "does not match" in _refused(
        it_db, "MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) CREATE (a)-[:OWNS {since:'Jan 2020', until:null}]->(b)")
    assert "does not match" in _refused(
        it_db, "MATCH (a:Entity {id:'a'}) SET a.incorporated_on = '2019-03-04', a.created_at = '2019-03-04'")
    # a timestamp: what the writers send, with either spelling of UTC; never naive
    for ok in ("2026-10-25T10:00:00.123456+00:00", "2026-10-25T10:00:00Z", "2026-10-25T12:00:00+02:00",
               "2020-01-01T00:00Z"):                     # what the server makes of a parsed `…T00:00:00Z`
        it_db.run_command("MATCH (a:Entity {id:'a'}) SET a.last_scraped_at = $t", {"t": ok})
        assert it_db.run_sql("SELECT last_scraped_at AS t FROM Entity WHERE id = 'a'")[0]["t"] == ok
    for bad in ("2026-10-25", "2026-10-25T10:00:00", "yesterday"):
        assert "does not match" in _refused(it_db, "MATCH (a:Entity {id:'a'}) SET a.last_scraped_at = $t", {"t": bad}), bad
    # a null still passes: the date unknown is allowed
    it_db.run_command("MATCH (a:Entity {id:'a'}) SET a.incorporated_on = null, a.last_scraped_at = null")


def test_a_rule_is_refused_while_a_row_breaks_it_so_heals_run_first(it_db):
    from app.db.schema import ensure_indexes
    # the state of a database from before the rule: the property undeclared
    # (`ALTER … REGEXP null` would set the literal text "null"; dropping the
    # unindexed declaration is how a rule is lifted) and a row that breaks it
    it_db.run_sql("DROP PROPERTY Entity.hq_country IF EXISTS")
    it_db.run_command("CREATE (:Entity {id:'x', name:'X', name_normalized:'x', hq_country:'USA'})")
    res = ensure_indexes()
    assert "CREATE PROPERTY Entity.hq_country IF NOT EXISTS STRING" in res["ok"]
    assert [f["stmt"] for f in res["failed"]] == ["ALTER PROPERTY Entity.hq_country REGEXP '^[A-Z]{2}$'"]
    assert "violates it" in res["failed"][0]["error"]
    it_db.run_command("MATCH (e:Entity {id:'x'}) SET e.hq_country = 'US'")
    assert ensure_indexes()["failed"] == [] and ensure_indexes().get("complete") is True
