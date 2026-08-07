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
    """Insert a Source, two Entities, an OWNS edge, and the Claim behind it.

    Relationship provenance is read from Claim rows, not from the edge — the
    edge holds one winning value while the claims hold what each source said.
    The writers always produce both, so seeding both is what the real graph
    looks like.
    """
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
    _seed_claim(arcadedb, from_id="e-owner", to_id="e-target", source_id="s1",
                source_url="https://www.sec.gov/Archives/edgar/data/1/primary.htm",
                source_date="2025-02-14", last_seen="2026-07-12T09:00:00+00:00")


def _seed_claim(arcadedb, *, from_id, to_id, source_id,
                source_url=None, source_date=None, last_seen="2026-07-12T09:00:00+00:00"):
    from app.claims import KIND_OWNS, claim_key

    arcadedb.run_command(
        "CREATE (:Claim {claim_key: $k, kind: $kind, from_id: $f, to_id: $t, "
        "source_id: $s, source_url: $u, source_date: $d, last_seen_at: $seen})",
        {"k": claim_key(KIND_OWNS, from_id, to_id, source_id), "kind": KIND_OWNS,
         "f": from_id, "t": to_id, "s": source_id, "u": source_url,
         "d": source_date, "seen": last_seen},
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

    # A claim with no specific record URL → fall back to the source home page.
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
    _seed_claim(it_db, from_id="e2-owner", to_id="e2-target", source_id="s2")

    rows = get_sources_for_entity("e2-target")

    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.wikidata.org"  # fell back to home
    assert rows[0]["source_date"] is None


def test_sources_endpoint_empty_for_entity_without_sources(it_db):
    from app.routers.sources import get_sources_for_entity

    it_db.run_command("CREATE (:Entity {id: 'e-lonely', name: 'No Sources Co'})")

    assert get_sources_for_entity("e-lonely") == []


def test_entity_own_sources_deep_link_from_each_identifier(it_db):
    """A cross-source entity carrying BOTH a Wikidata QID and an SEC CIK (e.g. after a
    merge) shows BOTH sources, each deep-linked to the specific record — not the single
    stamped source_id at the source's home page."""
    from app.routers.sources import get_sources_for_entity

    it_db.run_command("CREATE (:Source {id: 'sec', name: 'SEC EDGAR', url: 'https://www.sec.gov/edgar', "
                      "type: 'register', credibility_score: 98})")
    it_db.run_command("CREATE (:Source {id: 'wd', name: 'Wikidata', url: 'https://www.wikidata.org', "
                      "type: 'knowledge_base', credibility_score: 80})")
    # A pure owner (no inbound edges) with both identifiers + a single stamped source_id.
    it_db.run_command(
        "CREATE (:Entity {id: 'vg', name: 'The Vanguard Group', "
        "wikidata_id: 'Q849363', sec_cik: '0000102909', source_id: 'sec'})")

    rows = get_sources_for_entity("vg")
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"SEC EDGAR", "Wikidata"}
    assert by_name["SEC EDGAR"]["url"] == \
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000102909"
    assert by_name["Wikidata"]["url"] == "https://www.wikidata.org/wiki/Q849363"


def test_entity_own_sources_fall_back_to_source_id_without_identifiers(it_db):
    """An entity with a stamped source_id but no hard identifier keeps the single
    source-id provenance row (home URL)."""
    from app.routers.sources import get_sources_for_entity

    it_db.run_command("CREATE (:Source {id: 'oc', name: 'OpenCorporates', "
                      "url: 'https://opencorporates.com', type: 'register', credibility_score: 70})")
    it_db.run_command("CREATE (:Entity {id: 'plain', name: 'Plain Co', source_id: 'oc'})")

    rows = get_sources_for_entity("plain")
    assert len(rows) == 1
    assert rows[0]["name"] == "OpenCorporates"
    assert rows[0]["url"] == "https://opencorporates.com"


def test_sources_dedupes_repeated_same_source_link(it_db):
    """The same source URL backing many facts (every Wikidata owner edge points at the
    company's Wikidata page) collapses to a single row."""
    from app.routers.sources import get_sources_for_entity

    it_db.run_command("CREATE (:Source {id: 'wd', name: 'Wikidata', url: 'https://www.wikidata.org', "
                      "type: 'knowledge_base', credibility_score: 80})")
    it_db.run_command("CREATE (:Entity {id: 'target', name: 'T'})")
    # three different owners, all recorded from the SAME Wikidata page, different dates
    for i, date in enumerate(("2024-01-01", "2024-02-02", "2024-03-03")):
        it_db.run_command(f"CREATE (:Person {{id: 'p{i}', full_name: 'Owner {i}'}})")
        it_db.run_command(
            f"MATCH (a:Person {{id: 'p{i}'}}),(b:Entity {{id: 'target'}}) "
            f"CREATE (a)-[:OWNS {{source_id: 'wd', source_url: 'https://www.wikidata.org/wiki/Q1', "
            f"source_date: '{date}', until: null}}]->(b)")
        _seed_claim(it_db, from_id=f"p{i}", to_id="target", source_id="wd",
                    source_url="https://www.wikidata.org/wiki/Q1", source_date=date)

    rows = get_sources_for_entity("target")
    wd = [r for r in rows if r["url"] == "https://www.wikidata.org/wiki/Q1"]
    assert len(wd) == 1                                   # collapsed to a single link
    assert wd[0]["source_date"] == "2024-03-03"           # kept the most recent instance
