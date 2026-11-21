"""What two companies say about two persons of one common name and birth
month (findings item 124, step 3): a shared owner or officer, an ownership
between the companies, one registered office that is no agent's — against a
real ArcadeDB, through the scan."""
from unittest.mock import patch

import pytest

from app import person_evidence

pytestmark = pytest.mark.integration

MONTHS = ["1961-03", "1958-07", "1974-11", "1980-01"]           # four birth months: a common name


def _smiths(it_db, *extra):
    """A David Smith per birth month, a second one born in the first — each of
    the two March ones at a company of his own."""
    rows = [(f"chpsc:ds{i}", m) for i, m in enumerate(MONTHS)] + [("chpsc:twin", MONTHS[0]), *extra]
    for pid, birth in rows:
        birth_sql = f", birth_date: '{birth}'" if birth else ""
        it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: 'David Smith'{birth_sql}}})")
    _company(it_db, "x", "chpsc:ds0")
    _company(it_db, "y", "chpsc:twin")


def _company(it_db, eid, *owners, office=None, postcode=None):
    extra = (f", address_normalized: '{office}'" if office else "") + (f", reg_postcode: '{postcode}'" if postcode else "")
    it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{eid} ltd', type: 'company'{extra}}})")
    for pid in owners:
        _owns(it_db, pid, eid)


def _owns(it_db, owner, eid, label="Person"):
    it_db.run_command(f"MATCH (o:{label} {{id: '{owner}'}}), (e:Entity {{id: '{eid}'}}) CREATE (o)-[:OWNS {{}}]->(e)")


def _office(it_db, eid, office, postcode):
    it_db.run_command(f"MATCH (e:Entity {{id: '{eid}'}}) SET e.address_normalized = '{office}', e.reg_postcode = '{postcode}'")


def _run():
    from app.routers.persons import deduplicate_high_confidence
    return deduplicate_high_confidence(apply=True)


def _alive(it_db):
    return sorted(r["id"] for r in it_db.run_command("MATCH (p:Person) RETURN p.id AS id"))


def _reason(it_db):
    return it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS r")[0]["r"]


def test_two_companies_and_nothing_between_them_merge_nobody(it_db):
    _smiths(it_db)
    assert _run()["merged_count"] == 0 and len(_alive(it_db)) == 5


def test_a_shared_co_owner_links_the_two(it_db):
    """Jane Smith owns both companies: the two David Smiths of March 1961 are
    her one co-owner."""
    _smiths(it_db)
    it_db.run_command("CREATE (:Person {id: 'chpsc:jane', full_name: 'Jane Smith', birth_date: '1963-05'})")
    _owns(it_db, "chpsc:jane", "x")
    _owns(it_db, "chpsc:jane", "y")
    res = _run()
    assert res["merged_count"] == 1 and len(_alive(it_db)) == 5       # 4 Davids + Jane
    assert person_evidence.CO_OWNER in _reason(it_db) and "same birth month" in _reason(it_db)


def test_the_co_owner_may_be_two_entries_of_one_name_and_month(it_db):
    """The register writes the co-owner once per company too: two Jane Smiths
    of May 1963, not yet one node."""
    _smiths(it_db)
    for pid, eid in (("chpsc:jane1", "x"), ("chpsc:jane2", "y")):
        it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: 'Jane Smith', birth_date: '1963-05'}})")
        _owns(it_db, pid, eid)
    _run()
    davids = [p for p in _alive(it_db) if p.startswith("chpsc:ds") or p == "chpsc:twin"]
    assert len(davids) == 4                               # five before: the two of March 1961 are one
    reasons = [r["r"] for r in it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS r")]
    assert any(person_evidence.CO_OWNER in r for r in reasons)


def test_a_co_owner_of_another_birth_month_is_somebody_else(it_db):
    _smiths(it_db)
    for pid, eid, birth in (("chpsc:jane1", "x", "1963-05"), ("chpsc:jane2", "y", "1970-09")):
        it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: 'Jane Smith', birth_date: '{birth}'}})")
        _owns(it_db, pid, eid)
    assert _run()["merged_count"] == 0


def test_an_institution_at_both_companies_is_no_sign_of_one_person(it_db):
    """A shared owner tied to more companies than `CO_OWNER_MAX_COMPANIES` — a
    fund manager, a group's holding company — is at everybody's company."""
    _smiths(it_db)
    it_db.run_command("CREATE (:Entity {id: 'fund', name: 'Big Fund Managers', type: 'company'})")
    _owns(it_db, "fund", "x", "Entity")
    _owns(it_db, "fund", "y", "Entity")
    _company(it_db, "z")
    _owns(it_db, "fund", "z", "Entity")
    with patch.object(person_evidence, "CO_OWNER_MAX_COMPANIES", 2):
        assert _run()["merged_count"] == 0
    assert _run()["merged_count"] == 1                    # three companies are within the real limit


def test_one_company_owning_the_other_links_the_two(it_db):
    _smiths(it_db)
    _owns(it_db, "x", "y", "Entity")
    assert _run()["merged_count"] == 1 and person_evidence.OWNS_OTHER in _reason(it_db)


def test_one_registered_office_links_the_two(it_db):
    _smiths(it_db)
    _office(it_db, "x", "12 mill lane otley ls21 1aa gb", "LS21 1AA")
    _office(it_db, "y", "12 mill lane otley ls21 1aa gb", "LS21 1AA")
    assert _run()["merged_count"] == 1 and person_evidence.SAME_OFFICE in _reason(it_db)


def test_an_agents_office_links_nobody(it_db):
    """71-75 Shelton Street, WC2H 9JQ: the registered office of tens of
    thousands of companies, on the shipped list."""
    assert "WC2H9JQ" in person_evidence.mass_office_postcodes()
    _smiths(it_db)
    _office(it_db, "x", "71 75 shelton street london wc2h 9jq gb", "WC2H 9JQ")
    _office(it_db, "y", "71 75 shelton street london wc2h 9jq gb", "WC2H 9JQ")
    assert _run()["merged_count"] == 0


def test_an_office_the_graph_holds_many_companies_at_links_nobody(it_db):
    """Not on the UK list (another country, a new agent): counted in the graph."""
    _smiths(it_db)
    for eid in ("x", "y", "m1", "m2"):
        if eid.startswith("m"):
            _company(it_db, eid)
        _office(it_db, eid, "1 harbour road douglas im1 1aa im", "IM1 1AA")
    with patch.object(person_evidence, "MASS_OFFICE_ENTITIES", 3):
        assert _run()["merged_count"] == 0
    assert _run()["merged_count"] == 1                    # four companies are within the real limit


def test_two_different_offices_link_nobody(it_db):
    _smiths(it_db)
    _office(it_db, "x", "12 mill lane otley ls21 1aa gb", "LS21 1AA")
    _office(it_db, "y", "14 mill lane otley ls21 1aa gb", "LS21 1AA")
    assert _run()["merged_count"] == 0


def test_the_companies_say_nothing_about_two_bearers_without_a_birth_date(it_db):
    """Evidence beside the month, never in its place: two undated David Smiths
    whose companies share an owner and an office stay two."""
    for pid in ("sec:1", "p-2"):
        it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: 'David Smith'}})")
    for i, m in enumerate(MONTHS):
        it_db.run_command(f"CREATE (:Person {{id: 'chpsc:o{i}', full_name: 'David Smith', birth_date: '{m}'}})")
    _company(it_db, "x", "sec:1", office="12 mill lane otley ls21 1aa gb", postcode="LS21 1AA")
    _company(it_db, "y", "p-2", office="12 mill lane otley ls21 1aa gb", postcode="LS21 1AA")
    _owns(it_db, "x", "y", "Entity")
    assert _run()["merged_count"] == 0 and len(_alive(it_db)) == 6


def test_a_third_of_the_month_without_such_a_tie_stays_apart(it_db):
    _smiths(it_db, ("chpsc:third", MONTHS[0]))
    _company(it_db, "w", "chpsc:third")
    _owns(it_db, "x", "y", "Entity")
    res = _run()
    assert res["merged_count"] == 1 and "chpsc:third" in _alive(it_db)


def test_under_a_rare_name_the_companies_are_not_asked(it_db):
    """Three birth months: the month merges, and no company is read for it."""
    for i, m in enumerate(MONTHS[:3]):
        it_db.run_command(f"CREATE (:Person {{id: 'chpsc:ds{i}', full_name: 'David Smith', birth_date: '{m}'}})")
    it_db.run_command(f"CREATE (:Person {{id: 'chpsc:twin', full_name: 'David Smith', birth_date: '{MONTHS[0]}'}})")
    with patch.object(person_evidence.CompanySide, "link", side_effect=AssertionError("asked")):
        assert _run()["merged_count"] == 1


def test_a_company_with_too_many_ties_is_not_read_for_whom_it_shares(it_db):
    _smiths(it_db)
    it_db.run_command("CREATE (:Person {id: 'chpsc:jane', full_name: 'Jane Smith', birth_date: '1963-05'})")
    _owns(it_db, "chpsc:jane", "x")
    _owns(it_db, "chpsc:jane", "y")
    with patch.object(person_evidence, "COMPANY_MAX_TIES", 1):          # each company has two ties
        assert _run()["merged_count"] == 0
    assert _run()["merged_count"] == 1


def test_a_person_tied_to_too_many_companies_is_not_read(it_db):
    """A nominee at hundreds of companies shares an office or an owner with
    anybody."""
    _smiths(it_db)
    _owns(it_db, "x", "y", "Entity")
    _company(it_db, "x2", "chpsc:ds0")
    with patch.object(person_evidence, "PERSON_MAX_COMPANIES", 1):      # ds0 is tied to two
        assert _run()["merged_count"] == 0
    assert _run()["merged_count"] == 1


def test_an_undated_bearer_is_not_pulled_in_by_what_the_companies_say(it_db):
    """Two spellings of one David Smith at one company, and an undated third
    whose company the first one owns: the ownership is evidence beside a birth
    month, and the third has none."""
    for i, m in enumerate(MONTHS[1:]):
        it_db.run_command(f"CREATE (:Person {{id: 'chpsc:o{i}', full_name: 'David Smith', birth_date: '{m}'}})")
    it_db.run_command("CREATE (:Person {id: 'chpsc:a', full_name: 'David Smith', birth_date: '1961-03'})")
    it_db.run_command("CREATE (:Person {id: 'chpsc:b', full_name: 'Smith David', birth_date: '1961-03'})")
    it_db.run_command("CREATE (:Person {id: 'sec:c', full_name: 'Mr David Smith'})")
    _company(it_db, "x", "chpsc:a", "chpsc:b")
    _company(it_db, "y", "sec:c")
    _owns(it_db, "x", "y", "Entity")
    res = _run()
    assert res["merged_count"] == 1 and "sec:c" in _alive(it_db)
