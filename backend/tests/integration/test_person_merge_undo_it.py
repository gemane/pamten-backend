"""A person merge can be undone (findings item 123): the merge stores three
pictures — both persons before, the survivor after — and the split puts the
two back from them, against a real ArcadeDB. Nothing is mocked: the merge, its
re-keying and the claims are the app's own."""
import pytest

from app import merge_undo
from app.claims import record_claim
from app.routers.persons import deduplicate_high_confidence, merge_log, merge_person_records, undo_merge

pytestmark = pytest.mark.integration

_USER = {"role": "contributor"}


def _state(pid: str) -> dict | None:
    """One person as a merge can change it — without record ids, and without
    the keep-separate mark a split adds."""
    pic = merge_undo.picture(pid)
    if pic is None:
        return None
    drop = ("claim_key", "last_seen_at", "first_seen_at")
    return {
        "node": pic["node"],
        "edges": sorted((e["type"], e["out"], e["other"], sorted(e["props"].items()))
                        for e in pic["edges"] if e["type"] != "NOT_DUPLICATE"),
        "claims": sorted(sorted((k, str(v)) for k, v in c.items() if k not in drop) for c in pic["claims"]),
        "claim_keys": sorted(c["claim_key"] for c in pic["claims"]),
    }


def _seat(it_db, pid, eid, role="Director", **props):
    body = ", ".join([f"role: '{role}'", *(f"{k}: '{v}'" for k, v in props.items())])
    it_db.run_command(f"MATCH (p:Person {{id: '{pid}'}}), (e:Entity {{id: '{eid}'}}) CREATE (p)-[:HAS_ROLE {{{body}}}]->(e)")
    record_claim(kind="role", from_id=pid, to_id=eid, source_id=props.get("source_id", "wikidata"),
                 role=role, since=props.get("since"))


def _two_people(it_db):
    """A Wikidata person and a register officer of the same name: different
    companies, one company both sit at, and fields only the officer carries."""
    for eid in ("co-a", "co-b", "co-both"):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{eid}', type: 'company'}})")
    it_db.run_command("CREATE (:Person {id: 'p-keep', full_name: 'Jane Smith', wikidata_id: 'Q1', "
                      "source_id: 'wikidata', aliases: ['J. Smith']})")
    it_db.run_command("CREATE (:Person {id: 'choff:dup', full_name: 'Jane Mary Smith', birth_date: '1979-10', "
                      "nationalities: ['GB'], source_id: 'ch-officers'})")
    _seat(it_db, "p-keep", "co-a", "CEO", since="2015-01-01")
    _seat(it_db, "p-keep", "co-both", since="2020-05-01")
    _seat(it_db, "choff:dup", "co-b", "Company Secretary", since="2019-01-02", source_id="ch-officers")
    _seat(it_db, "choff:dup", "co-both", since="2018-03-01", source_id="ch-officers")
    it_db.run_command("MATCH (p:Person {id: 'choff:dup'}), (e:Entity {id: 'co-b'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 12.5, source_id: 'uk-psc'}]->(e)")
    record_claim(kind="owns", from_id="choff:dup", to_id="co-b", source_id="uk-psc", stake_percent=12.5)


def _snapshot_id():
    return merge_log(limit=10, _=_USER)["entries"][0]["snapshot_id"]


def test_a_split_gives_both_persons_back_as_they_were(it_db):
    _two_people(it_db)
    keep, dup = _state("p-keep"), _state("choff:dup")
    merge_person_records("p-keep", "choff:dup")
    merged = _state("p-keep")
    assert _state("choff:dup") is None and merged["node"]["birth_date"] == "1979-10"
    assert len(merged["edges"]) == 4                 # CEO, secretary, the holding, ONE seat at co-both
    assert ("HAS_ROLE", True, "co-both", [("role", "Director"), ("since", "2018-03-01")]) in merged["edges"]

    report = undo_merge(_snapshot_id(), _=_USER)

    assert _state("p-keep") == keep and _state("choff:dup") == dup
    assert (report["kept_id"], report["restored_id"]) == ("p-keep", "choff:dup")
    assert {"birth_date", "nationalities", "aliases"} <= set(report["fields_restored"])
    assert (report["edges_moved"], report["edges_recreated"], report["edges_restored"]) == (2, 1, 1)
    assert report["written_since"] == [] and report["not_restored"] == []
    # the forwarding row is gone, the pair is on record as two people
    assert it_db.run_command("MATCH (m:MergedId {old_id: 'choff:dup'}) RETURN m.new_id AS n") == []
    assert it_db.run_command("MATCH (:Person {id: 'p-keep'})-[r:NOT_DUPLICATE]-(:Person {id: 'choff:dup'}) "
                             "RETURN count(r) AS n")[0]["n"] == 1
    entry = merge_log(limit=10, _=_USER)["entries"][0]
    assert entry["undone_at"] and entry["reason"] == "manual"


def test_an_undo_empties_the_pictures_and_keeps_the_log_line(it_db):
    """Kept while the merge stands: once it is undone the copy of the two
    persons' data has no purpose left, the row (ids, names, when, why) has."""
    _two_people(it_db)
    merge_person_records("p-keep", "choff:dup")
    sid = _snapshot_id()
    stored = it_db.run_command("MATCH (s:MergeSnapshot {id: $id}) RETURN s.payload AS p", {"id": sid})[0]["p"]
    assert stored and "choff:dup" in stored

    undo_merge(sid, _=_USER)

    row = it_db.run_command("MATCH (s:MergeSnapshot {id: $id}) RETURN s.payload AS p, s.undone_at AS u, "
                            "s.keep_id AS k, s.dup_id AS d, s.reason AS r", {"id": sid})[0]
    assert row["p"] is None
    assert row["u"] and (row["k"], row["d"], row["r"]) == ("p-keep", "choff:dup", "manual")


def test_a_merge_is_undone_once(it_db):
    from fastapi import HTTPException
    _two_people(it_db)
    merge_person_records("p-keep", "choff:dup")
    sid = _snapshot_id()
    undo_merge(sid, _=_USER)
    with pytest.raises(HTTPException) as again:
        undo_merge(sid, _=_USER)
    assert again.value.status_code == 409 and "already undone" in again.value.detail
    with pytest.raises(HTTPException) as unknown:
        undo_merge("no-such-id", _=_USER)
    assert unknown.value.status_code == 404


def test_one_assertion_both_made_is_two_claims_again(it_db):
    """Both nodes carried Wikidata's Director claim at the shared company: the
    merge folds them into one row, the split writes the duplicate's again."""
    _two_people(it_db)
    record_claim(kind="role", from_id="choff:dup", to_id="co-both", source_id="wikidata", role="Director",
                 since="2018-03-01")
    keep, dup = _state("p-keep"), _state("choff:dup")
    merge_person_records("p-keep", "choff:dup")
    report = undo_merge(_snapshot_id(), _=_USER)
    assert report["claims_recreated"] == 1
    assert _state("p-keep") == keep and _state("choff:dup") == dup


def test_the_id_the_merge_gave_the_survivor_goes_back_too(it_db):
    """A Wikidata person that takes its SEC twin's CIK is re-keyed `sec:…` —
    the twin's own id. The split gives the id back to the twin."""
    it_db.run_command("CREATE (:Entity {id: 'co-a', name: 'A', type: 'company'})")
    it_db.run_command("CREATE (:Person {id: 'wd:Q912811', full_name: 'Brian Moynihan', wikidata_id: 'Q912811'})")
    it_db.run_command("CREATE (:Person {id: 'sec:0001195071', full_name: 'Moynihan Brian T', sec_cik: '0001195071'})")
    _seat(it_db, "wd:Q912811", "co-a", "CEO")
    _seat(it_db, "sec:0001195071", "co-a", "Director", source_id="sec-edgar", since="2010-01-01")
    keep, dup = _state("wd:Q912811"), _state("sec:0001195071")

    assert merge_person_records("wd:Q912811", "sec:0001195071") == "sec:0001195071"
    assert _state("wd:Q912811") is None
    report = undo_merge(_snapshot_id(), _=_USER)

    assert (report["kept_id"], report["restored_id"]) == ("wd:Q912811", "sec:0001195071")
    assert _state("wd:Q912811") == keep and _state("sec:0001195071") == dup
    assert "sec_cik" not in _state("wd:Q912811")["node"]
    assert it_db.run_command("MATCH (m:MergedId) RETURN count(m) AS n")[0]["n"] == 0


def test_what_was_written_since_the_merge_is_kept(it_db):
    _two_people(it_db)
    it_db.run_command("CREATE (:Entity {id: 'co-new', name: 'New', type: 'company'})")
    merge_person_records("p-keep", "choff:dup")
    # a later run: a new seat, an end for the seat the register twin brought,
    # and another value for a field the merge had filled
    _seat(it_db, "p-keep", "co-new", "Chair", since="2026-01-01")
    it_db.run_command("MATCH (:Person {id: 'p-keep'})-[r:HAS_ROLE]->(:Entity {id: 'co-b'}) SET r.until = '2026-09-30'")
    it_db.run_command("MATCH (p:Person {id: 'p-keep'}) SET p.birth_date = '1979-10-04'")

    report = undo_merge(_snapshot_id(), _=_USER)

    kept, restored = _state("p-keep"), _state("choff:dup")
    assert any(e[2] == "co-new" for e in kept["edges"])                       # stays with the kept person
    assert any("co-new" in line for line in report["written_since"])
    assert kept["node"]["birth_date"] == "1979-10-04"                         # not the merge's value any more
    assert "birth_date" not in report["fields_restored"]
    secretary = next(e for e in restored["edges"] if e[0] == "HAS_ROLE" and e[2] == "co-b")
    assert ("until", "2026-09-30") in secretary[3]                            # the seat as it is now
    assert not any(e[2] == "co-b" for e in kept["edges"])


def test_a_later_merge_is_undone_first(it_db):
    _two_people(it_db)
    keep, dup = _state("p-keep"), _state("choff:dup")
    it_db.run_command("CREATE (:Person {id: 'p-third', full_name: 'Jane M. Smith'})")
    merge_person_records("p-keep", "choff:dup")
    first = _snapshot_id()
    merge_person_records("p-third", "p-keep")
    second = _snapshot_id()
    assert second != first
    with pytest.raises(ValueError, match="undo that merge first"):
        merge_undo.split(first)
    merge_undo.split(second)
    merge_undo.split(first)
    assert _state("p-keep") == keep and _state("choff:dup") == dup


def test_flags_stake_rows_and_forwarded_ids_follow_the_duplicate_back(it_db):
    _two_people(it_db)
    it_db.run_command("CREATE (:Flag {id: 'f1', target_kind: 'node', node_id: 'choff:dup', status: 'open'})")
    it_db.run_command("CREATE (:Flag {id: 'f2', target_kind: 'node', node_id: 'p-keep', status: 'open'})")
    it_db.run_command("CREATE (:MergedId {id: 'm1', old_id: 'choff:older', new_id: 'choff:dup', kind: 'Person'})")
    it_db.run_command("CREATE (:StakeHistory {from_id: 'choff:dup', to_id: 'co-b', source_id: 'uk-psc', "
                      "stake_percent: 20.0, as_of: '2024-01-01', recorded_at: '2025-01-01T00:00:00+00:00'})")
    merge_person_records("p-keep", "choff:dup")
    assert it_db.run_command("MATCH (f:Flag {id: 'f1'}) RETURN f.node_id AS n")[0]["n"] == "p-keep"

    report = undo_merge(_snapshot_id(), _=_USER)

    flags = {r["id"]: r["n"] for r in it_db.run_command("MATCH (f:Flag) RETURN f.id AS id, f.node_id AS n")}
    assert flags == {"f1": "choff:dup", "f2": "p-keep"}
    assert it_db.run_command("MATCH (m:MergedId) RETURN m.old_id AS o, m.new_id AS n") == [
        {"o": "choff:older", "n": "choff:dup"}]
    assert it_db.run_command("MATCH (s:StakeHistory) RETURN s.from_id AS f")[0]["f"] == "choff:dup"
    assert (report["references_moved"], report["stakes_moved"]) == (1, 1)


def test_the_scan_does_not_merge_the_two_again(it_db):
    it_db.run_command("CREATE (:Person {id: 'choff:a', full_name: 'Mark Stephen Jones', birth_date: '1959-11'})")
    it_db.run_command("CREATE (:Person {id: 'choff:b', full_name: 'Mark Stephen Jones', birth_date: '1959-11'})")
    assert deduplicate_high_confidence(apply=True)["merged_count"] == 1
    entry = merge_log(limit=10, _=_USER)["entries"][0]
    assert entry["reason"].endswith("same birth month")
    undo_merge(entry["snapshot_id"], _=_USER)
    again = deduplicate_high_confidence(apply=True)
    assert again["merged_count"] == 0
    assert it_db.run_command("MATCH (p:Person) RETURN count(p) AS n")[0]["n"] == 2
    assert merge_undo.snapshots(open_only=True) == [] and len(merge_undo.snapshots()) == 1


def test_a_seat_deleted_since_the_merge_is_not_drawn_again(it_db):
    _two_people(it_db)
    merge_person_records("p-keep", "choff:dup")
    it_db.run_command("MATCH (:Person {id: 'p-keep'})-[r:HAS_ROLE]->(:Entity {id: 'co-b'}) DELETE r")
    report = undo_merge(_snapshot_id(), _=_USER)
    edges = _state("choff:dup")["edges"]
    assert not any(e[0] == "HAS_ROLE" and e[2] == "co-b" for e in edges)
    assert any(e[0] == "OWNS" and e[2] == "co-b" for e in edges) and report["edges_moved"] == 1


def test_a_deleted_person_is_not_kept_in_a_picture(it_db):
    """An erasure must reach the undo's store: deleting the merged person
    deletes the pictures that show it, and the log entry offers no undo."""
    from app.routers.persons import delete_person
    _two_people(it_db)
    it_db.run_command("CREATE (:Person {id: 'p-other', full_name: 'Somebody Else'})")
    it_db.run_command("CREATE (:Person {id: 'p-other2', full_name: 'Else Somebody'})")
    merge_person_records("p-other", "p-other2")
    merge_person_records("p-keep", "choff:dup")
    delete_person("p-keep", _=_USER)
    left = merge_undo.snapshots()
    assert [s["keep_id"] for s in left] == ["p-other"]
    assert "Jane Mary Smith" not in str(it_db.run_command("MATCH (s:MergeSnapshot) RETURN s.payload AS p"))
    entry = next(e for e in merge_log(limit=10, _=_USER)["entries"] if e["keep_id"] == "p-keep")
    assert entry["snapshot_id"] is None
