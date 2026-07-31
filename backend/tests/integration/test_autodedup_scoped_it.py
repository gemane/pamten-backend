"""
Real-ArcadeDB test that the post-scrape auto-dedup is scoped to the persons a
scrape touched — not a full-DB person scan (which is O(all persons) and crawls once
the graph grows to millions of nodes).

Covers: the touched-persons collector records upserted ids; a scoped scan groups the
touched persons + existing same-name candidates and ignores unrelated persons; an
empty touched set does no work.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _person(it_db, pid, full_name):
    it_db.run_command(
        "CREATE (p:Person {id:$id, full_name:$n, alias:[], wikidata_id:null})",
        {"id": pid, "n": full_name})


def test_autodedup_scoped_to_touched(it_db):
    from app.routers.persons import deduplicate_high_confidence, scan_duplicate_groups
    from app.scraper.graph_writer import _touched_persons
    from app.scraper.runner import _upsert_person_by_name

    # A scrape touches two persons the sources spelled differently (same name token
    # set) — the collector records them while it's active.
    token = _touched_persons.set(set())
    a = _upsert_person_by_name("William H Gates III", "src")
    b = _upsert_person_by_name("Gates William H Iii", "src")   # same name-key as a
    touched = set(_touched_persons.get())
    _touched_persons.reset(token)
    assert {a, b} <= touched

    # An EXISTING person (not touched) with the same exact name → must be pulled in
    # as a candidate. An unrelated person → must never be considered.
    _person(it_db, "existing-gates", "William H Gates III")
    _person(it_db, "unrelated", "Totally Unrelated Person")

    groups = scan_duplicate_groups(seed_ids=list(touched))
    grouped = {m["id"] for g in groups for m in g["members"]}
    assert {a, b, "existing-gates"} <= grouped     # seeds + existing same-name candidate
    assert "unrelated" not in grouped              # unrelated person excluded from the scope

    # Nothing touched → no work, no groups.
    assert scan_duplicate_groups(seed_ids=[]) == []
    assert deduplicate_high_confidence(apply=False, seed_ids=[])["review_count"] == 0

    # The full scan (no seed) still sees everyone — the same duplicate group surfaces.
    full = scan_duplicate_groups()
    assert any({a, b, "existing-gates"} <= {m["id"] for m in g["members"]} for g in full)
