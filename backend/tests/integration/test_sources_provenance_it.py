"""
Real-ArcadeDB integration tests for the per-entry provenance endpoint.

These exercise the actual read/write Cypher and the real _Record result type —
the exact code paths that the mocked unit tests cannot see. Either bug that hit
production (Cypher-dialect rejection, and dict(rec) on a whole row) would fail
these tests.

Skipped unless ARCADEDB_IT_URL is configured — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed_provenance(arcadedb):
    """Insert a Source, two Entities, and one OWNS edge carrying provenance."""
    arcadedb.run_command(
        "CREATE (:Source {id: 's1', name: 'SEC EDGAR', url: 'https://www.sec.gov', "
        "type: 'register', credibility_score: 95})"
    )
    arcadedb.run_command("CREATE (:Entity {id: 'e-target', name: 'Target Co'})")
    arcadedb.run_command("CREATE (:Entity {id: 'e-owner', name: 'Owner Co'})")
    arcadedb.run_command(
        """
        MATCH (a:Entity {id: 'e-owner'}), (b:Entity {id: 'e-target'})
        CREATE (a)-[:OWNS {
            source_id: 's1',
            source_url: 'https://www.sec.gov/Archives/edgar/data/1/primary.htm',
            source_date: '2025-02-14',
            last_scraped_at: '2026-07-12T09:00:00+00:00',
            ownership_type: 'majority',
            until: null
        }]->(b)
        """
    )


def test_sources_endpoint_returns_provenance(it_db):
    from app.routers.sources import get_sources_for_entity

    _seed_provenance(it_db)

    rows = get_sources_for_entity("e-target")

    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "SEC EDGAR"
    assert row["type"] == "register"
    assert row["credibility_score"] == 95
    # Specific record URL wins over the source home page
    assert row["url"] == "https://www.sec.gov/Archives/edgar/data/1/primary.htm"
    assert row["source_date"] == "2025-02-14"
    assert row["last_scraped_at"].startswith("2026-07-12")


def test_sources_endpoint_falls_back_to_home_url(it_db):
    from app.routers.sources import get_sources_for_entity

    # A source referenced by an edge with no per-edge source_url → home URL.
    it_db.run_command(
        "CREATE (:Source {id: 's2', name: 'Wikidata', url: 'https://www.wikidata.org', "
        "type: 'knowledge_base', credibility_score: 80})"
    )
    it_db.run_command("CREATE (:Entity {id: 'e2-target', name: 'T2'})")
    it_db.run_command("CREATE (:Entity {id: 'e2-owner', name: 'O2'})")
    it_db.run_command(
        """
        MATCH (a:Entity {id: 'e2-owner'}), (b:Entity {id: 'e2-target'})
        CREATE (a)-[:OWNS {source_id: 's2', ownership_type: 'minority', until: null}]->(b)
        """
    )

    rows = get_sources_for_entity("e2-target")

    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.wikidata.org"  # fell back to home
    assert rows[0]["source_date"] is None


def test_sources_endpoint_empty_for_entity_without_sources(it_db):
    from app.routers.sources import get_sources_for_entity

    it_db.run_command("CREATE (:Entity {id: 'e-lonely', name: 'No Sources Co'})")

    assert get_sources_for_entity("e-lonely") == []
