"""
Real-ArcadeDB test for the scoped, high-confidence post-scrape entity auto-merge
(deduplicate_entities_for) — the entity twin of the person auto-dedup.

Covers: the touched-entity collector records upserted ids; same-name groups that
are `definitive` (shared hard id) or `high` (same registered address) are merged
and their edges migrated onto the survivor; `medium` (name + country/year only) is
left for review; unrelated / unique names are untouched; empty seed = no work.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _entity(it_db, eid, name_normalized, **props):
    fields = {"id": eid, "name": eid, "name_normalized": name_normalized, **props}
    assigns = ", ".join(f"{k}: ${k}" for k in fields)
    it_db.run_command(f"CREATE (e:Entity {{{assigns}}})", fields)


def test_entity_autodedup_scoped_high_confidence(it_db):
    from app.scraper.maintenance import deduplicate_entities_for

    # definitive: same normalized name + shared hard id (wikidata_id)
    _entity(it_db, "acme-keep", "acme", wikidata_id="Q1", name_credibility=90)
    _entity(it_db, "acme-dup", "acme", wikidata_id="Q1", name_credibility=10)
    # an edge on the loser must move to the survivor
    it_db.run_command("CREATE (o:Entity {id:'owner', name:'Owner', name_normalized:'owner'})")
    it_db.run_command("MATCH (o:Entity {id:'owner'}), (e:Entity {id:'acme-dup'}) "
                      "CREATE (o)-[:OWNS {source_id:'s'}]->(e)")

    # high: same normalized name + same registered address, different LEIs
    _entity(it_db, "beta-1", "beta", registered_address="1 main st", lei_id="L1")
    _entity(it_db, "beta-2", "beta", registered_address="1 main st", lei_id="L2")

    # medium: same name + same country/year only → left for review, NOT merged
    _entity(it_db, "gamma-1", "gamma", country="US", founded="1990")
    _entity(it_db, "gamma-2", "gamma", country="US", founded="1990")

    # unique name → nothing to merge
    _entity(it_db, "delta-1", "delta")

    touched = ["acme-keep", "beta-1", "gamma-1", "delta-1"]
    res = deduplicate_entities_for(touched, apply=True)

    assert res["entities_merged"] == 2          # acme (definitive) + beta (high)
    assert res["needs_review"] == 1             # gamma (medium)

    def count(nn):
        return it_db.run_command(
            "MATCH (e:Entity {name_normalized:$n}) RETURN count(e) AS c", {"n": nn})[0]["c"]

    assert count("acme") == 1                   # merged
    assert count("beta") == 1                   # merged
    assert count("gamma") == 2                  # untouched (review)
    assert count("delta") == 1

    # the loser's OWNS edge was migrated onto the survivor
    survivor = it_db.run_command(
        "MATCH (o:Entity {id:'owner'})-[:OWNS]->(e:Entity) RETURN e.id AS id")
    assert survivor and survivor[0]["id"] == "acme-keep"

    # empty touched set → no work
    assert deduplicate_entities_for([], apply=True)["entities_merged"] == 0


def test_touched_entity_collector_records_upserts(it_db):
    from app.scraper.runner import _record_touched_entity, _touched_entities, _upsert_entity_by_name

    token = _touched_entities.set(set())
    a = _upsert_entity_by_name("Acme Holdings", source_id="s")
    b = _upsert_entity_by_name("Beta Corp", source_id="s")
    touched = set(_touched_entities.get())
    _touched_entities.reset(token)
    assert {a, b} <= touched

    # outside a collector context it's a harmless no-op that returns the id
    assert _record_touched_entity("x") == "x"
