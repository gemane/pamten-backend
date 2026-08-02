"""
Real-ArcadeDB test for migrate_ownership_types: it re-derives ownership_type IN PLACE
(updates by @rid), never duplicating or dropping edges, and only touches what it should.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(db):
    db.run_command("CREATE (:Entity {id: 'c'})")
    for oid in ("blackrock", "founder", "wikisub", "gleifsub"):
        db.run_command(f"CREATE (:Entity {{id: '{oid}'}})")
    # blackrock: 6.2% but stale 'majority' → should become minority
    db.run_command("MATCH (a:Entity {id:'blackrock'}),(b:Entity {id:'c'}) "
                   "CREATE (a)-[:OWNS {stake_percent:6.2, ownership_type:'majority', until:null}]->(b)")
    # founder: ~1% but stale 'majority' → minority
    db.run_command("MATCH (a:Entity {id:'founder'}),(b:Entity {id:'c'}) "
                   "CREATE (a)-[:OWNS {stake_percent:1.03, ownership_type:'majority', until:null}]->(b)")
    # wikisub: no stake, stale Wikidata 'majority' default → unknown
    db.run_command("MATCH (a:Entity {id:'wikisub'}),(b:Entity {id:'c'}) "
                   "CREATE (a)-[:OWNS {stake_percent:null, ownership_type:'majority', until:null}]->(b)")
    # gleifsub: no stake, 'controlling' (GLEIF consolidation) → MUST be kept
    db.run_command("MATCH (a:Entity {id:'gleifsub'}),(b:Entity {id:'c'}) "
                   "CREATE (a)-[:OWNS {stake_percent:null, ownership_type:'controlling', "
                   "direct_or_indirect:'direct', until:null}]->(b)")


def test_migrate_reclassifies_in_place_without_duplicating(it_db):
    from app.scraper.maintenance import migrate_ownership_types

    _seed(it_db)
    before = it_db.run_sql("SELECT count(*) AS c FROM OWNS")[0]["c"]

    migrate_ownership_types()

    after = it_db.run_sql("SELECT count(*) AS c FROM OWNS")[0]["c"]
    assert after == before == 4              # no duplication, no dropped edges

    got = {r["owner"]: r["t"] for r in it_db.run_command(
        "MATCH (a:Entity)-[r:OWNS]->(b:Entity {id:'c'}) RETURN a.id AS owner, r.ownership_type AS t")}
    assert got == {
        "blackrock": "minority",   # 6.2% reclassified from the stale 'majority'
        "founder":   "minority",   # ~1% reclassified
        "wikisub":   "unknown",    # stakeless Wikidata 'majority' default cleared
        "gleifsub":  "controlling",  # stakeless real signal preserved
    }


def test_migrate_preserves_edge_properties(it_db):
    """In-place update keeps properties the old delete+recreate would have dropped."""
    from app.scraper.maintenance import migrate_ownership_types

    it_db.run_command("CREATE (:Entity {id: 'x'})")
    it_db.run_command("CREATE (:Entity {id: 'y'})")
    it_db.run_command(
        "MATCH (a:Entity {id:'x'}),(b:Entity {id:'y'}) "
        "CREATE (a)-[:OWNS {stake_percent:8.34, ownership_type:'majority', "
        "source_url:'https://sec.gov/filing', direct_or_indirect:'direct', until:null}]->(b)")

    migrate_ownership_types()

    r = dict(it_db.run_command(
        "MATCH (a:Entity {id:'x'})-[o:OWNS]->(b:Entity {id:'y'}) "
        "RETURN o.ownership_type AS t, o.source_url AS surl, o.direct_or_indirect AS doi")[0])
    assert r["t"] == "minority"                          # reclassified
    assert r["surl"] == "https://sec.gov/filing"         # provenance kept
    assert r["doi"] == "direct"                          # marker kept
