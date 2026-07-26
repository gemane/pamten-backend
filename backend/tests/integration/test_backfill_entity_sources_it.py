"""
Real-ArcadeDB test for backfill_entity_sources.

The mocked unit tests can't catch the bug that motivated this: a Cypher
`MATCH … SET` and a single `run_sql` UPDATE both ran without persisting on the
real engine, so entities the Wikidata/SEC scrapers created before they stamped
source_id kept showing no source. This runs the backfill end-to-end and asserts
the write actually committed — including that SQL `IS NULL` matches an *absent*
source_id property (the pre-fix nodes never had the key).
"""
import pytest

pytestmark = pytest.mark.integration


def test_backfill_stamps_source_id_and_persists(it_db):
    from app.scraper import maintenance

    it_db.run_command("CREATE (s:Source {id:'src-wd',  name:'Wikidata',  type:'knowledge_base'})")
    it_db.run_command("CREATE (s:Source {id:'src-sec', name:'SEC EDGAR', type:'register'})")

    # Entities created WITHOUT a source_id key (property absent), as the pre-fix
    # scrapers left them.
    it_db.run_command("CREATE (e:Entity {id:'wd1', name:'Government of Abu Dhabi', wikidata_id:'Q113685851'})")
    it_db.run_command("CREATE (e:Entity {id:'sec1', name:'Vanguard Group Inc', sec_cik:'0000102909'})")
    # Neither identifier → can't attribute → must stay null.
    it_db.run_command("CREATE (e:Entity {id:'none1', name:'Anon Ltd'})")
    # Already sourced → must NOT be touched.
    it_db.run_command("CREATE (e:Entity {id:'has1', name:'Already', wikidata_id:'Q7', source_id:'pre-existing'})")

    result = maintenance.backfill_entity_sources()

    assert result["updated"] == {"wikidata": 1, "sec_edgar": 1}
    assert result["still_missing"] == 1          # only 'none1'

    def _source(node_id):
        rows = it_db.run_sql(f"SELECT source_id FROM Entity WHERE id = '{node_id}'")
        return rows[0]["source_id"]

    # The writes committed (a fresh SELECT sees them).
    assert _source("wd1")  == "src-wd"
    assert _source("sec1") == "src-sec"
    assert _source("none1") is None
    assert _source("has1") == "pre-existing"     # not clobbered


def test_backfill_is_idempotent(it_db):
    from app.scraper import maintenance

    it_db.run_command("CREATE (s:Source {id:'src-wd', name:'Wikidata', type:'knowledge_base'})")
    it_db.run_command("CREATE (e:Entity {id:'wd1', name:'Gov', wikidata_id:'Q1'})")

    first  = maintenance.backfill_entity_sources()
    second = maintenance.backfill_entity_sources()

    assert first["updated"]["wikidata"] == 1
    assert second["updated"]["wikidata"] == 0     # nothing left to stamp
    rows = it_db.run_sql("SELECT source_id FROM Entity WHERE id = 'wd1'")
    assert rows[0]["source_id"] == "src-wd"
