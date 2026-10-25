"""Real-ArcadeDB: the twin heals and the writers' fold (app.field_twins).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_the_heals_bring_an_older_database_to_one_field(it_db):
    from app.field_twins import heal_all

    # the three person shapes the 2026-10-25 survey found, and an entity twin
    it_db.run_command("CREATE (:Person {id:'chpsc:01:a', full_name:'A', nationality:'GB', nationalities:[]})")
    it_db.run_command("CREATE (:Person {id:'p2', full_name:'B', nationality:'US', nationalities:['US','CA'], source_id:'wikidata'})")
    it_db.run_command("CREATE (:Person {id:'p3', full_name:'C', nationalities:['DE'], source_id:'sec-edgar'})")
    it_db.run_command("CREATE (:Entity {id:'e1', name:'E1', name_normalized:'e1', country:'NL', countries:[]})")
    it_db.run_command("CREATE (:Entity {id:'e2', name:'E2', name_normalized:'e2', country:'US', countries:['US']})")
    # an empty list where no source named a country: absent after the heal
    it_db.run_command("CREATE (:Person {id:'p4', full_name:'D', nationalities:[], source_id:'sec-edgar'})")
    it_db.run_command("CREATE (:Entity {id:'e3', name:'E3', name_normalized:'e3', countries:[]})")
    it_db.run_command("MATCH (a:Entity {id:'e1'}), (b:Entity {id:'e2'}) CREATE (a)-[:OWNS {until:null}]->(b)")
    it_db.run_command("MATCH (a:Entity {id:'e2'}), (b:Entity {id:'e1'}) CREATE (a)-[:OWNS {until:null, stale:true}]->(b)")
    # `OWNS.stale` defaults to false since #430, so a fresh edge is never unset;
    # the older database this heal exists for is made by clearing the flag after
    # the write (an UPDATE to null leaves null — the default applies to new rows)
    it_db.run_command("MATCH (a:Entity {id:'e1'})-[r:OWNS]->(b:Entity {id:'e2'}) SET r.stale = null")
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS WHERE stale IS NULL")[0]["n"] == 1
    # the other flags the same way (2026-10-25): a voting group written before
    # the default had no nominee flag, every person no on-demand flag
    it_db.run_command("MATCH (e:Entity {id:'e3'}) SET e.is_nominee = null")
    it_db.run_command("MATCH (p:Person {id:'p3'}) SET p.on_demand_scraped = null")
    # an empty description beside a real one
    it_db.run_command("CREATE (:Person {id:'p5', full_name:'E', description:'', source_id:'sec-edgar'})")
    it_db.run_command("CREATE (:Person {id:'p6', full_name:'F', description:'A banker', source_id:'wikidata'})")

    dry = heal_all(apply=False)
    # the dry run counts every empty list as it stands — the two the fold will
    # fill and the two nothing names; the apply below runs the fold first
    assert dry["empty_lists"] == {"applied": False, "Person.nationalities": 2, "Entity.countries": 2}
    assert dry["nationalities"]["dropped"] == 2 and dry["nationalities"]["folded"] == 1
    assert dry["countries"]["fixed"] == 1 and dry["person_sources"]["stamped"] == 1
    assert dry["flags"]["fixed"] == 3 and dry["flags"]["OWNS.stale"] == 1
    assert dry["flags"]["Entity.is_nominee"] == 1 and dry["flags"]["Person.on_demand_scraped"] == 1
    assert dry["empty_strings"] == {"applied": False, "Person.description": 1}
    assert it_db.run_sql("SELECT nationality FROM Person WHERE id = 'chpsc:01:a'")[0]["nationality"] == "GB"   # untouched

    res = heal_all(apply=True)
    assert res["nationalities"]["dropped"] == 2 and res["countries"]["fixed"] == 1
    assert res["person_sources"]["stamped"] == 1 and res["flags"]["fixed"] == 3
    assert it_db.run_sql("SELECT is_nominee FROM Entity WHERE id = 'e3'")[0]["is_nominee"] is False
    assert it_db.run_sql("SELECT on_demand_scraped FROM Person WHERE id = 'p3'")[0]["on_demand_scraped"] is False
    assert res["empty_strings"]["Person.description"] == 1
    assert "description" not in it_db.run_sql("SELECT FROM Person WHERE id = 'p5'")[0]
    assert it_db.run_sql("SELECT description FROM Person WHERE id = 'p6'")[0]["description"] == "A banker"
    rows = {r["id"]: r for r in it_db.run_sql("SELECT id, nationalities, source_id FROM Person")}
    assert rows["chpsc:01:a"]["nationalities"] == ["GB"] and rows["chpsc:01:a"]["source_id"] == "uk-psc"
    assert rows["p2"]["nationalities"] == ["US", "CA"] and rows["p3"]["nationalities"] == ["DE"]
    assert it_db.run_sql("SELECT count(*) AS n FROM Person WHERE nationality IS NOT NULL")[0]["n"] == 0
    assert it_db.run_sql("SELECT countries FROM Entity WHERE id = 'e1'")[0]["countries"] == ["NL"]
    assert res["empty_lists"]["Person.nationalities"] == 1 and res["empty_lists"]["Entity.countries"] == 1
    assert "nationalities" not in it_db.run_sql("SELECT FROM Person WHERE id = 'p4'")[0]
    assert "countries" not in it_db.run_sql("SELECT FROM Entity WHERE id = 'e3'")[0]
    assert it_db.run_sql("SELECT FROM Entity WHERE id = 'e2'")[0]["countries"] == ["US"]      # a filled list stays
    stale = it_db.run_sql("SELECT stale, count(*) AS n FROM OWNS GROUP BY stale")
    assert sorted((r["stale"], r["n"]) for r in stale) == [(False, 1), (True, 1)]
    # idempotent
    again = heal_all(apply=True)
    assert again["nationalities"]["dropped"] == 0 and again["countries"]["fixed"] == 0
    assert again["person_sources"]["stamped"] == 0 and again["flags"]["fixed"] == 0
    assert again["empty_lists"]["Person.nationalities"] == 0 and again["empty_strings"]["Person.description"] == 0


def test_the_writers_fold_the_scalar_into_the_list(it_db):
    from app.scraper.runner import _upsert_person, _upsert_entity

    pid = _upsert_person("Larry Page", nationality="US", description="", wikidata_id="Q1")
    row = it_db.run_sql("SELECT nationalities, nationality FROM Person WHERE id = :id", {"id": pid})[0]
    assert row["nationalities"] == ["US"] and row.get("nationality") is None
    # a later source with a list adds, a blank-fill never blanks
    _upsert_person("Larry Page", nationality=None, description="", wikidata_id="Q1", nationalities=["CA"])
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["US"]

    eid = _upsert_entity("Heineken N.V.", "company", "NL", None, None, "", "Q2",
                         countries=[], source_id="wikidata")
    assert it_db.run_sql("SELECT countries FROM Entity WHERE id = :id", {"id": eid})[0]["countries"] == ["NL"]


def test_the_writers_leave_an_unknown_list_absent(it_db):
    """What no source stated is absent, not `[]` — on the Wikidata person and
    entity writers, the name-keyed person writer and the PSC person."""
    from app.scraper.graph_writer import _upsert_person_by_name
    from app.scraper.runner import _upsert_entity, _upsert_person

    pid = _upsert_person("Nobody Known", nationality=None, description="", wikidata_id="Q9")
    row = it_db.run_sql("SELECT FROM Person WHERE id = :id", {"id": pid})[0]
    assert "nationalities" not in row and row.get("description") is None       # '' is absent too
    _upsert_person("Nobody Known", nationality="FR", description="", wikidata_id="Q9")      # learned later: filled
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["FR"]
    _upsert_person("Nobody Known", nationality=None, description="", wikidata_id="Q9")      # a blank never blanks
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["FR"]

    nid = _upsert_person_by_name("Jane Form", source_id="sec-edgar")
    row = it_db.run_sql("SELECT FROM Person WHERE id = :id", {"id": nid})[0]
    assert "nationalities" not in row and row.get("description") is None

    eid = _upsert_entity("Stateless Ltd", "company", None, None, None, "", "Q8", countries=[], source_id="wikidata")
    assert "countries" not in it_db.run_sql("SELECT FROM Entity WHERE id = :id", {"id": eid})[0]
    _upsert_entity("Stateless Ltd", "company", "IE", None, None, "", "Q8", source_id="wikidata")
    assert it_db.run_sql("SELECT countries FROM Entity WHERE id = :id", {"id": eid})[0]["countries"] == ["IE"]


def test_the_dead_keys_leave_and_the_undated_role_claims_get_their_first_sighting(it_db):
    from app.field_twins import heal_claim_dates, heal_dead_keys
    it_db.run_command("CREATE (:Person {id:'p1', full_name:'A', updated_at:'2026-01-01T00:00:00+00:00'})")
    it_db.run_command("CREATE (:Person {id:'p2', full_name:'B'})")
    it_db.run_command("CREATE (:Entity {id:'e1', name:'E1'})")
    it_db.run_command("CREATE (:Entity {id:'e2', name:'E2'})")
    it_db.run_command("MATCH (a:Entity {id:'e1'}), (b:Entity {id:'e2'}) "
                      "CREATE (a)-[:OWNS {until:null, file_date:'2026-01-02', source_date:'2026-01-02'}]->(b)")
    it_db.run_command("MATCH (a:Person {id:'p1'}), (b:Person {id:'p2'}) "
                      "CREATE (a)-[:RELATED_TO {relation:'affiliate', credibility_score:98}]->(b)")
    it_db.run_sql("INSERT INTO Claim SET claim_key='k1', kind='role', from_id='p1', to_id='e1', source_id='wikidata', "
                  "first_seen_at='2026-10-06T14:01:02+00:00', last_seen_at='2026-10-25T00:00:00+00:00'")
    it_db.run_sql("INSERT INTO Claim SET claim_key='k2', kind='role', from_id='p2', to_id='e1', source_id='wikidata', "
                  "source_date='2026-09-01', first_seen_at='2026-10-06T14:01:02+00:00', last_seen_at='2026-10-25T00:00:00+00:00'")

    assert heal_dead_keys(apply=False) == {"applied": False, "OWNS.file_date": 1, "Person.updated_at": 1,
                                           "RELATED_TO.credibility_score": 1}
    assert heal_dead_keys(apply=True)["OWNS.file_date"] == 1
    assert "file_date" not in it_db.run_sql("SELECT FROM OWNS")[0]
    assert it_db.run_sql("SELECT source_date FROM OWNS")[0]["source_date"] == "2026-01-02"
    assert "updated_at" not in it_db.run_sql("SELECT FROM Person WHERE id = 'p1'")[0]
    assert "credibility_score" not in it_db.run_sql("SELECT FROM RELATED_TO")[0]

    assert heal_claim_dates(apply=True) == {"applied": True, "claims": 1}
    dates = {r["claim_key"]: r["source_date"] for r in it_db.run_sql("SELECT claim_key, source_date FROM Claim")}
    assert dates == {"k1": "2026-10-06", "k2": "2026-09-01"}
    assert heal_claim_dates(apply=True)["claims"] == 0


def test_a_person_is_stamped_whenever_a_source_writes_them(it_db):
    """`last_scraped_at` on a person used to be set by the person scrape
    only — no company-scraped person had one (954 on dev, 2026-10-25)."""
    from app.scraper.graph_writer import _upsert_person_by_name
    from app.scraper.runner import _upsert_person
    def stamp(pid):
        return it_db.run_sql("SELECT last_scraped_at AS t FROM Person WHERE id = :id", {"id": pid})[0]["t"]
    OLD = "2020-01-01T00:00:00+00:00"
    pid = _upsert_person("Some Body", nationality=None, description="", wikidata_id="Q77")
    assert (stamp(pid) or "") >= "2026-01-01"
    it_db.run_sql("UPDATE Person SET last_scraped_at = :t WHERE id = :id", {"t": OLD, "id": pid})
    _upsert_person("Some Body", nationality=None, description="", wikidata_id="Q77")     # the backfill path
    assert stamp(pid) > OLD
    nid = _upsert_person_by_name("Jane Form", source_id="sec-edgar")
    assert (stamp(nid) or "") >= "2026-01-01"
    it_db.run_sql("UPDATE Person SET last_scraped_at = :t WHERE id = :id", {"t": OLD, "id": nid})
    assert _upsert_person_by_name("Jane Form", source_id="sec-edgar", sec_cik="0000001") == nid   # the name match
    assert stamp(nid) > OLD
    assert it_db.run_sql("SELECT sec_cik FROM Person WHERE id = :id", {"id": nid})[0]["sec_cik"] == "0000001"


def test_a_knowledge_base_role_claim_is_dated_by_the_scrape_day(it_db):
    from app.scraper.runner import _upsert_entity, _upsert_person, _upsert_role
    pid = _upsert_person("Some Body", nationality=None, description="", wikidata_id="Q77")
    eid = _upsert_entity("Some Co", "company", None, None, None, "", "Q78", source_id="wikidata")
    _upsert_role(pid, eid, "CEO", "wikidata", since="2020-01-01")
    rows = it_db.run_sql("SELECT source_date FROM Claim WHERE kind = 'role' AND from_id = :p", {"p": pid})
    assert rows and rows[0]["source_date"] and rows[0]["source_date"] >= "2026-01-01"


def test_the_person_merge_and_the_person_api_leave_an_unknown_list_absent(it_db):
    """The sibling paths: the merge wrote `nationalities: []` onto the kept
    person when neither side had one (16 on dev after the 2026-10-25 scrape),
    the API's create and update wrote the model's default `[]`."""
    from app.models.person import PersonCreate
    from app.routers.persons import create_person, merge_person_records, update_person
    it_db.run_command("CREATE (:Person {id:'keep', full_name:'Larry Page', description:''})")
    it_db.run_command("CREATE (:Person {id:'dup', full_name:'Page Lawrence', description:''})")
    merge_person_records("keep", "dup")
    kept = it_db.run_sql("SELECT FROM Person WHERE id = 'keep'")[0]
    assert "nationalities" not in kept
    assert kept.get("description") is None          # '' from either side is absent, never copied

    made = create_person(PersonCreate(first_name="Ada", last_name="Lovelace", description=""), _={"role": "contributor"})
    pid = made["id"] if isinstance(made, dict) else made.id
    row = it_db.run_sql("SELECT FROM Person WHERE id = :id", {"id": pid})[0]
    assert "nationalities" not in row and row.get("description") is None
    update_person(pid, PersonCreate(first_name="Ada", last_name="Lovelace", nationalities=["GB"]), _={"role": "contributor"})
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["GB"]


def test_the_address_key_heal_rekeys_an_older_database(it_db):
    from app.field_twins import heal_address_keys
    it_db.run_command("CREATE (:Entity {id:'lei:1', name:'Acme', address:'1 Example Street, London, EC1A 1BB, GB', "
                      "registered_address:'1 example street london ec1a 1bb gb'})")
    it_db.run_command("CREATE (:Entity {id:'gb-coh:1', name:'Harrogate Co', "
                      "registered_address:'1 HIGH STREET, HARROGATE, HG1 1AA'})")
    it_db.run_command("CREATE (:Entity {id:'chpsc:1', name:'Manor Ltd', address:'The Manor, Cheltenham, GL51 0TJ', "
                      "registered_address:'The Manor, Cheltenham, GL51 0TJ'})")
    it_db.run_command("CREATE (:Entity {id:'wd:1', name:'No Address Inc'})")
    res = heal_address_keys(apply=False)
    assert (res["scanned"], res["keyed"], res["addresses_moved"], res["removed"]) == (4, 3, 1, 3)
    assert "registered_address" in it_db.run_sql("SELECT FROM Entity WHERE id = 'lei:1'")[0], "a dry run wrote"
    assert heal_address_keys(apply=True)["keyed"] == 3
    rows = {r["id"]: r for r in it_db.run_sql("SELECT id, address, address_normalized, registered_address FROM Entity")}
    assert rows["lei:1"]["address_normalized"] == "1 example street london ec1a 1bb gb"
    assert rows["gb-coh:1"]["address"] == "1 HIGH STREET, HARROGATE, HG1 1AA"
    assert rows["gb-coh:1"]["address_normalized"] == "1 high street harrogate hg1 1aa"
    assert rows["chpsc:1"]["address_normalized"] == "the manor cheltenham gl51 0tj"
    assert all(r.get("registered_address") is None for r in rows.values())
    assert "address_normalized" not in it_db.run_sql("SELECT FROM Entity WHERE id = 'wd:1'")[0]
    assert heal_address_keys(apply=True)["keyed"] == 0
