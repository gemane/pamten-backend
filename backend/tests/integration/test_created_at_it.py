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


def test_the_backfill_dates_from_claims_then_scrapes_and_guesses_nothing(it_db):
    from app.created_at import backfill

    it_db.run_command("CREATE (:Entity {id:'claimed', name:'C', name_normalized:'c'})")
    it_db.run_command("CREATE (:Entity {id:'scraped', name:'S', name_normalized:'s', "
                      "last_scraped_at:'2026-05-05T00:00:00+00:00'})")
    it_db.run_command("CREATE (:Entity {id:'silent', name:'N', name_normalized:'n'})")
    it_db.run_command("CREATE (:Person {id:'p-claimed', full_name:'P'})")
    for key, frm, to, seen in (("k1", "owner", "claimed", "2025-06-01T00:00:00+00:00"),
                               ("k2", "claimed", "sub", "2025-02-01T00:00:00+00:00"),
                               ("k3", "p-claimed", "claimed", "2025-09-01T00:00:00+00:00")):
        it_db.run_sql("UPDATE Claim SET claim_key = :k, kind = 'owns', from_id = :f, to_id = :t, "
                      "source_id = 's', first_seen_at = :seen UPSERT WHERE claim_key = :k",
                      {"k": key, "f": frm, "t": to, "seen": seen})

    dry = backfill(apply=False)
    assert dry["Entity"]["missing"] == 3 and dry["Entity"]["written"] == 0
    assert _created_at(it_db, "Entity", "claimed") is None

    res = backfill(apply=True)
    assert (res["Entity"]["from_claims"], res["Entity"]["from_scrape"], res["Entity"]["unknown"]) == (1, 1, 1)
    assert res["Person"]["from_claims"] == 1
    assert _created_at(it_db, "Entity", "claimed") == "2025-02-01T00:00:00+00:00"   # the oldest of its three claims
    assert _created_at(it_db, "Entity", "scraped") == "2026-05-05T00:00:00+00:00"
    assert _created_at(it_db, "Entity", "silent") is None                            # nothing recorded, nothing guessed
    assert _created_at(it_db, "Person", "p-claimed") == "2025-09-01T00:00:00+00:00"
    assert backfill(apply=True)["Entity"]["missing"] == 1                            # only the unknown one remains
