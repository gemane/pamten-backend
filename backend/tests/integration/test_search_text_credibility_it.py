"""The search index must carry the WINNING name: a lower-credibility source
enriching a register entity keeps the legal name searchable (the SpaceX bug —
Wikidata's label+description evicted "Space Exploration Technologies Corp."
from search_text and the resolver returned News Corp)."""
import pytest

from app.scraper import runner

pytestmark = pytest.mark.integration


def _gleif_company(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'sx', name: 'SPACE EXPLORATION TECHNOLOGIES CORP.', "
        "name_normalized: 'space exploration technologies', "
        "search_text: 'SPACE EXPLORATION TECHNOLOGIES CORP.', name_credibility: 90, "
        "lei_id: 'LEI549300TEST0000001', type: 'company'})")


def _wikidata_enrich(source_id):
    return runner._upsert_entity(
        "SpaceX", "company", None, None, None,
        "American spaceflight and AI company", "Q193701",
        lei="LEI549300TEST0000001",
        aliases=["Space Exploration Technologies"], source_id=source_id,
        credibility_score=60)


def test_a_losing_name_does_not_evict_the_legal_name_from_search(it_db):
    _gleif_company(it_db)
    src = runner._ensure_source(runner.WIKIDATA_SOURCE_NAME,
                                runner.WIKIDATA_SOURCE_URL, 60, "knowledge_base")
    eid = _wikidata_enrich(src)
    assert eid == "sx", "the LEI bridge attaches to the register node"
    row = it_db.run_sql("SELECT name, name_normalized, search_text FROM Entity "
                        "WHERE id = 'sx'")[0]
    assert row["name"] == "SPACE EXPLORATION TECHNOLOGIES CORP."
    assert row["name_normalized"] == "space exploration technologies"
    st = row["search_text"].lower()
    assert "exploration" in st and "technologies" in st, "legal name stays indexed"
    assert "spacex" in st, "the losing label still joins the index"
    assert "spaceflight" in st, "the description still enriches the index"


def test_a_winning_name_still_takes_over_the_index(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'lo', name: 'Oldname Ltd', name_normalized: 'oldname', "
        "search_text: 'Oldname Ltd', name_credibility: 40, "
        "wikidata_id: 'Q_LOW', type: 'company'})")
    src = runner._ensure_source(runner.WIKIDATA_SOURCE_NAME,
                                runner.WIKIDATA_SOURCE_URL, 60, "knowledge_base")
    runner._upsert_entity("Newname GmbH", "company", None, None, None, None,
                          "Q_LOW", source_id=src, credibility_score=60)
    row = it_db.run_sql("SELECT name, name_normalized, search_text FROM Entity "
                        "WHERE id = 'lo'")[0]
    assert row["name"] == "Newname GmbH"
    assert row["name_normalized"] == "newname gmbh"
    assert "newname" in row["search_text"].lower()
