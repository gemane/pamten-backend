"""
Real-ArcadeDB integration test for person-detail storage + backfill in
_upsert_person — exercises the list-valued SET (alias / nationalities) and the
size(COALESCE(...)) / CASE backfill Cypher, which the mocked unit tests can't
validate against ArcadeDB's dialect.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_upsert_person_stores_and_backfills_detail(it_db):
    from app.scraper.runner import _upsert_person
    from app.database import db

    # First scrape: a bare founder name, no detail yet.
    pid = _upsert_person("Elon Musk", nationality=None, description=None,
                         wikidata_id="Q317521")
    with db.get_session() as s:
        p = s.run("MATCH (p:Person {id: $id}) RETURN p", id=pid).single()["p"]
    assert list(p["alias"]) == []
    assert list(p["nationalities"]) == []

    # Re-scrape, now enriched: same wikidata_id → backfills the blanks and must
    # return the SAME id (no duplicate person).
    pid2 = _upsert_person("Elon Musk", nationality=None, description="entrepreneur",
                          wikidata_id="Q317521",
                          birth_date="1971-06-28", death_date=None,
                          aliases=["Elon", "Technoking"], nationalities=["US", "CA"])
    assert pid2 == pid
    with db.get_session() as s:
        p = s.run("MATCH (p:Person {id: $id}) RETURN p", id=pid).single()["p"]
    assert p["birth_date"] == "1971-06-28"
    assert p["description"] == "entrepreneur"
    assert p["nationality"] == "US"                      # derived from nationalities[0]
    assert list(p["alias"]) == ["Elon", "Technoking"]
    assert list(p["nationalities"]) == ["US", "CA"]


def test_existing_detail_is_not_overwritten(it_db):
    from app.scraper.runner import _upsert_person
    from app.database import db

    pid = _upsert_person("Ada Lovelace", nationality=None, description="mathematician",
                         wikidata_id="Q7259",
                         birth_date="1815-12-10", aliases=["Ada"], nationalities=["GB"])
    # A later scrape with different detail must NOT clobber what's already stored.
    _upsert_person("Ada Lovelace", nationality=None, description="poet",
                   wikidata_id="Q7259",
                   birth_date="1900-01-01", aliases=["Countess"], nationalities=["US"])
    with db.get_session() as s:
        p = s.run("MATCH (p:Person {id: $id}) RETURN p", id=pid).single()["p"]
    assert p["birth_date"] == "1815-12-10"
    assert p["description"] == "mathematician"
    assert list(p["alias"]) == ["Ada"]
    assert list(p["nationalities"]) == ["GB"]
