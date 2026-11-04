"""The suite cannot reach the internet, and that is enforced rather than hoped.

Unit tests were calling Wikidata, Nominatim, EDGAR and GLEIF for real. It cost
about a dozen seconds a run and went unnoticed for months, until one call timed
out in CI and failed a branch that had nothing to do with it — which is the
characteristic damage: not a wrong answer, but a failure that appears somewhere
else, later, and looks like somebody else's fault.

The guard lives in `tests/conftest.py` as an autouse fixture. These tests are its
own, because a guard nobody checks is a guard that quietly stops working.
"""
import httpx
import pytest


def test_an_outbound_request_is_refused():
    from tests.conftest import NetworkAccessAttempted

    with pytest.raises(NetworkAccessAttempted, match="example.test"):
        httpx.get("https://example.test/thing")


def test_the_message_names_the_host_and_the_way_out():
    # The one fact needed to write the missing mock, plus how to opt out on
    # purpose — an error that only says "blocked" sends you reading conftest.
    from tests.conftest import NetworkAccessAttempted

    with pytest.raises(NetworkAccessAttempted) as exc:
        httpx.get("https://query.wikidata.org/sparql")
    assert "query.wikidata.org" in str(exc.value)
    assert "allow_network" in str(exc.value)


def test_the_database_is_still_reachable():
    # Integration tests talk to ArcadeDB over HTTP. Blocking that would not make
    # the suite hermetic, it would make it useless — so localhost is allowed and
    # the connection error proves the request left the guard.
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://localhost:59999/", timeout=0.2)


def test_the_api_test_client_still_works(client, monkeypatch):
    """`TestClient` drives the app through httpx at http://testserver, so the
    guard sees every router test as an outbound request. Blocking it would break
    the whole API suite at once — which is why `testserver` is allow-listed, and
    why that entry needs a test of its own: the database is reachable by two
    routes, so removing the allow-list alone leaves the DB tests passing and only
    this one failing."""
    # `/` answers without touching the database; `/health` probes it, and a
    # database that does not answer reads as "unreachable" (503). The database
    # is stubbed down here: a unit test that reaches it fails (findings item 52).
    from app.db import arcadedb

    def down(*_a, **_kw):
        raise ConnectionError("ArcadeDB unreachable: stubbed")
    monkeypatch.setattr(arcadedb, "_post_raw", down)
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 503


@pytest.mark.allow_network
def test_a_test_can_opt_out_when_the_request_is_the_point():
    """No test uses this today. It exists so that needing it is a decision someone
    writes down, rather than a reason to weaken the guard for everybody.

    Addressed to 192.0.2.1 — RFC 5737 TEST-NET-1, reserved and non-routable, so
    the attempt fails at the socket without DNS and without troubling anyone
    real. Written against `localhost` first, which proved nothing: localhost is
    allow-listed, so the test passed whether the marker worked or not.
    """
    from tests.conftest import NetworkAccessAttempted

    try:
        httpx.get("http://192.0.2.1:1/", timeout=0.2)
    except NetworkAccessAttempted:                      # pragma: no cover
        pytest.fail("the marker did not lift the guard")
    except httpx.HTTPError:
        pass                                            # refused by the network, not by us


# ── Unit tests do not reach the database (findings item 52) ──────────────────
#
# A unit test mocks `db.get_session`, but helpers that query through `run_sql`
# themselves went out to localhost:2480 — 162 requests from 72 tests, nearly
# all swallowed by best-effort code. The guard refuses the request AND fails
# the test at teardown, because the swallowing would hide a mere refusal. Only
# a run from outside shows a teardown failure, so these drive a child pytest.

_PROBE = '''
import pytest

def _swallowed():
    from app.db.arcadedb import run_sql
    try:
        run_sql("SELECT 1")
    except Exception:
        pass

def test_swallowed_request():
    _swallowed()

@pytest.mark.allow_db
def test_allowed_request():
    _swallowed()

def test_refused():
    from tests.conftest import DatabaseAccessAttempted
    from app.db.arcadedb import run_sql
    with pytest.raises(DatabaseAccessAttempted):
        run_sql("SELECT 1")

def test_faked_request(fake_arcadedb):
    _swallowed()
    assert fake_arcadedb.sent("SELECT 1")
'''


def _run_probe(path):
    import os
    import subprocess
    import sys
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-p", "tests.conftest", "--rootdir", str(path.parent), str(path)],
        cwd=backend, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).stdout


def _outcome(out: str, name: str) -> set[str]:
    """The -rA summary words for one probe test (PASSED, and ERROR when its
    teardown failed — a test can carry both)."""
    import re
    return set(re.findall(rf"^(PASSED|FAILED|ERROR) \S*test_probe\.py::{name}\b", out, re.M)) - (
        {"PASSED"} if re.search(rf"^ERROR \S*test_probe\.py::{name}\b", out, re.M) else set())


def test_a_unit_test_that_reaches_the_database_fails(tmp_path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(_PROBE)
    out = _run_probe(probe)
    assert _outcome(out, "test_swallowed_request") == {"ERROR"}, out[-3000:]
    assert "unit test reached the database" in out and "/api/v1/command/" in out
    assert _outcome(out, "test_allowed_request") == {"PASSED"}     # the marker lets it out
    assert _outcome(out, "test_faked_request") == {"PASSED"}       # the fake answers it
    assert _outcome(out, "test_refused") == {"ERROR"}              # refused, never sent


def test_an_integration_test_still_reaches_the_database(tmp_path):
    folder = tmp_path / "tests" / "integration"
    folder.mkdir(parents=True)
    probe = folder / "test_probe.py"
    probe.write_text(_PROBE)
    out = _run_probe(probe)
    assert _outcome(out, "test_swallowed_request") == {"PASSED"}, out[-3000:]
