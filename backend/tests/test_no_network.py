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


def test_the_api_test_client_still_works(client):
    """`TestClient` drives the app through httpx at http://testserver, so the
    guard sees every router test as an outbound request. Blocking it would break
    the whole API suite at once — which is why `testserver` is allow-listed, and
    why that entry needs a test of its own: the database is reachable by two
    routes, so removing the allow-list alone leaves the DB tests passing and only
    this one failing."""
    assert client.get("/health").status_code == 200


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
