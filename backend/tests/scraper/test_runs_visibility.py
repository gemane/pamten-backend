"""Who may read the scrape run log, and how much of it.

`GET /scraper/runs` is public: what the platform ingests is the kind of thing an
ownership-transparency project should be transparent about. The exception text is
not public — it can carry internal URLs, database errors, or a credential
embedded in a failing request URL.

These drive the endpoint over HTTP so the auth dependency actually runs; the run
log itself is stubbed.
"""
from unittest.mock import patch

import pytest

RUNS = [
    {"id": "r1", "source": "wikidata", "target": "Acme Corp", "status": "failed",
     "started_at": "2026-08-07T10:00:00Z", "finished_at": "2026-08-07T10:00:09Z",
     "total": 0, "error": "HTTPError 503 from https://internal.example/x?key=hunter2"},
    {"id": "r2", "source": "sec_edgar", "target": "Globex", "status": "ok",
     "started_at": "2026-08-07T09:00:00Z", "finished_at": "2026-08-07T09:01:00Z",
     "total": 42, "error": ""},
]


@pytest.fixture(autouse=True)
def _stub_run_log():
    with patch("app.scraper.router.list_runs", return_value=[dict(r) for r in RUNS]) as m:
        yield m


def _get(client, make_token=None, role=None):
    headers = {"Authorization": f"Bearer {make_token(role=role)}"} if role else {}
    r = client.get("/scraper/runs", headers=headers)
    assert r.status_code == 200
    return r.json()


# ── Public access ─────────────────────────────────────────────────────────────

def test_anonymous_may_read_the_log(client):
    body = _get(client)
    assert body["count"] == 2


def test_anonymous_sees_what_ran_and_how_it_went(client):
    """The transparency payload: source, target, timings, counts, outcome."""
    run = _get(client)["runs"][0]
    assert run["source"] == "wikidata"
    assert run["target"] == "Acme Corp"
    assert run["status"] == "failed"          # the failure is not hidden
    assert run["total"] == 0
    assert run["started_at"] and run["finished_at"]


def test_anonymous_never_sees_the_exception_text(client):
    # The stub deliberately embeds a credential in the error, which is exactly
    # what must not reach an unauthenticated caller.
    body = _get(client)
    assert all("error" not in run for run in body["runs"])
    assert "hunter2" not in str(body)


def test_a_signed_in_viewer_is_still_redacted(client, make_token):
    """Being logged in is not the bar — having a reason to debug is."""
    body = _get(client, make_token, "viewer")
    assert all("error" not in run for run in body["runs"])


def test_a_moderator_is_redacted_too(client, make_token):
    # Moderators moderate data, they don't run scrapers.
    body = _get(client, make_token, "moderator")
    assert all("error" not in run for run in body["runs"])


# ── Privileged access ─────────────────────────────────────────────────────────

def test_contributor_sees_the_error(client, make_token):
    body = _get(client, make_token, "contributor")
    assert body["runs"][0]["error"].startswith("HTTPError 503")


def test_admin_sees_the_error(client, make_token):
    body = _get(client, make_token, "admin")
    assert body["runs"][0]["error"].startswith("HTTPError 503")


def test_redaction_leaves_every_other_field_intact(client, make_token):
    """Guards against a redaction that strips more than it should."""
    anon = _get(client)["runs"][0]
    priv = _get(client, make_token, "admin")["runs"][0]
    assert set(priv) - set(anon) == {"error"}


# ── Unchanged behaviour ───────────────────────────────────────────────────────

def test_limit_is_passed_through(client, _stub_run_log):
    client.get("/scraper/runs", params={"limit": 5})
    assert _stub_run_log.call_args[0][0] == 5


def test_limit_is_still_capped(client):
    # Opening the endpoint up must not also open up how much it will return.
    assert client.get("/scraper/runs", params={"limit": 501}).status_code == 422


def test_an_invalid_token_is_still_rejected(client):
    """Optional auth means "no credentials is fine", not "any credentials".

    A garbage token is a broken client or an attack, and silently treating it as
    anonymous would hide both.
    """
    r = client.get("/scraper/runs", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
