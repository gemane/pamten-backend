"""Real-ArcadeDB tests for entity keep-separate and the entity merge log.

Entities had neither, which was backwards: an entity merge is the riskier of the
two kinds — it runs automatically during scraping and it DETACH DELETEs the
loser — yet a moderator could not mark two companies as different, and nothing
recorded what had been merged. Persons had both from the start.

Run against a real database because the thing under test destroys nodes: a
mocked session would happily agree that a merge did the right thing while the
Cypher deleted the wrong row, or nothing at all.
"""
import pytest

from app.db.arcadedb import run_query, run_sql
from app.scraper import maintenance

pytestmark = pytest.mark.integration


def _entity(it_db, eid: str, name: str, **props) -> None:
    sets = "".join(f", {k}: '{v}'" for k, v in props.items())
    it_db.run_command(
        f"CREATE (:Entity {{id: '{eid}', name: '{name}', "
        f"name_normalized: '{name.lower()}'{sets}}})")


def _keep_separate(it_db, a: str, b: str) -> None:
    it_db.run_command(
        "MATCH (a:Entity {id:$a}), (b:Entity {id:$b}) "
        "MERGE (a)-[r:NOT_DUPLICATE]->(b) SET r.at = '2026-08-08T00:00:00Z'",
        {"a": a, "b": b})


def _ids() -> set:
    return {r["id"] for r in run_query("MATCH (e:Entity) RETURN e.id AS id")}


# ── Keep-separate is honoured by the auto-merge ───────────────────────────────

def test_two_companies_sharing_an_id_normally_merge(it_db):
    """The baseline the next test contrasts with."""
    _entity(it_db, "a", "Acme", lei_id="LEI1")
    _entity(it_db, "b", "Acme", lei_id="LEI1")

    maintenance.deduplicate_entities(limit=None)

    assert len(_ids()) == 1


def test_a_kept_separate_pair_is_not_merged(it_db):
    _entity(it_db, "a", "Acme", lei_id="LEI1")
    _entity(it_db, "b", "Acme", lei_id="LEI1")
    _keep_separate(it_db, "a", "b")

    maintenance.deduplicate_entities(limit=None)

    assert _ids() == {"a", "b"}


def test_keep_separate_is_checked_per_pair_not_per_group(it_db):
    """A third same-id company must not drag a node someone explicitly separated
    into a destructive merge. The person side drops a group only when *every*
    pair is dismissed; for entities that would delete a node a human protected."""
    _entity(it_db, "a", "Acme", lei_id="LEI1")
    _entity(it_db, "b", "Acme", lei_id="LEI1")
    _entity(it_db, "c", "Acme", lei_id="LEI1")
    _keep_separate(it_db, "a", "b")

    maintenance.deduplicate_entities(limit=None)

    remaining = _ids()
    assert "b" in remaining, "the protected node was deleted"
    assert "c" not in remaining, "an unprotected duplicate should still merge"


def test_the_scoped_path_honours_keep_separate_too(it_db):
    """Both merge paths must agree — the scoped one runs after every scrape."""
    _entity(it_db, "a", "Acme", lei_id="LEI1")
    _entity(it_db, "b", "Acme", lei_id="LEI1")
    _keep_separate(it_db, "a", "b")

    maintenance.deduplicate_entities_for(["a", "b"], apply=True)

    assert _ids() == {"a", "b"}


def test_no_keep_separate_marks_costs_nothing(it_db):
    # The lookup short-circuits on a count over the edge type; with none, the
    # merge must behave exactly as before.
    _entity(it_db, "a", "Acme", lei_id="LEI1")
    _entity(it_db, "b", "Acme", lei_id="LEI1")

    result = maintenance.deduplicate_entities(limit=None)

    assert result["entities_merged"] == 1


# ── The merge log ─────────────────────────────────────────────────────────────

def _log(kind: str) -> list[dict]:
    return run_sql(f"SELECT FROM MergeLog WHERE kind = '{kind}'")


def test_an_entity_merge_is_recorded(it_db):
    _entity(it_db, "keep", "Acme Ltd", lei_id="LEI1")
    _entity(it_db, "dead", "Acme Limited", lei_id="LEI1")

    maintenance.deduplicate_entities(limit=None)

    rows = _log("entity")
    assert len(rows) == 1
    assert rows[0]["dup_id"] == "dead" or rows[0]["keep_id"] == "dead"


def test_the_log_names_both_sides(it_db):
    _entity(it_db, "a", "Acme", lei_id="LEI1")
    _entity(it_db, "b", "Acme", lei_id="LEI1")

    maintenance.deduplicate_entities(limit=None)

    row = _log("entity")[0]
    assert row["keep_name"] and row["dup_name"]
    assert row["at"]
    assert row["count"] == 1


def test_repeated_merges_of_a_rescraped_duplicate_bump_the_count(it_db):
    """Scraping recreates duplicates, so the log must not grow a row per run.

    Each round adds a *fresh* duplicate node carrying the same name, which is
    what a re-scrape actually produces — a new id for the same company. The
    survivor keeps its id, so the log's (keep_id, dup_name) key is stable and
    `count` accumulates instead of the log growing a row per import.

    Ids are chosen so the intended survivor sorts first: the tie-break is
    (credibility, verified, id), and all three are equal here except the id.
    """
    _entity(it_db, "aaa-keep", "Acme", lei_id="LEI1")
    for n in range(3):
        _entity(it_db, f"zzz-dup{n}", "Acme", lei_id="LEI1")
        maintenance.deduplicate_entities(limit=None)

    assert _ids() == {"aaa-keep"}
    rows = _log("entity")
    assert len(rows) == 1, f"expected one deduped log row, got {len(rows)}"
    assert rows[0]["count"] == 3


def test_entity_and_person_logs_do_not_mix(it_db):
    _entity(it_db, "a", "Acme", lei_id="LEI1")
    _entity(it_db, "b", "Acme", lei_id="LEI1")
    maintenance.deduplicate_entities(limit=None)

    assert len(_log("entity")) == 1
    assert _log("person") == []


# ── The merge still does everything it did ────────────────────────────────────

def test_the_merge_still_leaves_a_forwarding_address(it_db):
    """The four steps were duplicated across two call sites and kept drifting —
    the forwarding address was missing from one until #204, the property
    carry-over until #205. They now run from one helper; prove it still does all
    of it."""
    from app.merged_ids import resolve_current_id

    _entity(it_db, "keep", "Acme", lei_id="LEI1", sec_cik="123")
    _entity(it_db, "dead", "Acme", lei_id="LEI1", wikidata_id="Q1")

    maintenance.deduplicate_entities(limit=None)

    survivor = next(iter(_ids()))
    with_session = run_query(
        "MATCH (e:Entity {id:$id}) RETURN e.wikidata_id AS wd, e.sec_cik AS cik",
        {"id": survivor})[0]
    # the loser's identifier came across
    assert with_session["wd"] == "Q1"
    from app.database import db
    with db.get_session() as session:
        dead = "dead" if survivor == "keep" else "keep"
        assert resolve_current_id(session, dead) == survivor
