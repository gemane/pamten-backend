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
