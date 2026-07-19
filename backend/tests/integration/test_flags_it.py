"""
Real-ArcadeDB test for the verification flags API — exercises the create /
duplicate-collapse / summary-count / queue / patch Cypher end-to-end (the
mocked unit suite can't catch dialect or count-shape bugs).
"""
from types import SimpleNamespace

import pytest

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
    queue = flags.list_flags(status="open", target_kind=None, category=None, limit=100, _=None)
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
