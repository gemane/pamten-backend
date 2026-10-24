"""Real-ArcadeDB: declared property types coerce new writes, and the heal
re-types what was stored before the declaration.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_the_bootstrap_declares_the_types_and_writes_are_coerced(it_db):
    # it_db ran ensure_indexes(): the declarations are in the catalog
    declared = {(t["name"], p["name"]): p["type"]
                for t in it_db.run_sql("SELECT name, properties FROM schema:types")
                for p in (t.get("properties") or [])}
    assert declared[("OWNS", "stake_percent")] == "DOUBLE" and declared[("OWNS", "shares")] == "LONG"
    assert declared[("Entity", "founded")] == "INTEGER" and declared[("Entity", "aliases")] == "LIST"
    assert declared[("Claim", "stake_percent")] == "DOUBLE"

    it_db.run_command("CREATE (:Entity {id:'a', name:'A', name_normalized:'a'})")
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', name_normalized:'b'})")
    # an int stake and a float share count, as a writer's JSON may carry them
    it_db.run_command("MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) "
                      "CREATE (a)-[:OWNS {stake_percent:$s, shares:$n, until:null}]->(b)", {"s": 20, "n": 1.5e8})
    row = it_db.run_sql("SELECT stake_percent, stake_percent.type() AS st, shares, shares.type() AS sh FROM OWNS")[0]
    assert (row["st"], row["sh"]) == ("DOUBLE", "LONG") and row["stake_percent"] == 20.0 and row["shares"] == 150000000
    # and a second run of the bootstrap issues nothing for them
    from app.db.schema import ensure_indexes
    assert ensure_indexes().get("complete") is True


def test_the_heal_retypes_rows_stored_before_the_declaration(it_db):
    from app.property_types import heal

    # undo the declaration for the test, store the old way, declare again
    it_db.run_sql("DROP PROPERTY OWNS.stake_percent IF EXISTS")
    it_db.run_sql("DROP PROPERTY OWNS.shares IF EXISTS")
    it_db.run_command("CREATE (:Entity {id:'a', name:'A', name_normalized:'a'})")
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', name_normalized:'b'})")
    it_db.run_command("MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) "
                      "CREATE (a)-[:OWNS {stake_percent:$s, shares:$n, until:null}]->(b)", {"s": 20, "n": 1.5e8})
    it_db.run_sql("CREATE PROPERTY OWNS.stake_percent IF NOT EXISTS DOUBLE")
    it_db.run_sql("CREATE PROPERTY OWNS.shares IF NOT EXISTS LONG")
    before = it_db.run_sql("SELECT stake_percent.type() AS st, shares.type() AS sh FROM OWNS")[0]
    assert (before["st"], before["sh"]) == ("INTEGER", "DOUBLE")          # the declaration touched nothing

    assert heal(apply=False)["converted"] == {"OWNS.stake_percent": 1, "OWNS.shares": 1}
    assert it_db.run_sql("SELECT stake_percent.type() AS st FROM OWNS")[0]["st"] == "INTEGER"   # dry run

    res = heal(apply=True)
    assert res["converted"] == {"OWNS.stake_percent": 1, "OWNS.shares": 1}
    after = it_db.run_sql("SELECT stake_percent, stake_percent.type() AS st, shares, shares.type() AS sh FROM OWNS")[0]
    assert (after["st"], after["sh"]) == ("DOUBLE", "LONG") and after["stake_percent"] == 20.0 and after["shares"] == 150000000
    assert heal(apply=True)["converted"] == {}                              # idempotent
