"""
Real-ArcadeDB test for `runner._ensure_source` — the get-or-create Source helper
that the refactor collapsed from five per-source functions into one parameterized
one. It's mocked everywhere it's used, so this pins the actual behaviour.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_ensure_source_get_or_create(it_db):
    from app.scraper.runner import _ensure_source

    # First call creates the Source with the passed fields.
    sid = _ensure_source("Wikidata", "https://www.wikidata.org", 80, "knowledge_base")
    rows = it_db.run_command(
        "MATCH (s:Source {name:'Wikidata'}) RETURN s.id AS id, s.url AS url, "
        "s.credibility_score AS cred, s.type AS type")
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["url"] == "https://www.wikidata.org"
    assert rows[0]["cred"] == 80
    assert rows[0]["type"] == "knowledge_base"

    # Second call for the same name returns the SAME id and makes no duplicate
    # (even with different url/credibility args — get wins over create).
    again = _ensure_source("Wikidata", "https://other.example", 99, "register")
    assert again == sid
    assert it_db.run_command(
        "MATCH (s:Source {name:'Wikidata'}) RETURN count(s) AS n")[0]["n"] == 1

    # A different name → a distinct node/id; type_ defaults to 'register'.
    gleif = _ensure_source("GLEIF", "https://www.gleif.org", 92)
    assert gleif != sid
    assert it_db.run_command(
        "MATCH (s:Source {name:'GLEIF'}) RETURN s.type AS t")[0]["t"] == "register"
    assert it_db.run_command("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] == 2
