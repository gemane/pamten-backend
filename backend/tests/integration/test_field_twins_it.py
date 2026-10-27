"""Real-ArcadeDB: the twin heals and the writers' fold (app.field_twins).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_the_writers_fold_the_scalar_into_the_list(it_db):
    from app.scraper.runner import _upsert_person

    pid = _upsert_person("Larry Page", nationality="US", description="", wikidata_id="Q1")
    row = it_db.run_sql("SELECT nationalities, nationality FROM Person WHERE id = :id", {"id": pid})[0]
    assert row["nationalities"] == ["US"] and row.get("nationality") is None
    # a later source with a list adds, a blank-fill never blanks
    _upsert_person("Larry Page", nationality=None, description="", wikidata_id="Q1", nationalities=["CA"])
    assert it_db.run_sql("SELECT nationalities FROM Person WHERE id = :id", {"id": pid})[0]["nationalities"] == ["US"]


def test_the_writers_leave_an_unknown_list_absent(it_db):
    """What no source stated is absent, not `[]` — on the Wikidata person and
    entity writers, the name-keyed person writer and the PSC person. (The
    Wikidata entity writer states no country at all.)"""
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

    eid = _upsert_entity("Stateless Ltd", None, None, "", "Q8", source_id="wikidata")
    assert "countries" not in it_db.run_sql("SELECT FROM Entity WHERE id = :id", {"id": eid})[0]


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
    assert _upsert_person_by_name("Jane Form", source_id="sec-edgar", sec_cik="0000001") == nid   # the name match (a bare CIK, padded on write)
    assert stamp(nid) > OLD
    assert it_db.run_sql("SELECT sec_cik FROM Person WHERE id = :id", {"id": nid})[0]["sec_cik"] == "0000000001"


def test_a_knowledge_base_role_claim_is_dated_by_the_scrape_day(it_db):
    from app.scraper.runner import _upsert_entity, _upsert_person, _upsert_role
    pid = _upsert_person("Some Body", nationality=None, description="", wikidata_id="Q77")
    eid = _upsert_entity("Some Co", None, None, "", "Q78", source_id="wikidata")
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
