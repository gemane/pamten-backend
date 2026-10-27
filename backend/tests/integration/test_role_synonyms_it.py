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


def test_a_filing_dated_a_day_from_the_community_seat_is_that_seat(it_db, pair):
    # Ronald Sugar at Apple: Wikidata says 2010-11-17, his Form 4 2010-11-16.
    _upsert_role("pm", "sx", "Board Member", pair["wd"], since="2010-11-17", credibility_score=80)
    _upsert_role_sec("pm", "sx", "Director", pair["sec"], since="2010-11-16", credibility_score=98)
    rows = it_db.run_command(
        "MATCH (:Person {id:'pm'})-[r:HAS_ROLE]->(:Entity {id:'sx'}) "
        "RETURN r.role AS role, r.since AS since")
    assert [(r["role"], r["since"]) for r in rows] == [("Director", "2010-11-16")], \
        "one seat, the statutory word, the earlier day"


def test_the_filing_first_then_the_community_date_a_day_later(it_db, pair):
    _upsert_role_sec("pm", "sx", "Director", pair["sec"], since="2010-11-16", credibility_score=98)
    _upsert_role("pm", "sx", "Board Member", pair["wd"], since="2010-11-17", credibility_score=80)
    rows = it_db.run_command(
        "MATCH (:Person {id:'pm'})-[r:HAS_ROLE]->(:Entity {id:'sx'}) "
        "RETURN r.role AS role, r.since AS since")
    assert [(r["role"], r["since"]) for r in rows] == [("Director", "2010-11-16")]


def test_a_filing_after_an_ended_spell_opens_a_new_one(it_db, pair):
    _upsert_role("pm", "sx", "CEO", pair["wd"], since="2008-10-01", until="2018-09-07",
                 credibility_score=80)
    _upsert_role_sec("pm", "sx", "CEO", pair["sec"], since="2021-03-01", credibility_score=98)
    rows = it_db.run_command(
        "MATCH (:Person {id:'pm'})-[r:HAS_ROLE]->(:Entity {id:'sx'}) "
        "RETURN r.since AS since, r.until AS until ORDER BY r.since")
    assert [(r["since"], r["until"]) for r in rows] == [
        ("2008-10-01", "2018-09-07"), ("2021-03-01", None)]


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
