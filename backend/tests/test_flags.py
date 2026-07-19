"""
Unit tests for the verification flags API. ArcadeDB is faked at the
db.get_session() seam; auth, validation and rate-limiting run for real.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_flag_rate_limit():
    from app.routers import flags
    with flags._flag_lock:
        flags._flag_events.clear()
    yield
    with flags._flag_lock:
        flags._flag_events.clear()


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


ENTITY_FLAG = {"target_kind": "entity", "node_id": "e1", "category": "not-real"}
OWNS_FLAG = {"target_kind": "owns", "from_id": "a", "to_id": "b", "category": "wrong-percent"}


# ── POST /flags — anyone can report ──────────────────────────────────────────

def test_anonymous_can_create_a_flag(client, fake_db):
    r = client.post("/flags", json=ENTITY_FLAG)
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def test_logged_in_user_can_create_a_flag(client, fake_db, make_token):
    r = client.post("/flags", json=ENTITY_FLAG, headers=_auth(make_token(role="viewer")))
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def test_anonymous_is_rate_limited_at_two_per_window(client, fake_db):
    assert client.post("/flags", json=ENTITY_FLAG).status_code == 200
    assert client.post("/flags", json=ENTITY_FLAG).status_code == 200
    assert client.post("/flags", json=ENTITY_FLAG).status_code == 429   # 3rd blocked


def test_logged_in_user_has_a_higher_ceiling(client, fake_db, make_token):
    h = _auth(make_token(role="viewer", sub="u-hi"))
    for _ in range(3):
        assert client.post("/flags", json=ENTITY_FLAG, headers=h).status_code == 200


def test_duplicate_report_is_collapsed(client, fake_db):
    # Existence check returns a row → treated as a repeat, no new flag.
    fake_db.queue([{"id": "existing-flag"}])
    r = client.post("/flags", json=ENTITY_FLAG)
    assert r.json()["status"] == "duplicate"
    assert r.json()["id"] == "existing-flag"


# ── validation ───────────────────────────────────────────────────────────────

def test_edge_flag_requires_endpoints(client, fake_db):
    r = client.post("/flags", json={"target_kind": "owns", "category": "wrong-percent"})
    assert r.status_code == 422


def test_role_flag_requires_role(client, fake_db):
    r = client.post("/flags", json={"target_kind": "role", "from_id": "p", "to_id": "e",
                                    "category": "wrong-role"})
    assert r.status_code == 422


def test_node_flag_requires_node_id(client, fake_db):
    r = client.post("/flags", json={"target_kind": "entity", "category": "not-real"})
    assert r.status_code == 422


def test_cannot_resolve_via_patch_in_phase_a(client, fake_db, make_token):
    r = client.patch("/flags/f1", json={"status": "resolved"},
                     headers=_auth(make_token(role="moderator")))
    assert r.status_code == 422


# ── GET /flags — moderator queue ─────────────────────────────────────────────

def test_queue_requires_moderator(client, fake_db, make_token):
    assert client.get("/flags").status_code == 401                       # anonymous
    assert client.get("/flags", headers=_auth(make_token(role="viewer"))).status_code == 403
    assert client.get("/flags", headers=_auth(make_token(role="contributor"))).status_code == 403


def test_moderator_sees_the_queue(client, fake_db, make_token):
    fake_db.queue([{"id": "f1", "target_kind": "entity", "category": "not-real",
                    "note": "", "status": "open", "reporter_kind": "anon",
                    "from_id": "", "to_id": "", "role": "", "node_id": "e1",
                    "created_at": "2026", "updated_at": "2026"}])
    r = client.get("/flags?status=open", headers=_auth(make_token(role="moderator")))
    assert r.status_code == 200
    assert r.json()[0]["id"] == "f1"


def test_admin_can_also_moderate(client, fake_db, make_token):
    fake_db.queue([])
    assert client.get("/flags", headers=_auth(make_token(role="admin"))).status_code == 200


# ── GET /flags/summary — disputed badge (public) ─────────────────────────────

def test_summary_returns_open_count(client, fake_db):
    fake_db.queue([{"n": 3}])
    r = client.get("/flags/summary?node_id=e1")
    assert r.status_code == 200
    assert r.json()["open"] == 3


def test_summary_needs_a_target(client, fake_db):
    assert client.get("/flags/summary").status_code == 400


# ── PATCH /flags/{id} — moderator triage ─────────────────────────────────────

def test_patch_requires_moderator(client, fake_db, make_token):
    r = client.patch("/flags/f1", json={"status": "reviewing"},
                     headers=_auth(make_token(role="viewer")))
    assert r.status_code == 403


def test_moderator_can_set_reviewing(client, fake_db, make_token):
    fake_db.queue([{"id": "f1"}])
    r = client.patch("/flags/f1", json={"status": "reviewing"},
                     headers=_auth(make_token(role="moderator")))
    assert r.status_code == 200
    assert r.json()["status"] == "reviewing"


def test_patch_unknown_flag_404(client, fake_db, make_token):
    fake_db.queue([])
    r = client.patch("/flags/nope", json={"status": "rejected"},
                     headers=_auth(make_token(role="moderator")))
    assert r.status_code == 404
