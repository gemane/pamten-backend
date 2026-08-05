"""
Real-ArcadeDB tests for merge forwarding addresses.

The unit tests fake the session; these prove the actual behaviour end to end —
that a merged-away id still resolves through the real MergedId index, and that
the person merge (which DETACH DELETEs the duplicate) leaves one behind.

Mocked tests have repeatedly agreed with each other and disagreed with ArcadeDB
in this codebase, so the parts that matter run against a real database.
"""
import pytest

from app.database import db
from app.merged_ids import record_merge, resolve_current_id
from app.routers.persons import merge_person_records
from app.routers import search

pytestmark = pytest.mark.integration


def test_redirect_survives_a_round_trip(it_db):
    with db.get_session() as session:
        record_merge(session, old_id="old-1", new_id="new-1", kind="Entity")
        assert resolve_current_id(session, "old-1") == "new-1"


def test_unmerged_id_resolves_to_nothing(it_db):
    with db.get_session() as session:
        assert resolve_current_id(session, "never-merged") is None


def test_second_merge_repoints_the_first_redirect(it_db):
    # A→B, then B→C. A must resolve straight to C: following to B would land on
    # a node that no longer exists.
    with db.get_session() as session:
        record_merge(session, old_id="A", new_id="B")
        record_merge(session, old_id="B", new_id="C")
        assert resolve_current_id(session, "A") == "C"
        assert resolve_current_id(session, "B") == "C"


def test_recording_the_same_merge_twice_is_idempotent(it_db):
    # Auto-dedup re-runs on every scrape; repeated merges must not pile up rows.
    with db.get_session() as session:
        record_merge(session, old_id="dup", new_id="keep")
        record_merge(session, old_id="dup", new_id="keep")
        rows = list(session.run(
            "MATCH (m:MergedId {old_id:'dup'}) RETURN m.new_id AS new_id"))
        assert len(rows) == 1
        assert rows[0]["new_id"] == "keep"


def _two_persons(it_db):
    it_db.run_command("CREATE (p:Person {id:'keep-1', full_name:'Lawrence Page'})")
    it_db.run_command("CREATE (p:Person {id:'dup-1', full_name:'Larry Page'})")


def test_person_merge_leaves_a_forwarding_address(it_db):
    _two_persons(it_db)
    merge_person_records(keep="keep-1", dup="dup-1")

    with db.get_session() as session:
        # The duplicate really is gone...
        assert session.run("MATCH (p:Person {id:'dup-1'}) RETURN p").single() is None
        # ...but its id still points somewhere.
        assert resolve_current_id(session, "dup-1") == "keep-1"


def test_person_merge_records_the_dup_id_in_the_log(it_db):
    # The MergeLog previously recorded only the duplicate's NAME, so there was no
    # way to answer "where did this id go?" at all.
    _two_persons(it_db)
    merge_person_records(keep="keep-1", dup="dup-1")

    rows = list(it_db.run_sql(
        "SELECT dup_id, keep_id FROM MergeLog WHERE keep_id = 'keep-1'"))
    assert rows and rows[0].get("dup_id") == "dup-1"


def test_person_profile_follows_the_redirect(it_db):
    # The user-visible payoff: a link to the merged-away person still opens the
    # surviving profile instead of 404ing.
    _two_persons(it_db)
    merge_person_records(keep="keep-1", dup="dup-1")

    profile = search.get_person_profile("dup-1")
    assert profile["person"]["id"] == "keep-1"


def test_entity_profile_follows_the_redirect(it_db):
    it_db.run_command("CREATE (e:Entity {id:'ent-keep', name:'Kept Co', type:'company'})")
    with db.get_session() as session:
        record_merge(session, old_id="ent-old", new_id="ent-keep", kind="Entity")

    profile = search.get_full_profile("ent-old")
    assert profile["entity"]["id"] == "ent-keep"


def test_a_live_id_is_never_redirected(it_db):
    # A stale or wrong redirect row must not hijack an id that still exists.
    it_db.run_command("CREATE (e:Entity {id:'live', name:'Live Co', type:'company'})")
    it_db.run_command("CREATE (e:Entity {id:'other', name:'Other Co', type:'company'})")
    with db.get_session() as session:
        record_merge(session, old_id="live", new_id="other", kind="Entity")

    assert search.get_full_profile("live")["entity"]["id"] == "live"


def test_missing_and_unredirected_id_still_404s(it_db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        search.get_full_profile("no-such-entity")
    assert exc.value.status_code == 404


# ── Entity merges leave one too ───────────────────────────────────────────────
#
# The first version of this feature wired the forwarding address into the PERSON
# merge only, while the entity merges in scraper/maintenance.py kept deleting
# nodes outright. That was the more damaging gap: entity auto-merge runs after
# every scrape, so with the Wikidata LEI bridge live a single "Refresh from
# Sources" would have merged a pair and destroyed the losing id with no redirect.

def _dup_pair_sharing_a_lei(it_db):
    """Two same-name entities sharing an LEI — a "definitive" group."""
    it_db.run_command(
        "CREATE (e:Entity {id:'ent-a', name:'Acme Corporation', name_normalized:'acme', "
        "type:'company', lei_id:'AAAA1111BBBB2222CCCC', name_credibility:92})")
    it_db.run_command(
        "CREATE (e:Entity {id:'ent-b', name:'Acme', name_normalized:'acme', "
        "type:'company', lei_id:'AAAA1111BBBB2222CCCC', name_credibility:80})")


def test_scoped_entity_automerge_leaves_a_forwarding_address(it_db):
    from app.scraper.maintenance import deduplicate_entities_for

    _dup_pair_sharing_a_lei(it_db)
    result = deduplicate_entities_for(["ent-a", "ent-b"], apply=True)
    assert result["entities_merged"] == 1

    with db.get_session() as session:
        # Higher credibility survives; the loser redirects to it.
        assert resolve_current_id(session, "ent-b") == "ent-a"


def test_identifier_entity_merge_leaves_a_forwarding_address(it_db):
    from app.scraper.maintenance import deduplicate_entities

    _dup_pair_sharing_a_lei(it_db)
    result = deduplicate_entities(limit=10)
    assert result["entities_merged"] >= 1

    with db.get_session() as session:
        assert resolve_current_id(session, "ent-b") == "ent-a"


def test_profile_of_a_merged_away_entity_opens_the_survivor(it_db):
    # End to end: the case the Microsoft merge will create.
    from app.scraper.maintenance import deduplicate_entities_for

    _dup_pair_sharing_a_lei(it_db)
    deduplicate_entities_for(["ent-a", "ent-b"], apply=True)

    profile = search.get_full_profile("ent-b")
    assert profile["entity"]["id"] == "ent-a"
