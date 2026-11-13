"""Companies House officers against a real ArcadeDB: the serving officers of a
UK company become persons (`choff:{id}`) and seats, a company holding a seat
and a resigned officer are left out, and a resignation the register states
later ends the seat a previous run wrote."""
from unittest.mock import patch

import pytest

from app.config import settings
from app.scraper import ch_officers as ch

pytestmark = pytest.mark.integration

CO = "gb-coh:08415302"


def _item(oid, name, role="director", appointed="2014-10-16", **over):
    item = {"name": name, "officer_role": role, "appointed_on": appointed,
            "date_of_birth": {"month": 10, "year": 1979}, "nationality": "British",
            "address": {"postal_code": "EX1 1AA"}, "country_of_residence": "England",
            "links": {"officer": {"appointments": f"/officers/{oid}/appointments"}}}
    item.update(over)
    return item


ITEMS = [
    _item("jane", "SMITH, Jane Mary"),
    _item("omar", "KHAN, Omar", role="secretary", appointed="2019-01-02"),
    _item("gone", "OLD, Peter", resigned_on="2018-06-30"),                    # left before the first run
    _item("firm", "PRISM COSEC LIMITED", role="corporate-secretary"),
]


@pytest.fixture(autouse=True)
def _key():
    with patch.object(settings, "COMPANIES_HOUSE_API_KEY", "k"):
        yield


def _seed(it_db):
    it_db.run_command(f"CREATE (:Entity {{id: '{CO}', name: 'Example Trading Limited', type: 'company', "
                      "name_normalized: 'example trading', companies_house_id: '08415302'})")
    it_db.run_command("CREATE (:Entity {id: 'lei:us', name: 'No Number Inc', type: 'company', name_normalized: 'no number'})")


def _run(items, **kw):
    with patch.object(ch, "fetch_officers", return_value=items) as fetch:
        out = ch.run_ch_officers(**kw)
    return out, [c.args[1] for c in fetch.call_args_list]


def _seats(it_db):
    return {(r["pid"], r["role"]): r for r in it_db.run_command(
        f"MATCH (p:Person)-[r:HAS_ROLE]->(e:Entity {{id: '{CO}'}}) RETURN p.id AS pid, r.role AS role, "
        "r.since AS since, r.until AS until, r.source_id AS sid, r.source_url AS url, r.read_from AS rf")}


def test_serving_officers_become_persons_and_seats(it_db):
    _seed(it_db)
    out, asked = _run(ITEMS)
    assert asked == ["08415302"]                     # only the company with a number
    assert (out["status"], out["read"], out["written"], out["resigned"], out["corporate"], out["ended"]) == \
        ("ok", 1, 2, 1, 1, 0)
    seats = _seats(it_db)
    assert set(seats) == {("choff:jane", "Director"), ("choff:omar", "Company Secretary")}
    jane = seats[("choff:jane", "Director")]
    assert (str(jane["since"])[:10], jane["until"], jane["sid"], jane["rf"]) == ("2014-10-16", None, "ch-officers", "field")
    assert jane["url"] == "https://find-and-update.company-information.service.gov.uk/company/08415302/officers"
    people = {r["id"]: r for r in it_db.run_command(
        "MATCH (p:Person) RETURN p.id AS id, p.full_name AS full, p.first_name AS first, p.last_name AS last, "
        "p.birth_date AS birth, p.nationalities AS nat, p.source_id AS sid")}
    assert set(people) == {"choff:jane", "choff:omar"}            # nobody for the resigned, nobody for the firm
    assert people["choff:jane"] == {"id": "choff:jane", "full": "Jane Mary Smith", "first": "Jane Mary",
                                    "last": "Smith", "birth": "1979-10", "nat": None, "sid": "ch-officers"}
    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS n")[0]["n"] == 2
    claims = it_db.run_command(f"MATCH (c:Claim) WHERE c.to_id = '{CO}' RETURN c.kind AS kind, c.role AS role")
    assert sorted(c["role"] for c in claims) == ["Company Secretary", "Director"]
    run = it_db.run_command("MATCH (r:ScrapeRun) RETURN r.source AS s, r.status AS st, r.total AS n")
    assert [(r["s"], r["st"], r["n"]) for r in run] == [("ch-officers", "ok", 2)]


def test_a_second_run_writes_no_second_seat(it_db):
    _seed(it_db)
    _run(ITEMS)
    _run(ITEMS)
    assert len(_seats(it_db)) == 2
    assert it_db.run_command("MATCH (p:Person) RETURN count(p) AS n")[0]["n"] == 2


def test_a_resignation_stated_later_ends_the_seat_on_its_day(it_db):
    _seed(it_db)
    _run(ITEMS)
    later = [_item("jane", "SMITH, Jane Mary", resigned_on="2026-09-30"), *ITEMS[1:]]
    out, _ = _run(later)
    seats = _seats(it_db)
    assert str(seats[("choff:jane", "Director")]["until"])[:10] == "2026-09-30"
    assert seats[("choff:omar", "Company Secretary")]["until"] is None
    assert (out["ended"], out["written"]) == (1, 1)
    # the next run finds nothing left to end
    assert _run(later)[0]["ended"] == 0


def test_a_person_another_source_already_ended_or_merged_is_followed(it_db):
    """The officer's node was merged into a Wikidata person: the seat and the
    later resignation land on the survivor, no `choff:` twin is created."""
    from app.merged_ids import record_merge_sql
    _seed(it_db)
    _run(ITEMS)
    it_db.run_command("CREATE (:Person {id: 'wd:Q1', full_name: 'Jane Smith'})")
    it_db.run_command(f"MATCH (p:Person {{id: 'choff:jane'}})-[r:HAS_ROLE]->(e:Entity {{id: '{CO}'}}), "
                      "(q:Person {id: 'wd:Q1'}) CREATE (q)-[:HAS_ROLE {role: r.role, since: r.since, "
                      "source_id: r.source_id, source_url: r.source_url}]->(e)")
    it_db.run_command("MATCH (p:Person {id: 'choff:jane'}) DETACH DELETE p")
    record_merge_sql("choff:jane", "wd:Q1", "Person")
    _run(ITEMS)
    assert it_db.run_command("MATCH (p:Person {id: 'choff:jane'}) RETURN count(p) AS n")[0]["n"] == 0
    assert ("wd:Q1", "Director") in _seats(it_db) and len(_seats(it_db)) == 2
    _run([_item("jane", "SMITH, Jane Mary", resigned_on="2026-09-30"), *ITEMS[1:]])
    assert str(_seats(it_db)[("wd:Q1", "Director")]["until"])[:10] == "2026-09-30"


def test_only_and_limit_narrow_the_run(it_db):
    _seed(it_db)
    it_db.run_command("CREATE (:Entity {id: 'gb-coh:00000002', name: 'Second Ltd', type: 'company', "
                      "name_normalized: 'second', companies_house_id: '00000002'})")
    assert _run([], only=["08415302"])[1] == ["08415302"]
    assert _run([], limit=1)[1] == ["00000002"]


def test_a_source_switched_off_in_the_scraper_panel_reads_nothing(it_db):
    """The gate unmocked: the catalogue's own Source node, switched off."""
    _seed(it_db)
    assert _run(ITEMS)[0]["status"] == "ok"                       # a new source starts switched on
    it_db.run_command("MATCH (s:Source {id: 'ch-officers'}) SET s.enabled = false")
    out, asked = _run(ITEMS)
    assert out == {"status": "disabled"} and asked == []
