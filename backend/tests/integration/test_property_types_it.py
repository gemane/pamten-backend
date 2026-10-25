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
    assert declared[("OWNS", "stake_percent")] == "DOUBLE" and declared[("OWNS", "shares")] == "DOUBLE"
    assert declared[("Entity", "founded")] == "INTEGER" and declared[("Entity", "aliases")] == "LIST"
    assert declared[("Claim", "stake_percent")] == "DOUBLE"

    it_db.run_command("CREATE (:Entity {id:'a', name:'A', name_normalized:'a'})")
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', name_normalized:'b'})")
    # an int stake and an int share count, as a writer's JSON may carry them
    it_db.run_command("MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) "
                      "CREATE (a)-[:OWNS {stake_percent:$s, shares:$n, until:null}]->(b)", {"s": 20, "n": 150000000})
    row = it_db.run_sql("SELECT stake_percent, stake_percent.type() AS st, shares, shares.type() AS sh FROM OWNS")[0]
    assert (row["st"], row["sh"]) == ("DOUBLE", "DOUBLE") and row["stake_percent"] == 20.0 and row["shares"] == 150000000.0
    # a fractional count (a Form 4's dividend-reinvestment shares) is kept as stated
    it_db.run_command("MATCH ()-[r:OWNS]->() SET r.shares = $n", {"n": 562701.427})
    assert it_db.run_sql("SELECT shares FROM OWNS")[0]["shares"] == 562701.427
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
                      "CREATE (a)-[:OWNS {stake_percent:$s, shares:$n, until:null}]->(b)", {"s": 20, "n": 150000000})
    it_db.run_sql("CREATE PROPERTY OWNS.stake_percent IF NOT EXISTS DOUBLE")
    it_db.run_sql("CREATE PROPERTY OWNS.shares IF NOT EXISTS DOUBLE")
    before = it_db.run_sql("SELECT stake_percent.type() AS st, shares.type() AS sh FROM OWNS")[0]
    assert (before["st"], before["sh"]) == ("INTEGER", "INTEGER")         # the declaration touched nothing

    assert heal(apply=False)["converted"] == {"OWNS.stake_percent": 1, "OWNS.shares": 1}
    assert it_db.run_sql("SELECT stake_percent.type() AS st FROM OWNS")[0]["st"] == "INTEGER"   # dry run

    res = heal(apply=True)
    assert res["converted"] == {"OWNS.stake_percent": 1, "OWNS.shares": 1}
    after = it_db.run_sql("SELECT stake_percent, stake_percent.type() AS st, shares, shares.type() AS sh FROM OWNS")[0]
    assert (after["st"], after["sh"]) == ("DOUBLE", "DOUBLE") and after["stake_percent"] == 20.0 and after["shares"] == 150000000.0
    assert heal(apply=True)["converted"] == {}                              # idempotent


def test_a_property_declared_with_another_type_is_redeclared_by_the_bootstrap(it_db):
    """shares_outstanding sat LONG on dev from the deploy before it became DOUBLE:
    the heal rewrote 47 rows, the LONG declaration coerced them back, and the
    next dry run saw the same 47 again. ArcadeDB has no ALTER PROPERTY … TYPE."""
    from app.db.schema import ensure_indexes
    from app.property_types import heal

    it_db.run_sql("DROP PROPERTY OWNS.shares_outstanding IF EXISTS")
    it_db.run_sql("CREATE PROPERTY OWNS.shares_outstanding IF NOT EXISTS LONG")
    it_db.run_command("CREATE (:Entity {id:'a', name:'A', name_normalized:'a'})")
    it_db.run_command("CREATE (:Entity {id:'b', name:'B', name_normalized:'b'})")
    it_db.run_command("MATCH (a:Entity {id:'a'}), (b:Entity {id:'b'}) "
                      "CREATE (a)-[:OWNS {shares_outstanding:$n, until:null}]->(b)", {"n": 47.0})
    assert it_db.run_sql("SELECT shares_outstanding.type() AS t FROM OWNS")[0]["t"] == "LONG"   # coerced

    # the heal refuses the group rather than rewriting it forever
    res = heal(apply=True)
    assert res["converted"] == {} and res["misdeclared"] == {"OWNS.shares_outstanding": {"server": "LONG", "code": "DOUBLE"}}
    assert it_db.run_sql("SELECT shares_outstanding.type() AS t FROM OWNS")[0]["t"] == "LONG"

    # the bootstrap drops and redeclares — without touching the row
    boot = ensure_indexes()
    assert boot["failed"] == [] and "DROP PROPERTY OWNS.shares_outstanding IF EXISTS" in boot["ok"]
    declared = {(t["name"], p["name"]): p["type"]
                for t in it_db.run_sql("SELECT name, properties FROM schema:types")
                for p in (t.get("properties") or [])}
    assert declared[("OWNS", "shares_outstanding")] == "DOUBLE"
    assert it_db.run_sql("SELECT shares_outstanding, shares_outstanding.type() AS t FROM OWNS")[0] \
        == {"shares_outstanding": 47, "t": "LONG"}

    # now the heal converts the row, and a second bootstrap is a no-op
    res = heal(apply=True)
    assert res["converted"] == {"OWNS.shares_outstanding": 1} and res["misdeclared"] == {}
    assert it_db.run_sql("SELECT shares_outstanding, shares_outstanding.type() AS t FROM OWNS")[0] \
        == {"shares_outstanding": 47.0, "t": "DOUBLE"}
    assert ensure_indexes().get("complete") is True
