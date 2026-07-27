"""
Real-ArcadeDB test for nominee/custodian detection: the BODS importer flags a
nominee-named entity, and flag_nominee_entities backfills existing ones via the
FULL_TEXT index. The regex + CONTAINSTEXT + boolean write can't be checked by the
mocked suite.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_bods_import_flags_nominee_entity(it_db):
    from app.scraper.bods import _run_import

    stmts = [{
        "recordType": "entity", "recordId": "E1",
        "recordDetails": {"name": "Talbot Nominees Limited",
                          "identifiers": [{"scheme": "XI-LEI", "id": "LEI-NOMINEE"}]},
    }, {
        "recordType": "entity", "recordId": "E2",
        "recordDetails": {"name": "Acme Trading AG",
                          "identifiers": [{"scheme": "XI-LEI", "id": "LEI-ACME"}]},
    }]
    _run_import(iter(stmts), source_id="src", credibility_score=90,
                limit=None, filter_jurisdiction=None)

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
