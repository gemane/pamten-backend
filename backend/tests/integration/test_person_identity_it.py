"""What a person's profile says about who this is (findings item 124, step 7
and the "how sure" step): the entries the person was made of, each with how
sure the merge was, and the other persons of the name and birth month —
against a real ArcadeDB, through the merge and the profile route's function."""
import pytest

from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

MONTHS = ["1961-03", "1958-07", "1974-11", "1980-01"]


def _person(it_db, pid, name="David Smith", birth=None, **extra):
    props = "".join(f", {k}: '{v}'" for k, v in {**({"birth_date": birth} if birth else {}), **extra}.items())
    it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: '{name}'{props}}})")


def _at(it_db, company, *pids):
    it_db.run_command(f"CREATE (:Entity {{id: '{company}', name: '{company} ltd', type: 'company'}})")
    for pid in pids:
        it_db.run_command(f"MATCH (p:Person {{id: '{pid}'}}), (e:Entity {{id: '{company}'}}) CREATE (p)-[:OWNS {{}}]->(e)")


def _identity(pid):
    from app.routers.search import get_person_profile
    return get_person_profile(pid)["identity"]


def test_a_person_nobody_was_merged_into_says_so(it_db):
    _person(it_db, "p-1", "Ada Lovelace")
    assert _identity("p-1") == {"merged": {"count": 0, "tier": None, "entries": []}, "namesakes": None}


def test_the_entries_a_person_was_made_of_each_say_how_sure(it_db):
    """Three merges into one person: the scan's on a rare name and a birth
    month, the scan's on the month beside a shared company, and one by hand."""
    from app.routers.persons import deduplicate_high_confidence, merge_person_records
    _person(it_db, "chpsc:a", "Tadashi Yanai", "1949-02")
    _person(it_db, "chpsc:b", "Tadashi Yanai", "1949-02")
    assert deduplicate_high_confidence(apply=True)["merged_count"] == 1
    [kept] = [r["id"] for r in it_db.run_command("MATCH (p:Person) RETURN p.id AS id")]
    _person(it_db, "p-hand", "T. Yanai")
    merge_person_records(kept, "p-hand")                               # manual
    identity = _identity(kept)
    assert identity["merged"]["count"] == 2 and identity["merged"]["tier"] == "likely"
    assert {(e["name"], e["basis"], e["tier"]) for e in identity["merged"]["entries"]} == {
        ("Tadashi Yanai", "birth_month_rare_name", "likely"), ("T. Yanai", "manual", "confirmed")}
    assert all(set(e) == {"name", "at", "basis", "tier"} for e in identity["merged"]["entries"])   # no ids, no reason text


def test_a_merge_beside_a_shared_company_is_strong_and_the_person_as_sure_as_its_weakest(it_db):
    from app.routers.persons import deduplicate_high_confidence
    for i, m in enumerate(MONTHS[1:]):
        _person(it_db, f"chpsc:o{i}", birth=m)
    _person(it_db, "chpsc:a", birth=MONTHS[0])
    _person(it_db, "chpsc:b", birth=MONTHS[0])
    _at(it_db, "acme", "chpsc:a", "chpsc:b")
    deduplicate_high_confidence(apply=True)
    kept = next(r["id"] for r in it_db.run_command("MATCH (p:Person)-[:OWNS]->(:Entity {id: 'acme'}) RETURN p.id AS id"))
    merged = _identity(kept)["merged"]
    assert (merged["count"], merged["tier"]) == (1, "strong")
    assert merged["entries"][0]["basis"] == "birth_month_and_company"


def test_entries_follow_the_person_through_a_later_merge_and_a_new_id(it_db):
    """A was merged into B, then B into the Wikidata person, who took the SEC
    twin's filer number and its id with it: the page of the survivor lists
    all of them, under whatever id each merge was logged."""
    from app.routers.persons import merge_person_records
    _person(it_db, "p-a", "Lachlan K Murdoch")
    _person(it_db, "p-b", "Lachlan Keith Murdoch")
    _person(it_db, "wd:Q1", "Lachlan Murdoch", wikidata_id="Q1")
    _person(it_db, "sec:0001234567", "Murdoch Lachlan K", sec_cik="0001234567")
    merge_person_records("p-b", "p-a")
    merge_person_records("wd:Q1", "p-b")
    survivor = merge_person_records("wd:Q1", "sec:0001234567")
    assert survivor == "sec:0001234567"                                   # re-keyed
    merged = _identity(survivor)["merged"]
    assert sorted(e["name"] for e in merged["entries"]) == ["Lachlan K Murdoch", "Lachlan Keith Murdoch",
                                                            "Murdoch Lachlan K"]
    assert merged["tier"] == "confirmed"                                   # all by hand


def test_an_undone_merge_is_not_listed(it_db):
    from app.routers.persons import merge_log, merge_person_records, undo_merge
    _person(it_db, "p-a", "Jane Mary Smith")
    _person(it_db, "p-b", "Jane M Smith")
    merge_person_records("p-a", "p-b")
    assert _identity("p-a")["merged"]["count"] == 1
    undo_merge(merge_log(limit=5, _={"role": "contributor"})["entries"][0]["snapshot_id"], _={"role": "contributor"})
    assert _identity("p-a")["merged"] == {"count": 0, "tier": None, "entries": []}


def test_the_others_of_the_name_and_birth_month_are_listed(it_db):
    """Two more David Smiths of March 1961 the dedupe left apart (a common
    name, nothing links them), one of another month, one without a date."""
    for i, m in enumerate(MONTHS):
        _person(it_db, f"chpsc:ds{i}", birth=m)
    _person(it_db, "chpsc:twin", "Smith David", MONTHS[0])           # another word order, the same name
    _person(it_db, "wd:third", "David Smith", "1961-03-04")           # a day in the month
    _person(it_db, "sec:undated", "David Smith")
    _at(it_db, "acme", "chpsc:twin")
    names = _identity("chpsc:ds0")["namesakes"]
    assert names["count"] == 2
    assert [(e["id"], e["full_name"], e["birth_date"], e["companies"]) for e in names["entries"]] == [
        ("wd:third", "David Smith", "1961-03-04", []), ("chpsc:twin", "Smith David", "1961-03", ["acme ltd"])]
    assert all(set(e) == {"id", "full_name", "birth_date", "companies"} for e in names["entries"])


def test_a_suppressed_namesake_is_not_listed_and_a_person_without_a_month_has_no_list(it_db):
    _person(it_db, "p-a", birth="1961-03")
    _person(it_db, "p-hidden", birth="1961-03")
    _person(it_db, "p-year", birth="1961")
    assert _identity("p-a")["namesakes"]["count"] == 1
    run_sql("INSERT INTO Suppression SET id = 's1', target_kind = 'person', node_id = 'p-hidden', "
            "from_id = '', to_id = '', role = ''")
    assert _identity("p-a")["namesakes"] == {"count": 0, "entries": []}
    assert _identity("p-year")["namesakes"] is None


def test_the_list_is_cut_and_the_count_is_not(it_db):
    from app import person_identity
    for i in range(person_identity.MAX_NAMESAKES + 4):
        _person(it_db, f"chpsc:n{i:02d}", birth="1961-03")
    names = _identity("chpsc:n00")["namesakes"]
    assert names["count"] == person_identity.MAX_NAMESAKES + 3 and len(names["entries"]) == person_identity.MAX_NAMESAKES
