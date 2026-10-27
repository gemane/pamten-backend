"""Real-ArcadeDB: a seat ends no later than its holder's death, a departure
is cited beside the appointment, and a re-read keeps the end
(app.roles, app.claims, 2026-10-25).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seats(it_db, pid):
    return {r["role"]: r for r in it_db.run_sql(
        "SELECT role, since, until, until_reason, until_source_url FROM HAS_ROLE WHERE @out.id = :p", {"p": pid})}


def test_a_death_date_closes_the_open_seats_but_not_the_founders(it_db):
    from app.scraper.runner import _upsert_entity, _upsert_person
    from app.scraper.graph_writer import _upsert_role

    pid = _upsert_person("Paul Allen", None, "", "Q27", source_id="wikidata")
    ms = _upsert_entity("Microsoft", None, None, "", "Q2283", source_id="wikidata")
    _upsert_role(pid, ms, "Founder", "wikidata", source_url="w")
    _upsert_role(pid, ms, "Board Member", "wikidata", since="1990-01-01", source_url="w")
    _upsert_role(pid, ms, "Chairman", "wikidata", since="2019-06-01", source_url="w")     # dated after the death
    assert all(s["until"] is None for s in _seats(it_db, pid).values())

    _upsert_person("Paul Allen", None, "", "Q27", death_date="2018-10-15", source_id="wikidata")   # the death arrives
    seats = _seats(it_db, pid)
    assert seats["Board Member"]["until"] == "2018-10-15"
    assert seats["Board Member"]["until_reason"] == "death"
    assert seats["Board Member"]["until_source_url"] == "https://www.wikidata.org/wiki/Q27"
    assert seats["Founder"]["until"] is None and seats["Chairman"]["until"] is None
    # the source's claim is left as stated: it named no end
    claim = it_db.run_sql("SELECT until FROM Claim WHERE kind = 'role' AND from_id = :p AND role = 'Board Member'",
                          {"p": pid})[0]
    assert claim["until"] is None


def test_a_seat_opened_for_someone_already_dead_ends_at_once(it_db):
    from app.scraper.runner import _upsert_person
    from app.scraper.sec_writer import _upsert_role_sec

    pid = _upsert_person("Sidney Harman", None, "", "Q1585", death_date="2011-04-12", source_id="wikidata")
    it_db.run_command("CREATE (:Entity {id:'harman', name:'Harman', name_normalized:'harman'})")
    _upsert_role_sec(pid, "harman", "Director", "sec-edgar", source_url="u4", source_date="2010-02-01",
                     filing_type="Form 4", read_from="field")
    seat = _seats(it_db, pid)["Director"]
    assert (seat["until"], seat["until_reason"]) == ("2011-04-12", "death")


def test_the_heal_closes_what_was_written_before_the_rule(it_db):
    from app.roles import heal_seats_at_death

    it_db.run_command("CREATE (:Person {id:'park', full_name:'Byung-Gook Park', death_date:'2022-05-17', wikidata_id:'Q5'})")
    it_db.run_command("CREATE (:Person {id:'jobs', full_name:'Steve Jobs', death_date:'2011-10-05'})")
    it_db.run_command("CREATE (:Person {id:'alive', full_name:'Alive One'})")
    it_db.run_command("CREATE (:Entity {id:'sx', name:'Samsung', name_normalized:'samsung'})")
    for pid, role, since in (("park", "Board Member", None), ("park", "Advisor", "2023-01-01"),
                             ("jobs", "Founder", "1976-04-01"), ("alive", "CEO", None)):
        it_db.run_command("MATCH (p:Person {id:$p}), (e:Entity {id:'sx'}) "
                          "CREATE (p)-[:HAS_ROLE {role:$r, since:$s, source_id:'wikidata'}]->(e)",
                          {"p": pid, "r": role, "s": since})

    dry = heal_seats_at_death(apply=False)
    assert (dry["closed"], dry["founders_kept"], dry["after_death"]) == (1, 1, 1)
    assert dry["people"] == ["Byung-Gook Park († 2022-05-17): Board Member @ Samsung, Advisor @ Samsung"]
    assert it_db.run_sql("SELECT count(*) AS n FROM HAS_ROLE WHERE until IS NOT NULL")[0]["n"] == 0

    res = heal_seats_at_death(apply=True)
    assert (res["closed"], res["founders_kept"], res["after_death"]) == (1, 1, 1)
    park = _seats(it_db, "park")
    assert park["Board Member"]["until"] == "2022-05-17" and park["Board Member"]["until_reason"] == "death"
    assert park["Advisor"]["until"] is None                      # dated after the death: the source's error, reported
    assert _seats(it_db, "jobs")["Founder"]["until"] is None
    assert _seats(it_db, "alive")["CEO"]["until"] is None
    assert heal_seats_at_death(apply=True)["closed"] == 0        # idempotent


def test_a_re_read_keeps_the_end_and_a_new_spell_replaces_it(it_db):
    from app.claims import record_claim
    from app.scraper.sec_writer import _close_role_sec, _upsert_role_sec

    it_db.run_command("CREATE (:Person {id:'tc', full_name:'Tim Cook'})")
    it_db.run_command("CREATE (:Entity {id:'ap', name:'Apple', name_normalized:'apple'})")
    _upsert_role_sec("tc", "ap", "CEO", "sec-edgar", source_url="u4", source_date="2026-07-01",
                     filing_type="Form 4", read_from="field")
    _close_role_sec("tc", "ap", "2026-09-01", role="CEO", source_id="sec-edgar", source_url="u8k",
                    source_date="2026-09-02", filing_type="8-K", read_from="narrative")
    q = "SELECT until, until_source_url, until_read_from, source_url, filing_type FROM Claim WHERE kind = 'role' AND from_id = 'tc'"
    (claim,) = it_db.run_sql(q)
    assert (claim["until"], claim["until_source_url"], claim["until_read_from"]) == ("2026-09-01", "u8k", "narrative")
    assert (claim["source_url"], claim["filing_type"]) == ("u4", "Form 4")

    # an older Form 4 re-read: no end, no start — the departure stands
    _upsert_role_sec("tc", "ap", "CEO", "sec-edgar", source_url="u4", source_date="2026-07-01",
                     filing_type="Form 4", read_from="field")
    (claim,) = it_db.run_sql(q)
    assert claim["until"] == "2026-09-01" and claim["until_source_url"] == "u8k"

    # a new spell — a different start — replaces the end
    record_claim(kind="role", from_id="tc", to_id="ap", source_id="sec-edgar", role="CEO", since="2027-01-10",
                 source_url="u4b", credibility_score=98)
    (claim,) = it_db.run_sql(q)
    assert claim["until"] is None and claim["until_source_url"] is None


def test_wikidata_cites_its_own_item_for_an_end_it_states(it_db):
    from app.scraper.graph_writer import _upsert_role

    it_db.run_command("CREATE (:Person {id:'sj', full_name:'Steve Jobs'})")
    it_db.run_command("CREATE (:Entity {id:'ap', name:'Apple', name_normalized:'apple'})")
    _upsert_role("sj", "ap", "CEO", "wikidata", since="1997-09-16", source_url="https://www.wikidata.org/wiki/Q312")
    _upsert_role("sj", "ap", "CEO", "wikidata", since="1997-09-16", until="2011-08-24",
                 source_url="https://www.wikidata.org/wiki/Q312")
    seat = _seats(it_db, "sj")["CEO"]
    assert (seat["until"], seat["until_source_url"]) == ("2011-08-24", "https://www.wikidata.org/wiki/Q312")
