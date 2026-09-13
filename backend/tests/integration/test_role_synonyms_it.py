"""One board seat, two vocabularies: the writers must recognise Wikidata's
"Board Member" and SEC's "Director" as the same position — one edge, labelled
by the most credible asserter, corroborated by both — while different
positions never corroborate each other."""
import pytest

from app.scraper.graph_writer import _upsert_role
from app.scraper.sec_writer import _upsert_role_sec
from app.scraper import runner

pytestmark = pytest.mark.integration


@pytest.fixture()
def pair(it_db):
    it_db.run_command("CREATE (:Person {id: 'pm', full_name: 'Elon Musk', "
                      "search_text: 'Elon Musk'})")
    it_db.run_command("CREATE (:Entity {id: 'sx', name: 'SpaceX', "
                      "name_normalized: 'spacex', search_text: 'SpaceX', type: 'company'})")
    wd = runner._ensure_source("Wikidata", "https://www.wikidata.org", 80, "knowledge_base")
    sec = runner._ensure_source("SEC EDGAR", "https://www.sec.gov", 98, "regulator")
    return {"wd": wd, "sec": sec}


def _roles(it_db):
    return it_db.run_command(
        "MATCH (:Person {id:'pm'})-[r:HAS_ROLE]->(:Entity {id:'sx'}) "
        "RETURN r.role AS role, r.credibility_score AS cred ORDER BY r.role")


def test_a_statutory_source_takes_over_the_community_label(it_db, pair):
    _upsert_role("pm", "sx", "Board Member", pair["wd"], credibility_score=80)
    _upsert_role_sec("pm", "sx", "Director", pair["sec"], credibility_score=98)
    rows = _roles(it_db)
    assert [(r["role"], r["cred"]) for r in rows] == [("Director", 98)], \
        "one seat, one edge, the statutory word"


def test_a_community_reassertion_leaves_the_statutory_label_alone(it_db, pair):
    _upsert_role_sec("pm", "sx", "Director", pair["sec"], credibility_score=98)
    _upsert_role("pm", "sx", "Board Member", pair["wd"], credibility_score=80)
    rows = _roles(it_db)
    assert [(r["role"], r["cred"]) for r in rows] == [("Director", 98)]


def test_different_positions_stay_separate_edges(it_db, pair):
    _upsert_role("pm", "sx", "CEO", pair["wd"], credibility_score=80)
    _upsert_role_sec("pm", "sx", "Director", pair["sec"], credibility_score=98)
    assert [r["role"] for r in _roles(it_db)] == ["CEO", "Director"]


def test_corroboration_counts_per_position_not_per_pair(it_db, pair):
    from app.routers.search import _corroborations_for, _attach_corroboration
    _upsert_role("pm", "sx", "Board Member", pair["wd"], credibility_score=80)
    _upsert_role("pm", "sx", "CEO", pair["wd"], credibility_score=80)
    _upsert_role_sec("pm", "sx", "Director", pair["sec"], credibility_score=98)
    claims = _corroborations_for("sx")

    director = _attach_corroboration({"role": "Director"}, claims, "pm", "sx", "role")
    assert director["corroborations"] == 2, "Board Member + Director corroborate"
    assert director["asserted_by"] == ["SEC EDGAR", "Wikidata"]

    ceo = _attach_corroboration({"role": "CEO"}, claims, "pm", "sx", "role")
    assert ceo["corroborations"] == 1, "SEC's Director says nothing about CEO"
    assert ceo["asserted_by"] == ["Wikidata"], "community-only shows as such"


def test_the_heal_merges_existing_synonym_duplicates(it_db, pair):
    from types import SimpleNamespace
    import manage
    it_db.run_command(
        "MATCH (p:Person {id:'pm'}), (e:Entity {id:'sx'}) "
        "CREATE (p)-[:HAS_ROLE {role: 'Board Member', since: '2002-05-06', "
        "credibility_score: 80}]->(e)")
    it_db.run_command(
        "MATCH (p:Person {id:'pm'}), (e:Entity {id:'sx'}) "
        "CREATE (p)-[:HAS_ROLE {role: 'Director', credibility_score: 98}]->(e)")
    manage.cmd_dedupe_role_synonyms(SimpleNamespace(dry_run=False))
    rows = it_db.run_command(
        "MATCH (:Person {id:'pm'})-[r:HAS_ROLE]->(:Entity {id:'sx'}) "
        "RETURN r.role AS role, r.since AS since")
    assert len(rows) == 1
    assert rows[0]["role"] == "Director"
    assert rows[0]["since"] == "2002-05-06", "the loser's date backfills the winner"
