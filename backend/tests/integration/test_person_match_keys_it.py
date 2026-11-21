"""The identity-verification day as evidence that two entries of one common
name and birth month are one person (findings item 124, step 5) — and the
officer-id bridge of step 4, which needs no code of its own."""
import pytest

from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

MONTHS = ["1961-03", "1958-07", "1974-11", "1980-01"]           # four birth months: a common name


def _person(it_db, pid, birth=None, keys=None, name="David Smith"):
    birth_sql = f", birth_date: '{birth}'" if birth else ""
    it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: '{name}'{birth_sql}}})")
    if keys:
        run_sql("UPDATE Person SET match_keys = :k WHERE id = :id", {"k": keys, "id": pid})


def _smiths(it_db, keys_a, keys_b):
    for i, m in enumerate(MONTHS[1:]):
        _person(it_db, f"chpsc:o{i}", m)
    _person(it_db, "chpsc:a", MONTHS[0], keys_a)
    _person(it_db, "chpsc:b", MONTHS[0], keys_b)


def _run():
    from app.routers.persons import deduplicate_high_confidence
    return deduplicate_high_confidence(apply=True)


def _alive(it_db):
    return sorted(r["id"] for r in it_db.run_command("MATCH (p:Person) RETURN p.id AS id"))


def _keys(pid):
    rows = run_sql("SELECT match_keys FROM Person WHERE id = :id", {"id": pid})
    return rows[0].get("match_keys") if rows else None


def test_the_same_verification_day_links_two_entries_of_a_common_name(it_db):
    _smiths(it_db, ["v:2026-01-05"], ["v:2026-01-05"])
    res = _run()
    assert res["merged_count"] == 1 and len(_alive(it_db)) == 4
    reason = it_db.run_command("MATCH (ml:MergeLog) RETURN ml.reason AS r")[0]["r"]
    assert "same birth month" in reason and "the same identity-verification day" in reason


def test_a_different_day_is_no_evidence_and_no_veto(it_db):
    """One person is verified again when an agent files for them (4 % of the
    groups of a name only one person bears): a different day decides nothing —
    here the two also share a company, and that still merges them."""
    _smiths(it_db, ["v:2026-01-05"], ["v:2026-07-19"])
    assert _run()["merged_count"] == 0
    it_db.run_command("CREATE (:Entity {id: 'acme', name: 'Acme', type: 'company'})")
    for pid in ("chpsc:a", "chpsc:b"):
        it_db.run_command(f"MATCH (p:Person {{id: '{pid}'}}), (e:Entity {{id: 'acme'}}) CREATE (p)-[:OWNS {{}}]->(e)")
    assert _run()["merged_count"] == 1


def test_only_one_of_them_verified_is_no_evidence(it_db):
    _smiths(it_db, ["v:2026-01-05"], None)
    assert _run()["merged_count"] == 0 and len(_alive(it_db)) == 5


def test_the_same_day_under_another_birth_month_links_nobody(it_db):
    """Thousands are verified on any one day: the day counts beside the name
    and the birth month, never in their place."""
    for i, m in enumerate(MONTHS):
        _person(it_db, f"chpsc:o{i}", m, ["v:2026-01-05"] if i < 2 else None)
    assert _run()["merged_count"] == 0 and len(_alive(it_db)) == 4


def test_the_merged_person_carries_the_keys_of_both_entries(it_db):
    _smiths(it_db, ["v:2026-01-05"], ["v:2026-01-05", "v:2026-07-19"])
    _run()
    [kept] = [p for p in _alive(it_db) if p in ("chpsc:a", "chpsc:b")]
    assert _keys(kept) == ["v:2026-01-05", "v:2026-07-19"]


def test_a_merge_of_two_persons_without_keys_adds_no_empty_list(it_db):
    from app.routers.persons import merge_person_records
    _person(it_db, "p-a", name="Jane Mary Smith")
    _person(it_db, "p-b", name="Jane Mary Smith")
    merge_person_records("p-a", "p-b")
    assert _keys("p-a") is None


def test_a_register_owner_and_the_two_companies_officer_are_one_person(it_db):
    """Step 4, the officer-id bridge: the register writes an owner per company
    statement, but Companies House links one OFFICER across companies by its
    own id. David Smith owns X and Y, as two entries; the director David Smith
    of the same birth month sits at both under one officer id — each entry
    shares a company with him, and the chain makes the three one."""
    for i, m in enumerate(MONTHS[1:]):
        _person(it_db, f"chpsc:o{i}", m)
    _person(it_db, "chpsc:x", MONTHS[0])
    _person(it_db, "chpsc:y", MONTHS[0])
    _person(it_db, "choff:abc", MONTHS[0])
    for eid in ("x", "y"):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{eid} ltd', type: 'company'}})")
        it_db.run_command(f"MATCH (p:Person {{id: 'chpsc:{eid}'}}), (e:Entity {{id: '{eid}'}}) CREATE (p)-[:OWNS {{}}]->(e)")
        it_db.run_command(f"MATCH (p:Person {{id: 'choff:abc'}}), (e:Entity {{id: '{eid}'}}) "
                          "CREATE (p)-[:HAS_ROLE {role: 'Director'}]->(e)")
    res = _run()
    assert res["merged_count"] == 2
    assert len([p for p in _alive(it_db) if p in ("chpsc:x", "chpsc:y", "choff:abc")]) == 1
    # without the officer the two owner entries are two namesakes
