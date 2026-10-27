"""
Roles for time travel — the Wikidata/generic and OpenCorporates writers,
against a real ArcadeDB.

An end date a source adds later never reached a seat we already held open
(a CEO stayed CEO forever), the undated-fill path dropped the end, and an
undated seat had no evidence date (dimmed in every year).
"""
import pytest

from app.scraper.graph_writer import _upsert_role
from app.scraper.runner import _upsert_role_oc

pytestmark = pytest.mark.integration


@pytest.fixture
def seat(it_db):
    it_db.run_command("CREATE (:Person {id:'p', full_name:'A Person'})")
    it_db.run_command("CREATE (:Entity {id:'c', name:'Company', type:'company'})")
    return it_db


def _seats(db):
    rows = db.run_command("MATCH (:Person {id:'p'})-[r:HAS_ROLE]->(:Entity {id:'c'}) "
                          "RETURN r.role AS role, r.since AS since, r.until AS until, "
                          "r.source_date AS sdate")
    return sorted(rows, key=lambda r: (r.get("since") or "", r.get("until") or ""))


class TestTheGenericWriter:
    def test_an_end_added_later_closes_the_open_seat(self, seat):
        _upsert_role("p", "c", "CEO", "wd", since="2011-01-01")
        _upsert_role("p", "c", "CEO", "wd", since="2011-01-01", until="2015-06-30")
        assert [(s["since"], s["until"]) for s in _seats(seat)] == [("2011-01-01", "2015-06-30")]

    def test_an_end_before_the_start_does_not_close_it(self, seat):
        _upsert_role("p", "c", "CEO", "wd", since="2018-01-01")
        _upsert_role("p", "c", "CEO", "wd", until="2015-06-30")      # undated, ended earlier
        assert [(s["since"], s["until"]) for s in _seats(seat)] == [("2018-01-01", None)]

    def test_filling_the_start_takes_the_end_too(self, seat):
        _upsert_role("p", "c", "CEO", "wd")                            # the person scrape: undated
        _upsert_role("p", "c", "CEO", "wd", since="2000-01-01", until="2008-12-31")
        assert [(s["since"], s["until"]) for s in _seats(seat)] == [("2000-01-01", "2008-12-31")]

    def test_an_undated_seat_is_dated_by_the_scrape(self, seat):
        _upsert_role("p", "c", "Director", "wd")
        sdate = _seats(seat)[0]["sdate"]
        assert sdate and len(sdate) == 10 and sdate >= "2026-01-01"


class TestOpenCorporates:
    def test_a_resignation_closes_the_open_seat(self, seat):
        _upsert_role_oc("p", "c", "Director", "2012-03-01", None, "oc")
        _upsert_role_oc("p", "c", "Director", "2012-03-01", "2019-09-30", "oc")
        assert [(s["since"], s["until"]) for s in _seats(seat)] == [("2012-03-01", "2019-09-30")]

    def test_a_resignation_before_the_start_does_not(self, seat):
        _upsert_role_oc("p", "c", "Director", "2020-01-01", None, "oc")
        _upsert_role_oc("p", "c", "Director", None, "2019-09-30", "oc")
        assert _seats(seat)[0]["until"] is None

    def test_an_earlier_start_for_the_held_seat_moves_it_back(self, seat):
        _upsert_role_oc("p", "c", "Director", "2020-01-05", None, "oc")
        _upsert_role_oc("p", "c", "Director", "2020-01-01", None, "oc")
        assert [(s["since"], s["until"]) for s in _seats(seat)] == [("2020-01-01", None)]

    def test_an_undated_seat_is_dated_by_the_scrape(self, seat):
        _upsert_role_oc("p", "c", "Secretary", None, None, "oc")
        assert (_seats(seat)[0]["sdate"] or "") >= "2026-01-01"
