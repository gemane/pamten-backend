"""
Real-ArcadeDB test for nominee/custodian detection: the shared `_entity` writer
flags a nominee-named entity inline (as every importer built on it does), and
flag_nominee_entities backfills existing ones via the FULL_TEXT index. The regex +
CONTAINSTEXT + boolean write can't be checked by the mocked suite.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_entity_writer_flags_nominee_inline(it_db):
    from app.scraper.bods import _BatchWriter, _entity

    batch = _BatchWriter()
    _entity(batch, "lei:LEI-NOMINEE", name="Talbot Nominees Limited",
            entity_type="company", country=None, founded=None, lei_id="LEI-NOMINEE",
            companies_house_id=None, source_id="src", credibility_score=90)
    _entity(batch, "lei:LEI-ACME", name="Acme Trading AG",
            entity_type="company", country=None, founded=None, lei_id="LEI-ACME",
            companies_house_id=None, source_id="src", credibility_score=90)
    batch.flush()

    nominee = it_db.run_sql("SELECT is_nominee FROM Entity WHERE lei_id = 'LEI-NOMINEE'")
    assert nominee and nominee[0]["is_nominee"] is True
    plain = it_db.run_sql("SELECT is_nominee FROM Entity WHERE lei_id = 'LEI-ACME'")
    assert plain and plain[0]["is_nominee"] is False


def test_backfill_flags_existing_nominees_only(it_db):
    from app.scraper import maintenance

    it_db.run_command("CREATE (:Entity {id:'e1', name:'UBS Nominees Pty Ltd', "
                      "search_text:'UBS Nominees Pty Ltd'})")
    it_db.run_command("CREATE (:Entity {id:'e2', name:'BlackRock Inc', "
                      "search_text:'BlackRock Inc'})")

    result = maintenance.flag_nominee_entities()
    assert result["flagged"] == 1

    assert it_db.run_sql("SELECT is_nominee FROM Entity WHERE id='e1'")[0]["is_nominee"] is True
    # a non-nominee is left untouched (never set)
    assert not it_db.run_sql("SELECT is_nominee FROM Entity WHERE id='e2'")[0].get("is_nominee")
