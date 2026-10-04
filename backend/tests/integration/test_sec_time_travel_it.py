"""
SEC edges for time travel — the write path against a real ArcadeDB.

What the time-travel audit found in the SEC writer: an amendment's date
written as a stated start (a holder since 2005 "since 2026"), Form 4 trade
dates as starts, a close that rewrote every period of the pair, an old 8-K
departure closing a newer seat, a detach that kept the withdrawn `since` as a
stated start, and Exhibit 21 subsidiaries that stayed current after the list
dropped them.
"""
import pytest

from app.scraper.sec_writer import (_close_role_sec, _upsert_owns_sec, _upsert_role_sec,
                                    detach_owns_sec, mark_ex21_stale)

pytestmark = pytest.mark.integration


@pytest.fixture
def pair(it_db):
    it_db.run_command("CREATE (:Entity {id:'h', name:'Holder', type:'fund'})")
    it_db.run_command("CREATE (:Entity {id:'c', name:'Company', type:'company'})")
    return it_db


def _edges(db, a="h", b="c"):
    rows = db.run_command(
        "MATCH (x {id:$a})-[r:OWNS]->(y {id:$b}) RETURN r.since AS since, "
        "r.since_basis AS basis, r.until AS until, r.stake_percent AS stake",
        {"a": a, "b": b})
    return sorted(rows, key=lambda r: (r.get("since") or "", r.get("until") or ""))


def _write(**kw):
    base = dict(owner_id="h", owned_id="c", source_id="sec", ownership_type="minority",
                stake_percent=6.0, filing_type="13G/A", source_url="https://sec.example.test/f")
    base.update(kw)
    _upsert_owns_sec(**base)


class TestTheStart:
    def test_an_amendment_is_a_lower_bound_and_an_original_states_the_start(self, pair):
        _write(file_date="2026-02-10", since_date="2025-12-31", since_basis="amendment")
        assert [(e["since"], e["basis"]) for e in _edges(pair)] == [("2025-12-31", "amendment")]
        # a later read finds the original schedule: the start moves EARLIER and is stated
        _write(file_date="2005-03-01", since_date="2005-02-20", since_basis=None, filing_type="13G")
        assert [(e["since"], e["basis"]) for e in _edges(pair)] == [("2005-02-20", None)]
        # a newer amendment never moves it later again
        _write(file_date="2027-02-10", since_date="2026-12-31", since_basis="amendment")
        assert [(e["since"], e["basis"]) for e in _edges(pair)] == [("2005-02-20", None)]

    def test_a_form_4_holding_has_no_start(self, pair):
        _write(file_date="2026-05-01", filing_type="Form 4", filing_dates_the_stake=False)
        assert _edges(pair)[0]["since"] is None


class TestClosingTheRightPeriod:
    def test_an_exit_closes_the_open_period_only(self, pair):
        pair.run_command("MATCH (a {id:'h'}),(b {id:'c'}) CREATE (a)-[:OWNS {source_id:'sec', "
                         "since:'2010-01-01', until:'2015-06-30', stake_percent:7.0}]->(b)")
        pair.run_command("MATCH (a {id:'h'}),(b {id:'c'}) CREATE (a)-[:OWNS {source_id:'sec', "
                         "since:'2020-01-01', stake_percent:6.0}]->(b)")
        _write(file_date="2023-04-01", until="2023-03-31")
        assert [(e["since"], e["until"]) for e in _edges(pair)] == [
            ("2010-01-01", "2015-06-30"), ("2020-01-01", "2023-03-31")]

    def test_re_reading_an_old_exit_does_not_close_a_newer_period(self, pair):
        pair.run_command("MATCH (a {id:'h'}),(b {id:'c'}) CREATE (a)-[:OWNS {source_id:'sec', "
                         "since:'2010-01-01', until:'2015-06-30', stake_percent:7.0}]->(b)")
        pair.run_command("MATCH (a {id:'h'}),(b {id:'c'}) CREATE (a)-[:OWNS {source_id:'sec', "
                         "since:'2020-01-01', stake_percent:6.0}]->(b)")
        _write(file_date="2015-07-10", until="2015-06-30", stake_percent=7.0)
        assert [(e["since"], e["until"]) for e in _edges(pair)] == [
            ("2010-01-01", "2015-06-30"), ("2020-01-01", None)]


    def test_an_exit_from_before_the_open_period_leaves_it_open(self, pair):
        # no closed period carries this end, so the open one is the only
        # candidate — and it began after the exit: it must not be closed
        pair.run_command("MATCH (a {id:'h'}),(b {id:'c'}) CREATE (a)-[:OWNS {source_id:'sec', "
                         "since:'2020-01-01', stake_percent:6.0}]->(b)")
        _write(file_date="2015-07-10", until="2015-06-30", stake_percent=7.0)
        assert ("2020-01-01", None) in [(e["since"], e["until"]) for e in _edges(pair)]


class TestRoles:
    @pytest.fixture
    def seat(self, it_db):
        it_db.run_command("CREATE (:Person {id:'p', full_name:'Returning Officer'})")
        it_db.run_command("CREATE (:Entity {id:'c', name:'Company', type:'company'})")
        _upsert_role_sec("p", "c", "CEO", source_id="sec", since="2024-02-01",
                         source_url="https://sec.example.test/f3")
        return it_db

    def test_an_old_departure_does_not_close_a_newer_seat(self, seat):
        assert _close_role_sec("p", "c", "2022-05-01", role="CEO", source_id="sec") == 0
        rows = seat.run_command("MATCH (:Person {id:'p'})-[r:HAS_ROLE]->() RETURN r.until AS u")
        assert rows[0].get("u") is None

    def test_a_departure_after_the_start_closes_it(self, seat):
        assert _close_role_sec("p", "c", "2025-05-01", role="CEO", source_id="sec") == 1


def test_a_detach_takes_the_start_from_the_claims_that_remain(pair):
    from app.claims import KIND_OWNS, record_claim
    pair.run_command("MATCH (a {id:'h'}),(b {id:'c'}) CREATE (a)-[:OWNS {source_id:'sec', "
                     "since:'2013-12-31', since_basis:'first_listed', stake_percent:100}]->(b)")
    record_claim(kind=KIND_OWNS, from_id="h", to_id="c", source_id="sec", since="2013-12-31",
                 since_basis="first_listed", stake_percent=100)
    record_claim(kind=KIND_OWNS, from_id="h", to_id="c", source_id="gleif", since="2016-08-01",
                 ownership_type="controlling", credibility_score=92)
    assert detach_owns_sec("h", "c", "sec") == "reassigned"
    edge = _edges(pair)[0]
    # GLEIF's own stated start — not SEC's withdrawn lower bound turned "stated"
    assert (edge["since"], edge["basis"]) == ("2016-08-01", None)


def test_a_subsidiary_the_newer_list_drops_is_dimmed_not_closed(it_db):
    it_db.run_command("CREATE (:Entity {id:'p', name:'Parent', type:'company'})")
    for sid in ("kept", "dropped", "other"):
        it_db.run_command(f"CREATE (:Entity {{id:'{sid}', name:'{sid}', type:'company'}})")
    old = "https://www.sec.gov/Archives/edgar/data/123/0001/ex21.htm"
    new = "https://www.sec.gov/Archives/edgar/data/123/0002/ex21.htm"
    rows = (("kept", new, "2025-12-31", "EX-21"), ("dropped", old, "2024-12-31", "EX-21"),
            ("other", "https://www.sec.gov/Archives/edgar/data/999/0003/ex21.htm", "2024-12-31", "EX-21"))
    for sid, url, d, ft in rows:
        it_db.run_command("MATCH (a {id:'p'}),(b {id:$s}) CREATE (a)-[:OWNS {source_id:'sec', "
                          "source_url:$u, source_date:$d, filing_type:$f}]->(b)",
                          {"s": sid, "u": url, "d": d, "f": ft})
    assert mark_ex21_stale({"p"}, new, "2025-12-31") == 1
    got = {r["b"]: (r.get("stale"), r.get("until")) for r in it_db.run_command(
        "MATCH (:Entity {id:'p'})-[r:OWNS]->(b) RETURN b.id AS b, r.stale AS stale, r.until AS until")}
    assert got["dropped"] == (True, None)
    assert not got["kept"][0] and not got["other"][0]      # listed now / another filer's exhibit


def test_the_profile_keeps_a_lower_bound_before_its_date(pair):
    # any basis but newly_listed is a lower bound: shown (dimmed) before the date
    from app.routers.search import get_full_profile
    pair.run_command("MATCH (a {id:'h'}),(b {id:'c'}) CREATE (a)-[:OWNS {source_id:'sec', "
                     "since:'2025-12-31', since_basis:'amendment', stake_percent:6.0}]->(b)")
    owners = get_full_profile("c", as_of="2019-12-31")["owners"]
    assert [o["owner"]["id"] for o in owners] == ["h"]
    pair.run_command("MATCH (a {id:'h'})-[r:OWNS]->(b {id:'c'}) SET r.since_basis = 'newly_listed'")
    assert get_full_profile("c", as_of="2019-12-31")["owners"] == []


def test_the_heal_repairs_what_the_old_writer_left(it_db):
    from app.claims import KIND_OWNS, KIND_ROLE, record_claim
    from app.scraper import runner
    from app.scraper.time_travel_heal import heal_sec_dates
    sec = runner._ensure_source("SEC EDGAR", "https://www.sec.gov", 98, "regulator")
    for i in ("amend", "orig", "f4", "c"):
        it_db.run_command(f"CREATE (:Entity {{id:'{i}', name:'{i}', type:'company'}})")
    it_db.run_command("CREATE (:Person {id:'p', full_name:'P'})")
    edges = (("amend", "13G/A", "2026-02-10", "2026-02-10", None),   # amendment date as a stated start
             ("orig", "13G", "2005-03-01", "2005-03-01", None),      # an original: stays stated
             ("f4", "Form 4", "2026-05-01", "2026-05-01", None))     # a trade date as a start
    for a, ft, since, sdate, until in edges:
        it_db.run_command("MATCH (x {id:$a}),(y {id:'c'}) CREATE (x)-[:OWNS {source_id:$s, "
                          "filing_type:$f, since:$since, source_date:$d}]->(y)",
                          {"a": a, "s": sec, "f": ft, "since": since, "d": sdate})
        record_claim(kind=KIND_OWNS, from_id=a, to_id="c", source_id=sec, filing_type=ft,
                     since=since, source_date=sdate, stake_percent=6.0)
    # a seat closed by an older departure: ended before it began
    it_db.run_command("MATCH (p {id:'p'}),(y {id:'c'}) CREATE (p)-[:HAS_ROLE {role:'CEO', "
                      "since:'2024-02-01', until:'2022-05-01', source_id:$s}]->(y)", {"s": sec})
    record_claim(kind=KIND_ROLE, from_id="p", to_id="c", source_id=sec, role="CEO",
                 until="2022-05-01")

    assert heal_sec_dates(dry_run=True) == {"amendment": 1, "form4": 1, "ended_before_start": 1, "pairs": 3}
    assert it_db.run_command("MATCH ()-[r:OWNS]->() WHERE r.since_basis IS NOT NULL RETURN r") == []

    heal_sec_dates()
    got = {r["a"]: (r.get("since"), r.get("basis")) for r in it_db.run_command(
        "MATCH (x)-[r:OWNS]->() RETURN x.id AS a, r.since AS since, r.since_basis AS basis")}
    assert got == {"amend": ("2026-02-10", "amendment"), "orig": ("2005-03-01", None),
                   "f4": (None, None)}
    assert it_db.run_command("MATCH ()-[r:HAS_ROLE]->() RETURN r.until AS u")[0].get("u") is None
    claims = {r["from_id"]: (r.get("since"), r.get("since_basis")) for r in it_db.run_sql(
        "SELECT from_id, since, since_basis FROM Claim WHERE kind = 'owns'")}
    assert claims == {"amend": ("2026-02-10", "amendment"), "orig": ("2005-03-01", None),
                      "f4": (None, None)}
    # idempotent
    assert heal_sec_dates() == {"amendment": 0, "form4": 0, "ended_before_start": 0, "pairs": 3}
