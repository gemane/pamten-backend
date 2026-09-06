"""Importers must not resurrect a merged-away node — the two-Alphabet re-split.

After a merge folds `lei:X` into a survivor, a later GLEIF import addressing
the entity by `lei:X` used to recreate it. The bulk entity flush now skips a
merged-away id (never clobbering the survivor), and the RR path redirects its
edge endpoints to the survivor.
"""
import pytest

from app.merged_ids import record_merge_sql, invalidate_forwarding_cache
from app.scraper.bulk_import import _BatchWriter
from app.scraper import gleif_incremental as gi

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fresh_cache():
    invalidate_forwarding_cache()
    yield
    invalidate_forwarding_cache()


def _merged_pair(it_db):
    # survivor is the PSC-style node (as in the real Alphabet case); the GLEIF
    # lei node was folded into it, leaving a forwarding row.
    it_db.run_command(
        "CREATE (:Entity {id: 'chpsc:keep', name: 'Alphabet, Inc.', "
        "name_normalized: 'alphabet', search_text: 'Alphabet, Inc.', type: 'company', "
        "lei_id: '5493006MHB84DD0ZWV18', name_credibility: 97})")
    record_merge_sql("lei:5493006MHB84DD0ZWV18", "chpsc:keep", kind="Entity")
    invalidate_forwarding_cache()


def test_the_bulk_entity_flush_does_not_resurrect_or_clobber(it_db):
    _merged_pair(it_db)
    batch = _BatchWriter()
    # a GLEIF re-import addressing Alphabet by its lei id, with GLEIF's own
    # (lower-credibility, uppercase) name
    batch.entity("lei:5493006MHB84DD0ZWV18",
                 {"name": "ALPHABET INC.", "name_normalized": "alphabet",
                  "lei_id": "5493006MHB84DD0ZWV18", "name_credibility": 92,
                  "type": "company"})
    batch.flush()
    rows = it_db.run_sql("SELECT id, name, name_credibility FROM Entity "
                         "WHERE name_normalized = 'alphabet'")
    assert len(rows) == 1, "no resurrection — still one Alphabet"
    keep = dict(rows[0])
    assert keep["id"] == "chpsc:keep"
    assert keep["name"] == "Alphabet, Inc.", "survivor's better name not clobbered"
    assert keep["name_credibility"] == 97


def test_the_rr_edge_attaches_to_the_survivor_not_a_resurrected_node(it_db):
    _merged_pair(it_db)
    # the child company Alphabet controls
    it_db.run_command(
        "CREATE (:Entity {id: 'lei:CHILD0000000000000', name: 'Child Co', "
        "name_normalized: 'child co', search_text: 'Child Co', type: 'company', "
        "lei_id: 'CHILD0000000000000'})")
    src = gi.run_command  # noqa: F841  (ensure module import side effects)
    from app.scraper.graph_writer import _ensure_source
    source_id = _ensure_source("GLEIF", "https://www.gleif.org", 92)
    gi._upsert_owns("5493006MHB84DD0ZWV18", "CHILD0000000000000", "direct",
                    source_id, 92)
    # the merged-away lei node was NOT resurrected
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity "
                         "WHERE id = 'lei:5493006MHB84DD0ZWV18'")[0]["n"] == 0
    # the edge runs FROM the survivor
    edge = it_db.run_sql("SELECT count(*) AS n FROM OWNS "
                         "WHERE source_id = :s", {"s": source_id})[0]["n"]
    assert edge == 1
    frm = it_db.run_command(
        "MATCH (a)-[:OWNS]->(b:Entity {id:'lei:CHILD0000000000000'}) RETURN a.id AS id")
    assert dict(frm[0])["id"] == "chpsc:keep", "edge attached to the survivor"
