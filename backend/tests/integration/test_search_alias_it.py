"""
Real-ArcadeDB integration test: person search must match on aliases, not just
full_name — so a person merged from a differently-spelled duplicate (e.g. Larry
Page, alias "Lawrence Page") is still findable by the alias.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_person_search_matches_alias(it_db):
    from app.routers.search import search

    it_db.run_command("CREATE (:Person {id:'p', full_name:'Larry Page', alias:['Lawrence Page','Lawrence Edward Page']})")

    names = [r["node"].get("full_name") for r in search(q="lawrence", country=None)
             if r.get("type") == "Person"]
    assert "Larry Page" in names          # found via alias, not full_name


def test_person_search_still_matches_full_name(it_db):
    from app.routers.search import search

    it_db.run_command("CREATE (:Person {id:'p2', full_name:'Larry Page', alias:[]})")
    names = [r["node"].get("full_name") for r in search(q="larry", country=None)
             if r.get("type") == "Person"]
    assert "Larry Page" in names
