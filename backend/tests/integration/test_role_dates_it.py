"""SEC role edges carry a start (Form 3) and can be closed (a "Former …" Form 4
or an 8-K Item 5.02): `since` on create, backfilled on an undated edge and
never overwriting a stated one; `_close_role_sec` ends only the named seat, or
every open seat, and a later assertion of the same seat opens a new spell."""
import pytest

from app.scraper.sec_writer import _close_role_sec, _upsert_role_sec
from app.scraper import runner

pytestmark = pytest.mark.integration


@pytest.fixture()
def pair(it_db):
    it_db.run_command("CREATE (:Person {id: 'tc', full_name: 'Timothy D Cook', "
                      "search_text: 'Timothy D Cook'})")
    it_db.run_command("CREATE (:Entity {id: 'ap', name: 'Apple Inc.', "
                      "name_normalized: 'apple', search_text: 'Apple Inc.', type: 'company'})")
    return runner._ensure_source("SEC EDGAR", "https://www.sec.gov", 98, "regulator")


def _roles(it_db):
    return [(r["role"], r["since"], r["until"]) for r in it_db.run_command(
        "MATCH (:Person {id:'tc'})-[r:HAS_ROLE]->(:Entity {id:'ap'}) "
        "RETURN r.role AS role, r.since AS since, r.until AS until ORDER BY r.role, r.since")]


def test_since_is_written_on_create_and_backfilled_on_an_undated_edge(it_db, pair):
    _upsert_role_sec("tc", "ap", "CEO", pair)
    assert _roles(it_db) == [("CEO", None, None)]
    _upsert_role_sec("tc", "ap", "CEO", pair, since="2011-08-24")
    assert _roles(it_db) == [("CEO", "2011-08-24", None)], "one seat, now dated"
    _upsert_role_sec("tc", "ap", "CEO", pair, since="2012-01-01")
    assert _roles(it_db) == [("CEO", "2011-08-24", None)], "a stated date is never overwritten"


def test_closing_names_the_seat_and_leaves_the_others(it_db, pair):
    _upsert_role_sec("tc", "ap", "CEO", pair, since="2011-08-24")
    _upsert_role_sec("tc", "ap", "Director", pair, since="2011-08-24")
    closed = _close_role_sec("tc", "ap", "2026-09-01", role="Chief Executive Officer",
                             source_id=pair, source_url="https://www.sec.gov/x")
    assert closed == 1
    assert _roles(it_db) == [("CEO", "2011-08-24", "2026-09-01"), ("Director", "2011-08-24", None)]
    # Board Member ≡ Director: the synonym closes the seat too.
    assert _close_role_sec("tc", "ap", "2027-01-01", role="Board Member") == 1
    assert _roles(it_db)[1] == ("Director", "2011-08-24", "2027-01-01")


def test_closing_without_a_role_ends_every_open_seat_once(it_db, pair):
    _upsert_role_sec("tc", "ap", "CEO", pair)
    _upsert_role_sec("tc", "ap", "Director", pair)
    assert _close_role_sec("tc", "ap", "2026-09-01", source_id=pair) == 2
    assert _close_role_sec("tc", "ap", "2026-10-01", source_id=pair) == 0, "nothing open remains"
    assert {u for _, _, u in _roles(it_db)} == {"2026-09-01"}


def test_reasserting_a_closed_seat_opens_a_new_spell(it_db, pair):
    _upsert_role_sec("tc", "ap", "CEO", pair, since="1997-09-16")
    _close_role_sec("tc", "ap", "2011-08-24", role="CEO", source_id=pair)
    _upsert_role_sec("tc", "ap", "CEO", pair, since="2026-01-01")
    assert _roles(it_db) == [("CEO", "1997-09-16", "2011-08-24"), ("CEO", "2026-01-01", None)]


def test_a_reassertion_dated_before_the_close_does_not_reopen_the_seat(it_db, pair):
    # Cook's older Form 4 (filed 2026-08-20) still says CEO after the 8-K
    # closed the seat on 2026-09-01 — the same spell, not a return. Only a
    # filing dated after the close opens a new one.
    _upsert_role_sec("tc", "ap", "CEO", pair, since="2011-08-24")
    _close_role_sec("tc", "ap", "2026-09-01", role="CEO", source_id=pair)
    _upsert_role_sec("tc", "ap", "Chief Executive Officer", pair, source_date="2026-08-20")
    assert _roles(it_db) == [("CEO", "2011-08-24", "2026-09-01")], "no second spell"
    _upsert_role_sec("tc", "ap", "CEO", pair, source_date="2026-10-15")
    assert _roles(it_db) == [("CEO", "2011-08-24", "2026-09-01"), ("CEO", None, None)], \
        "a filing after the close is a return"


def test_a_closing_records_the_departure_as_a_claim(it_db, pair):
    _upsert_role_sec("tc", "ap", "CEO", pair)
    _close_role_sec("tc", "ap", "2026-09-01", role="CEO", source_id=pair,
                    source_url="https://www.sec.gov/8k")
    rows = it_db.run_command("MATCH (c:Claim) WHERE c.kind = 'role' AND c.until = '2026-09-01' "
                             "RETURN c.role AS role, c.source_url AS url")
    assert [(r["role"], r["url"]) for r in rows] == [("CEO", "https://www.sec.gov/8k")]
