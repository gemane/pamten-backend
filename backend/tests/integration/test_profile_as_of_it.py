"""The full profile "as of" a date, against a real ArcadeDB — the time-travel
view's data. Which edges count as in force on a date, at every boundary, and
that the section counts and the row cap describe the same set.

A stated start after the date hides; a `first_listed` start is a lower bound
and never hides (the client dims it); an `until` on or before the date hides;
no dates at all never hides. The present (no date) is today's behaviour: open
edges only.
"""
import pytest

from app.routers.search import get_full_profile

pytestmark = pytest.mark.integration


def _company(it_db, eid, name=None):
    it_db.run_command("CREATE (:Entity {id: $id, name: $n, type: 'company'})",
                      {"id": eid, "n": name or eid})


def _owns(it_db, parent, child, **props):
    sets = ", ".join(f"{k}: ${k}" for k in props)
    it_db.run_command(
        f"MATCH (a:Entity {{id: $p}}), (b:Entity {{id: $c}}) "
        f"CREATE (a)-[:OWNS {{source_id: 's'{', ' + sets if sets else ''}}}]->(b)",
        {"p": parent, "c": child, **props})


def _seed(it_db):
    _company(it_db, "nc", "News Corp")
    for sid in ("stated", "bound", "ended", "undated", "partial", "holder"):
        _company(it_db, sid)
    _owns(it_db, "nc", "stated", since="2015-06-01", source_date="2015-06-01", stake_percent=50.0)
    _owns(it_db, "nc", "bound", since="2014-08-14", since_basis="first_listed",
          source_date="2026-08-07", stake_percent=40.0)
    _owns(it_db, "nc", "ended", since="2010-01-01", until="2018-03-31", until_reason="withdrawn",
          source_date="2010-01-01", stake_percent=90.0)
    _owns(it_db, "nc", "undated", stake_percent=30.0)
    _owns(it_db, "nc", "partial", since="2019-04-00", stake_percent=20.0)
    # an owner that sold out, and two seats: one ending, one starting, on the same day
    _owns(it_db, "holder", "nc", since="2012-01-01", until="2016-12-31", stake_percent=10.0)
    for pid, since, until in (("p-left", "2005-01-01", "2011-08-24"), ("p-new", "2011-08-24", None)):
        it_db.run_command("CREATE (:Person {id: $id, full_name: $id})", {"id": pid})
        it_db.run_command(
            "MATCH (p:Person {id: $pid}), (e:Entity {id: 'nc'}) "
            "CREATE (p)-[:HAS_ROLE {role: 'CEO', since: $since, until: $until, source_id: 's'}]->(e)",
            {"pid": pid, "since": since, "until": until})


def _subs(profile):
    return {s["entity"]["id"] for s in profile["subsidiaries"]}


def test_the_present_is_unchanged(it_db):
    _seed(it_db)
    p = get_full_profile("nc")
    assert _subs(p) == {"stated", "bound", "undated", "partial"}
    assert p["owners"] == []                                   # sold out in 2016
    assert {e["person"]["id"] for e in p["executives"]} == {"p-new"}
    assert p["counts"]["subsidiaries"] == 4 and p["counts"]["owners"] == 0


def test_as_of_2012_a_stated_later_start_hides_a_lower_bound_does_not(it_db):
    _seed(it_db)
    p = get_full_profile("nc", as_of="2012-12-31")
    assert _subs(p) == {"bound", "ended", "undated"}
    assert {o["owner"]["id"] for o in p["owners"]} == {"holder"}   # held 2012–2016
    assert p["counts"]["subsidiaries"] == 3 and p["counts"]["owners"] == 1


def test_as_of_2019_the_ended_holding_is_gone_and_the_rest_are_back(it_db):
    _seed(it_db)
    p = get_full_profile("nc", as_of="2019-12-31")
    assert _subs(p) == {"stated", "bound", "undated", "partial"}
    assert p["owners"] == []
    assert p["counts"]["subsidiaries"] == len(p["subsidiaries"])


@pytest.mark.parametrize("as_of, expected", [
    ("2018-03-31", False),   # until == as_of: ended by that day
    ("2018-03-30", True),
])
def test_an_until_on_the_date_means_ended(it_db, as_of, expected):
    _seed(it_db)
    assert ("ended" in _subs(get_full_profile("nc", as_of=as_of))) is expected


@pytest.mark.parametrize("as_of, expected", [
    ("2015-06-01", True),    # since == as_of: started by that day
    ("2015-05-31", False),
    ("2019-03-31", False),   # a partial "2019-04-00" start sorts where April belongs
    ("2019-04-30", True),
])
def test_a_since_on_the_date_means_started(it_db, as_of, expected):
    _seed(it_db)
    subs = _subs(get_full_profile("nc", as_of=as_of))
    key = "partial" if as_of.startswith("2019") else "stated"
    assert (key in subs) is expected


def test_counts_match_the_lists_for_every_date(it_db):
    _seed(it_db)
    for as_of in (None, "2009-12-31", "2012-12-31", "2016-12-31", "2019-12-31"):
        p = get_full_profile("nc", as_of=as_of)
        for section in ("subsidiaries", "owners", "executives"):
            assert p["counts"][section] == len(p[section]), (as_of, section)


def test_the_cap_applies_after_the_date_filter(it_db):
    # The 90 % holding ended in 2018 must not take one of the two slots in 2019.
    _seed(it_db)
    p = get_full_profile("nc", limit=2, as_of="2019-12-31")
    assert _subs(p) == {"stated", "bound"}
    assert p["counts"]["subsidiaries"] == 4


def test_roles_follow_the_same_rule(it_db):
    _seed(it_db)
    on_the_day = {e["person"]["id"] for e in get_full_profile("nc", as_of="2011-08-24")["executives"]}
    assert on_the_day == {"p-new"}             # the seat ending that day is over, the new one started
    before = {e["person"]["id"] for e in get_full_profile("nc", as_of="2011-08-23")["executives"]}
    assert before == {"p-left"}
