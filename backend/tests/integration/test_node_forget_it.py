"""Deleting a person or a company removes every row that names it by id
(findings item 129), against a real ArcadeDB: `DETACH DELETE` takes the node
and its edges, and the claims, stake history, industry codes, forwarding rows,
merge log, flags and pins are rows no edge holds. A suppression stays — it is
what keeps the record hidden when the next import writes it again."""
import pytest

from app import media, node_forget
from app.claims import record_claim
from app.db.arcadedb import run_sql
from app.routers.entities import delete_entity
from app.routers.persons import delete_person, merge_person_records

pytestmark = pytest.mark.integration

_USER = {"role": "contributor"}

#: every type that names a node by id, with the columns that do — written out
#: here, not read from the module, so a column the cleanup forgets shows
_NAMES = (("Claim", ("from_id", "to_id")), ("StakeHistory", ("from_id", "to_id")), ("IndustryCode", ("entity_id",)),
          ("Flag", ("node_id", "from_id", "to_id")), ("Pin", ("from_id", "to_id")), ("MergeLog", ("keep_id", "dup_id")),
          ("MergedId", ("old_id", "new_id")), ("MergeSnapshot", ("keep_id", "dup_id", "survivor_id")),
          ("ScrapeRun", ("entity_id",)), ("Suppression", ("node_id", "from_id", "to_id")))


def _naming(node_id: str) -> dict:
    """How many rows of each type still name this id."""
    out = {}
    for table, cols in _NAMES:
        where = " OR ".join(f"{c} = :id" for c in cols)
        out[table] = run_sql(f"SELECT count(*) AS c FROM {table} WHERE {where}", {"id": node_id})[0]["c"]
    return out


def _row(it_db, table: str, **props):
    body = ", ".join(f"{k} = '{v}'" for k, v in props.items())
    run_sql(f"INSERT INTO {table} SET {body}")


@pytest.fixture
def forgotten_images(monkeypatch):
    """The object store is the outside world here: record what would be removed."""
    calls = []
    monkeypatch.setattr(media, "forget", lambda kind, node_id: calls.append((kind, node_id)))
    return calls


def _graph(it_db):
    """Two persons and two companies, each named by every kind of row; the
    bystanders (p-other, co-other) share rows with nobody who is deleted."""
    for eid in ("co-a", "co-other"):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{eid}', type: 'company'}})")
    for pid, name in (("p-gone", "Jane Smith"), ("p-twin", "Jane M Smith"), ("p-other", "Somebody Else")):
        it_db.run_command(f"CREATE (:Person {{id: '{pid}', full_name: '{name}'}})")
    for pid, eid in (("p-gone", "co-a"), ("p-twin", "co-a"), ("p-other", "co-a"), ("p-other", "co-other")):
        it_db.run_command(f"MATCH (p:Person {{id: '{pid}'}}), (e:Entity {{id: '{eid}'}}) "
                          "CREATE (p)-[:HAS_ROLE {role: 'Director', source_id: 'wikidata'}]->(e)")
        record_claim(kind="role", from_id=pid, to_id=eid, source_id="wikidata", role="Director")
    it_db.run_command("MATCH (p:Person {id: 'p-gone'}), (e:Entity {id: 'co-a'}) "
                      "CREATE (p)-[:OWNS {stake_percent: 30.0, source_id: 'sec-edgar'}]->(e)")
    record_claim(kind="owns", from_id="p-gone", to_id="co-a", source_id="sec-edgar", stake_percent=30.0)
    for owner in ("p-gone", "p-other"):
        _row(it_db, "StakeHistory", from_id=owner, to_id="co-a", source_id="sec-edgar", as_of="2025-01-01")
    for eid in ("co-a", "co-other"):
        _row(it_db, "IndustryCode", key=f"{eid}|sic|6770", entity_id=eid, scheme="sic", code="6770")
        _row(it_db, "ScrapeRun", id=f"run-{eid}", source="sec_edgar", target=eid, status="ok", entity_id=eid)
    for n, (kind, node, frm, to) in enumerate((("person", "p-gone", "", ""), ("role", "", "p-gone", "co-a"),
                                               ("person", "p-other", "", ""), ("entity", "co-a", "", ""),
                                               ("role", "", "p-other", "co-other"))):
        _row(it_db, "Flag", id=f"f{n}", target_kind=kind, node_id=node, from_id=frm, to_id=to,
             status="open", note="lives at 1 Example Road")
    _row(it_db, "Pin", id="pin1", target_kind="owns", from_id="p-gone", to_id="co-a")
    _row(it_db, "Pin", id="pin2", target_kind="owns", from_id="p-other", to_id="co-other")
    _row(it_db, "Suppression", id="s1", target_kind="person", node_id="p-gone", from_id="", to_id="")
    _row(it_db, "Suppression", id="s2", target_kind="role", node_id="", from_id="p-gone", to_id="co-a", role="Director")
    _row(it_db, "Suppression", id="s3", target_kind="entity", node_id="co-a", from_id="", to_id="")


def test_a_deleted_person_leaves_no_row_that_names_them(it_db, forgotten_images):
    _graph(it_db)
    merge_person_records("p-gone", "p-twin")      # a merge log row with both names, a forwarding row, a stored copy
    before = _naming("p-gone")
    assert all(before[t] for t in ("Claim", "StakeHistory", "Flag", "Pin", "MergeLog", "MergedId", "MergeSnapshot"))
    other = _naming("p-other")

    report = delete_person("p-gone", _=_USER)

    after = _naming("p-gone")
    assert {t: n for t, n in after.items() if n} == {"Suppression": 2}     # kept: it keeps the record hidden
    assert _naming("p-twin") == {t: 0 for t in after}                      # the id it had absorbed, too
    assert not run_sql("SELECT FROM MergeLog WHERE keep_name = 'Jane Smith' OR dup_name = 'Jane M Smith'")
    assert not run_sql("SELECT FROM Person WHERE id = 'p-gone'")
    assert report["removed"]["Claim"] == before["Claim"] and report["removed"]["MergedId"] == 1
    assert report["removed"]["MergeLog"] == 1 and report["removed"]["MergeSnapshot"] == 1
    assert sorted(forgotten_images) == [("person", "p-gone"), ("person", "p-twin")]
    # nobody else lost anything
    assert _naming("p-other") == other
    assert run_sql("SELECT count(*) AS c FROM Claim WHERE to_id = 'co-a'")[0]["c"] == 1


def test_a_deleted_company_leaves_no_row_that_names_it(it_db, forgotten_images):
    _graph(it_db)
    before, other = _naming("co-a"), _naming("co-other")
    assert all(before[t] for t in ("Claim", "StakeHistory", "IndustryCode", "Flag", "Pin", "ScrapeRun"))

    report = delete_entity("co-a", _=_USER)

    after = _naming("co-a")
    assert {t: n for t, n in after.items() if n} == {"Suppression": 2}
    assert report["removed"]["IndustryCode"] == 1 and report["removed"]["ScrapeRun"] == 1
    assert run_sql("SELECT count(*) AS c FROM ScrapeRun WHERE id = 'run-co-a'")[0]["c"] == 1   # the log line stays
    assert forgotten_images == [("entity", "co-a")]
    assert _naming("co-other") == other
    # the people are still there, with the claims about their other companies
    assert run_sql("SELECT count(*) AS c FROM Claim WHERE from_id = 'p-other'")[0]["c"] == 1


def test_forgetting_twice_finds_nothing(it_db, forgotten_images):
    _graph(it_db)
    delete_person("p-gone", _=_USER)
    again = node_forget.forget_node("Person", "p-gone")
    assert not any(again.values())


def test_a_store_outage_does_not_stop_the_rows_going(it_db, monkeypatch):
    _graph(it_db)

    def down(kind, node_id):
        raise RuntimeError("bucket unreachable")
    monkeypatch.setattr(media, "forget", down)
    delete_person("p-gone", _=_USER)
    assert _naming("p-gone")["Claim"] == 0


@pytest.mark.parametrize("gone", ["p-twin", "p-other"])
def test_either_person_of_an_undone_merge_takes_its_log_row(it_db, forgotten_images, gone):
    """After an undo both exist again and no forwarding row joins them: the log
    row of the undone merge names the one as `keep`, the other as `dup`, and
    still carries both names — it goes with whichever of the two is deleted."""
    from app.routers.persons import merge_log, undo_merge
    _graph(it_db)
    merge_person_records("p-other", "p-twin")
    undo_merge(merge_log(limit=5, _=_USER)["entries"][0]["snapshot_id"], _=_USER)
    assert run_sql("SELECT FROM Person WHERE id = 'p-twin'") and _naming(gone)["MergeLog"] == 1

    delete_person(gone, _=_USER)

    assert _naming(gone)["MergeLog"] == 0
    assert not run_sql("SELECT FROM MergeLog WHERE dup_name = 'Jane M Smith'")
