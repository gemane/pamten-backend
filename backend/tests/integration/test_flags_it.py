"""
Real-ArcadeDB test for the verification flags API — exercises the create /
duplicate-collapse / summary-count / queue / patch Cypher end-to-end (the
mocked unit suite can't catch dialect or count-shape bugs).
"""
from types import SimpleNamespace

import itertools
import uuid

import pytest
from fastapi import Response

from app.routers import flags
from app.models.flag import FlagCreate, FlagStatusUpdate

pytestmark = pytest.mark.integration


def _req(ip="1.2.3.4"):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


def test_flag_create_collapse_summary_and_patch(it_db):
    flags._flag_events.clear()
    user = {"sub": "u1"}   # logged-in → higher rate ceiling for the test

    # Two distinct flags on the same node (different categories).
    r1 = flags.create_flag(
        FlagCreate(target_kind="entity", node_id="e1", category="not-real"), _req(), user=user)
    assert r1["status"] == "open"
    r2 = flags.create_flag(
        FlagCreate(target_kind="entity", node_id="e1", category="outdated"), _req(), user=user)
    assert r2["status"] == "open"

    # Same target + category + fingerprint again → collapsed, no new row.
    dup = flags.create_flag(
        FlagCreate(target_kind="entity", node_id="e1", category="not-real"), _req(), user=user)
    assert dup["status"] == "duplicate"
    assert dup["id"] == r1["id"]

    # Summary: two open flags on e1.
    assert flags.flag_summary(node_id="e1")["open"] == 2

    # Queue lists them (moderator dependency bypassed in a direct call).
    # skip/limit are passed explicitly: called directly like this, FastAPI is not
    # there to resolve their Query() defaults into ints.
    queue = flags.list_flags(Response(), status="open", target_kind=None, category=None,
                             skip=0, limit=100, _=None)
    ids = {f["id"] for f in queue}
    assert {r1["id"], r2["id"]} <= ids

    # Move one to reviewing → open count drops to 1.
    flags.update_flag_status(r1["id"], FlagStatusUpdate(status="reviewing"), _=None)
    assert flags.flag_summary(node_id="e1")["open"] == 1


def test_flag_on_an_owns_edge_addressed_by_natural_key(it_db):
    flags._flag_events.clear()
    flags.create_flag(
        FlagCreate(target_kind="owns", from_id="a", to_id="b", category="wrong-percent"),
        _req(ip="9.9.9.9"), user={"sub": "u2"})
    # Addressed by the edge's from/to natural key.
    assert flags.flag_summary(from_id="a", to_id="b")["open"] == 1
    assert flags.flag_summary(from_id="a", to_id="c")["open"] == 0



_seq = itertools.count(1)


def _flag(it_db, **props):
    """A Flag row, written straight to the database.

    Not through POST /flags: anonymous reporting is capped at two an hour, and
    these tests are about *reading* the queue. Going through the front door would
    be testing the rate limiter with extra steps.

    Every row gets its own `created_at`. The queue orders by it, so rows sharing
    a timestamp come back in whatever order the engine feels like — which looks
    exactly like a paging bug, intermittently.
    """
    fid = props.pop("id", None) or f"f-{uuid.uuid4().hex[:8]}"
    when = f"2026-08-15T10:{next(_seq):02d}:00Z"
    row = {"id": fid, "target_kind": "entity", "category": "not-real", "note": "",
           "status": "open", "reporter_kind": "anon", "from_id": "", "to_id": "",
           "role": "", "node_id": "", "created_at": when,
           "updated_at": when, **props}
    fields = ", ".join(f"{k}: '{v}'" for k, v in row.items())
    it_db.run_command(f"CREATE (:Flag {{{fields}}})")
    return fid


def test_related_to_finds_everything_reported_about_a_company(it_db):
    """The company AND its relationships.

    A report filed by right-clicking a subsidiary row belongs to the company
    whose panel it was filed from, so one id has to reach three different
    columns. That is an OR across node_id/from_id/to_id, and whether ArcadeDB's
    Cypher actually matches it is not something a mocked session can tell us.
    """
    _flag(it_db, node_id="acme")
    _flag(it_db, target_kind="owns", category="wrong-percent",
          from_id="acme", to_id="sub")            # acme owns something
    _flag(it_db, target_kind="owns", category="wrong-owner",
          from_id="parent", to_id="acme")         # something owns acme
    _flag(it_db, node_id="unrelated")

    got = flags.list_flags(Response(), related_to="acme", status=None, target_kind=None,
                           category=None, skip=0, limit=100, _=None)

    assert len(got) == 3
    assert {g["category"] for g in got} == {"not-real", "wrong-percent", "wrong-owner"}


def test_related_to_and_the_badge_count_the_same_set(it_db, client):
    """One number, one meaning: a moderator reading "Disputed (3)" finds three."""
    _flag(it_db, node_id="acme")
    _flag(it_db, target_kind="owns", category="wrong-percent", from_id="acme", to_id="sub")
    _flag(it_db, node_id="elsewhere")

    rows = flags.list_flags(Response(), related_to="acme", status="open", target_kind=None,
                            category=None, skip=0, limit=100, _=None)
    badge = client.get("/v1/flags/summary", params={"related_to": "acme"}).json()

    assert badge["open"] == len(rows) == 2


def test_related_to_combines_with_status(it_db):
    fid = _flag(it_db, node_id="acme")
    _flag(it_db, target_kind="owns", category="wrong-percent", from_id="acme", to_id="sub")
    flags.update_flag_status(fid, FlagStatusUpdate(status="reviewing"), _=None)

    open_only = flags.list_flags(Response(), related_to="acme", status="open", target_kind=None,
                                 category=None, skip=0, limit=100, _=None)
    assert [f["category"] for f in open_only] == ["wrong-percent"]


def test_skip_walks_the_list_without_repeats_or_gaps(it_db):
    for i in range(5):
        _flag(it_db, node_id=f"e{i}")

    def page(skip):
        return flags.list_flags(Response(), skip=skip, limit=2, status=None,
                                target_kind=None, category=None, _=None)

    seen = [f["id"] for s in (0, 2, 4) for f in page(s)]

    assert len(seen) == 5                 # 2 + 2 + 1, the last page short
    assert len(set(seen)) == 5            # nothing repeated across pages
    assert page(5) == []                  # past the end is empty, not an error


def test_the_total_is_the_whole_match_not_the_page(it_db):
    for i in range(5):
        _flag(it_db, node_id=f"e{i}")

    totals = []
    for skip in (0, 2, 4):
        resp = Response()
        flags.list_flags(resp, skip=skip, limit=2, status=None, target_kind=None,
                         category=None, _=None)
        totals.append(resp.headers["X-Total-Count"])

    # The count a pager needs is how many there are, not how many came back.
    assert totals == ["5", "5", "5"]


def test_the_total_respects_the_filters(it_db):
    _flag(it_db, node_id="acme")
    _flag(it_db, node_id="other")

    resp = Response()
    flags.list_flags(resp, related_to="acme", skip=0, limit=100, status=None,
                     target_kind=None, category=None, _=None)
    assert resp.headers["X-Total-Count"] == "1"

