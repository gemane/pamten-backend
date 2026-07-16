"""
Real-ArcadeDB integration test for the BODS importer:
  - relationship edges are written INLINE while streaming (no separate end-pass
    that an interrupted run would skip), so a partial run still yields connected
    data;
  - a Person owner (UK PSC "significant control") gets a real (Person)-[:OWNS]->
    (Entity) edge — not silently dropped by an owner-must-be-Entity match.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _entity(rid, name, lei):
    return {"recordType": "entity", "recordId": rid,
            "recordDetails": {"name": name, "jurisdiction": {"code": "AT"},
                              "entityType": {"type": "registeredEntity"},
                              "identifiers": [{"scheme": "XI-LEI", "id": lei}]}}


def _person(rid, full):
    return {"recordType": "person", "recordId": rid,
            "recordDetails": {"names": [{"type": "legal", "fullName": full}]}}


def _rel(subject, party, party_kind, stake):
    key = "describedByPersonStatement" if party_kind == "person" else "describedByEntityStatement"
    return {"recordType": "relationship", "recordId": f"R-{subject}-{party}",
            "recordDetails": {"subject": {"describedByEntityStatement": subject},
                              "interestedParty": {key: party},
                              "interests": [{"type": "shareholding", "share": {"exact": stake}}]}}


def test_run_import_writes_person_and_entity_ownership_inline(it_db):
    from app.scraper.bods import _run_import

    stmts = [
        _entity("E1", "Windco AG", "LEI-WIND"),
        _person("P1", "Owner One"),
        _rel("E1", "P1", "person", 75),        # person → company (PSC-style)
        _entity("E2", "Parent Holding", "LEI-PARENT"),
        _rel("E1", "E2", "entity", 30),        # company → company (GLEIF-style)
    ]
    counts = _run_import(iter(stmts), source_id="src", credibility_score=97,
                         limit=None, filter_jurisdiction=None)

    assert counts["entities"] == 2
    assert counts["persons"] == 1
    assert counts["relationships"] == 2       # both edges written in the single stream

    person_owns = it_db.run_command(
        "MATCH (p:Person {full_name:'Owner One'})-[o:OWNS]->(e:Entity {name:'Windco AG'}) "
        "RETURN o.stake_percent AS s")
    assert person_owns and person_owns[0]["s"] == 75    # the PSC person-owner edge exists

    entity_owns = it_db.run_command(
        "MATCH (a:Entity {name:'Parent Holding'})-[o:OWNS]->(e:Entity {name:'Windco AG'}) "
        "RETURN o.stake_percent AS s")
    assert entity_owns and entity_owns[0]["s"] == 30


def test_relationship_before_its_foreign_parent_uses_placeholder(it_db):
    """An imported company owned by a not-yet-seen foreign parent still gets an
    edge (to a placeholder), inline — nothing is lost."""
    from app.scraper.bods import _run_import

    stmts = [
        _entity("E1", "Local Sub GmbH", "LEI-LOCAL"),
        _rel("E1", "XI-LEI-FOREIGN123", "entity", 100),   # parent never appears
    ]
    counts = _run_import(iter(stmts), source_id="src", credibility_score=92,
                         limit=None, filter_jurisdiction=None)
    assert counts["relationships"] == 1
    owned = it_db.run_command(
        "MATCH (a:Entity)-[:OWNS]->(e:Entity {name:'Local Sub GmbH'}) RETURN a.lei_id AS lei")
    assert owned and owned[0]["lei"] == "FOREIGN123"      # placeholder carries the LEI
