"""
Real-ArcadeDB test that the post-scrape auto-dedup is scoped to the persons a
scrape touched — not a full-DB person scan (which is O(all persons) and crawls once
the graph grows to millions of nodes).

Covers: the touched-persons collector records upserted ids; a scoped scan groups the
touched persons + existing same-name candidates and ignores unrelated persons; an
empty touched set does no work.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _person(it_db, pid, full_name):
    it_db.run_command(
        "CREATE (p:Person {id:$id, full_name:$n, aliases: [], wikidata_id:null})",
        {"id": pid, "n": full_name})


def test_autodedup_scoped_to_touched(it_db):
    from app.routers.persons import deduplicate_high_confidence, scan_duplicate_groups
    from app.scraper.graph_writer import _touched_persons
    from app.scraper.runner import _upsert_person_by_name

    # A scrape touches two persons the sources spelled differently (same name token
    # set) — the collector records them while it's active.
    token = _touched_persons.set(set())
    a = _upsert_person_by_name("William H Gates III", "src")
    b = _upsert_person_by_name("Gates William H Iii", "src")   # same name-key as a
    touched = set(_touched_persons.get())
    _touched_persons.reset(token)
    assert {a, b} <= touched

    # An EXISTING person (not touched) with the same exact name → must be pulled in
    # as a candidate. An unrelated person → must never be considered.
    _person(it_db, "existing-gates", "William H Gates III")
    _person(it_db, "unrelated", "Totally Unrelated Person")

    groups = scan_duplicate_groups(seed_ids=list(touched))
    grouped = {m["id"] for g in groups for m in g["members"]}
    assert {a, b, "existing-gates"} <= grouped     # seeds + existing same-name candidate
    assert "unrelated" not in grouped              # unrelated person excluded from the scope

    # Nothing touched → no work, no groups.
    assert scan_duplicate_groups(seed_ids=[]) == []
    assert deduplicate_high_confidence(apply=False, seed_ids=[])["review_count"] == 0

    # The full scan (no seed) still sees everyone — the same duplicate group surfaces.
    full = scan_duplicate_groups()
    assert any({a, b, "existing-gates"} <= {m["id"] for m in g["members"]} for g in full)


def test_a_full_scan_reads_the_companies_in_one_query(it_db, monkeypatch):
    """The review list waited 17 s on 1,075 round trips: a full scan asked for
    each person's companies with a query of its own (2026-11-22). It reads them
    all at once now — and finds what the per-person scan found: two namesakes
    of one company are a group for their shared company, a third namesake with
    no company at all stands apart from that reason."""
    from app.database import _Session as ArcadeSession
    from app.routers.persons import scan_duplicate_groups
    for pid, name in (("p-a", "Jordan Quillfeather"), ("p-b", "Jordan Quillfeather"),
                      ("p-c", "Jordan Quillfeather"), ("p-other", "Somebody Else Entirely")):
        _person(it_db, pid, name)
    it_db.run_command("CREATE (e:Entity {id:'e-co', name:'Quill Co'})")
    for pid in ("p-a", "p-b", "p-other"):
        it_db.run_command("MATCH (p:Person {id:$p}), (e:Entity {id:'e-co'}) CREATE (p)-[:HAS_ROLE {role:'Director'}]->(e)",
                          {"p": pid})

    asked: list[str] = []
    run = ArcadeSession.run

    def counting(self, query, *args, **kwargs):
        asked.append(query)
        return run(self, query, *args, **kwargs)
    monkeypatch.setattr(ArcadeSession, "run", counting)

    full = scan_duplicate_groups()
    per_person = [q for q in asked if "(x:Person {id:$id})-[]-(e:Entity)" in q]
    assert per_person == []
    assert sum("MATCH (x:Person)-[]-(e:Entity)" in q for q in asked) == 1

    scoped = scan_duplicate_groups(seed_ids=["p-a", "p-b", "p-c"])
    key = lambda groups: sorted((sorted(m["id"] for m in g["members"]), g["confidence"], g["reason"]) for g in groups)  # noqa: E731
    mine = [g for g in full if {m["id"] for m in g["members"]} & {"p-a", "p-b", "p-c"}]
    assert mine and key(mine) == key(scoped)        # the same groups, the same reasons, either way
    assert any({m["id"] for m in g["members"]} == {"p-a", "p-b"} and "company" in g["reason"] for g in mine)
