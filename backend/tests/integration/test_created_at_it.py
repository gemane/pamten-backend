"""Real-ArcadeDB: `created_at` is set once, kept by a re-import and a merge,
and backfilled only from what was recorded (app.created_at).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _created_at(it_db, label, node_id):
    rows = it_db.run_sql(f"SELECT created_at FROM {label} WHERE id = '{node_id}'")
    return rows[0]["created_at"] if rows else None


def test_a_bulk_re_import_is_not_a_birth(it_db):
    from app.scraper.bulk_import import _BatchWriter

    b = _BatchWriter(batch_size=10)
    b.entity("lei:1", {"name": "Acme", "name_normalized": "acme"})
    b.flush()
    first = _created_at(it_db, "Entity", "lei:1")
    assert first and first.endswith("+00:00")

    b = _BatchWriter(batch_size=10)
    b.entity("lei:1", {"name": "Acme AG", "name_normalized": "acme ag"})
    b.flush()
    assert _created_at(it_db, "Entity", "lei:1") == first          # kept
    assert it_db.run_sql("SELECT name FROM Entity WHERE id = 'lei:1'")[0]["name"] == "Acme AG"


def test_the_scrape_path_and_the_gleif_delta_placeholder_stamp_it(it_db):
    from app.scraper.graph_writer import _upsert_entity_by_name, _upsert_person_by_name
    from app.scraper.gleif_incremental import _ensure_lei_node

    eid = _upsert_entity_by_name("Fresh Ltd", source_id="s")
    pid = _upsert_person_by_name("Ada Lovelace", source_id="s")
    assert _created_at(it_db, "Entity", eid) and _created_at(it_db, "Person", pid)

    nid = _ensure_lei_node("PLACEHOLDERLEI000001", "s")
    stamped = _created_at(it_db, "Entity", nid)
    assert stamped
    assert _ensure_lei_node("PLACEHOLDERLEI000001", "s") == nid
    assert _created_at(it_db, "Entity", nid) == stamped             # a second touch keeps it


def test_an_entity_merge_keeps_the_earlier_birth(it_db):
    from app.scraper.maintenance import deduplicate_entities_for

    # Seeded through parameters, as every writer stamps it: a Cypher LITERAL that
    # looks like a datetime is parsed into one by ArcadeDB and comes back as
    # '2026-03-01T00:00Z' — not the string the writers store.
    for eid, cred, born in (("ent-a", 92, "2026-03-01T00:00:00+00:00"), ("ent-b", 80, "2025-01-01T00:00:00+00:00")):
        it_db.run_command(
            "CREATE (:Entity {id:$id, name:'Acme', name_normalized:'acme', type:'company', "
            "lei_id:'AAAA1111BBBB2222CCCC', name_credibility:$cred, created_at:$born})",
            {"id": eid, "cred": cred, "born": born})
    assert deduplicate_entities_for(["ent-a", "ent-b"], apply=True)["entities_merged"] == 1
    assert _created_at(it_db, "Entity", "ent-a") == "2025-01-01T00:00:00+00:00"


def test_a_person_merge_keeps_the_earlier_birth(it_db):
    from app.routers.persons import merge_person_records

    for pid, born in (("keep-1", "2026-03-01T00:00:00+00:00"), ("dup-1", "2025-01-01T00:00:00+00:00")):
        it_db.run_command("CREATE (:Person {id:$id, first_name:'Ada', last_name:'L', full_name:'Ada L', "
                          "created_at:$born})", {"id": pid, "born": born})
    merge_person_records(keep="keep-1", dup="dup-1")
    assert _created_at(it_db, "Person", "keep-1") == "2025-01-01T00:00:00+00:00"


def test_a_bulk_write_keeps_the_source_that_wrote_the_record_first(it_db):
    """`source_id` follows the scrape's rule on the bulk paths too (the user,
    2026-11-28): GLEIF's import over a company EDGAR created leaves EDGAR's stamp."""
    from app.scraper.bulk_import import _BatchWriter
    from app.scraper.gleif_incremental import _ensure_lei_node

    def source(node_id):
        return it_db.run_sql(f"SELECT source_id FROM Entity WHERE id = '{node_id}'")[0].get("source_id")

    b = _BatchWriter(batch_size=10)
    b.entity("lei:STAMP000000000000001", {"name": "Acme Corp", "source_id": "sec-edgar"})
    b.entity("lei:STAMP000000000000002", {"name": "Unstamped"})
    b.flush()
    b = _BatchWriter(batch_size=10)
    b.entity("lei:STAMP000000000000001", {"name": "ACME CORPORATION", "source_id": "gleif"})
    b.entity("lei:STAMP000000000000002", {"name": "Unstamped", "source_id": "gleif"})
    b.entity("lei:STAMP000000000000003", {"name": "New", "source_id": "gleif"})
    b.flush()
    assert source("lei:STAMP000000000000001") == "sec-edgar"        # kept
    assert source("lei:STAMP000000000000002") == "gleif"            # filled where empty
    assert source("lei:STAMP000000000000003") == "gleif"            # a new record is its writer's
    assert it_db.run_sql("SELECT name FROM Entity WHERE id = 'lei:STAMP000000000000001'")[0]["name"] == "ACME CORPORATION"

    assert _ensure_lei_node("STAMP000000000000001", "gleif") == "lei:STAMP000000000000001"
    assert source("lei:STAMP000000000000001") == "sec-edgar"        # the delta's placeholder keeps it too
