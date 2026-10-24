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
    assert rules[("Claim", "claim_key")]["mandatory"] is True
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
    row = it_db.run_sql("SELECT stale, stake_percent FROM OWNS")[0]
    assert row["stale"] is False and row["stake_percent"] == 20.0
    assert "mandatory" in _refused(it_db, "CREATE (:Claim {kind:'owns', from_id:'a', to_id:'b', source_id:'s'})")
    assert "does not match" in _refused(
        it_db, "CREATE (:Claim {claim_key:'k', kind:'ownership', from_id:'a', to_id:'b', source_id:'s'})")


def test_a_rule_is_refused_while_a_row_breaks_it_so_heals_run_first(it_db):
    from app.db.schema import ensure_indexes
    it_db.run_sql("ALTER PROPERTY Entity.country REGEXP null")           # the rule lifted, as before 2026-10-25
    it_db.run_command("CREATE (:Entity {id:'x', name:'X', name_normalized:'x', country:'USA'})")
    res = ensure_indexes()
    assert [f["stmt"] for f in res["failed"]] == ["ALTER PROPERTY Entity.country REGEXP '^[A-Z]{2}$'"]
    assert "violates it" in res["failed"][0]["error"]
    it_db.run_command("MATCH (e:Entity {id:'x'}) SET e.country = 'US'")
    assert ensure_indexes()["failed"] == [] and ensure_indexes().get("complete") is True
