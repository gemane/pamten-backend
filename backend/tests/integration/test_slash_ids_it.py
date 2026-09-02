"""Ids with slashes must survive HTTP routing.

PSC-derived entities and persons carry the Companies House self-link in their
id (chpsc:/company/…/corporate-entity/…). The ASGI server percent-decodes the
path BEFORE routing, so a plain {id} path parameter can never match one — the
request 404s at the router and the page for that company simply fails to load
(found via the second Tesla, 2026-09-03). The {id:path} converter fixes that;
these tests drive the REAL HTTP layer, because calling the handler directly
would bypass the exact thing that was broken.

The converter is greedy, so registration order now matters: the second half
pins that the static routes registered after the catch-alls still resolve.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration

EID = "chpsc:/company/09533203/persons-with-significant-control/corporate-entity/louTest123"
PID = "chpsc:/company/08810260/persons-with-significant-control/individual/Q4oTest456"


@pytest.fixture()
def slashed(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: $id, name: 'Tesla, Inc.', name_normalized: 'tesla', "
        "search_text: 'Tesla, Inc.', type: 'company'})", {"id": EID})
    it_db.run_command(
        "CREATE (:Person {id: $id, full_name: 'Jane Slash', "
        "name_normalized: 'jane slash', search_text: 'Jane Slash'})", {"id": PID})
    with TestClient(app) as c:
        yield c


def test_the_entity_profile_loads_over_http(slashed):
    r = slashed.get(f"/v1/search/entity/{EID}/full-profile")
    assert r.status_code == 200
    assert r.json()["entity"]["name"] == "Tesla, Inc."


def test_the_person_profile_loads_over_http(slashed):
    r = slashed.get(f"/v1/search/person/{PID}/full-profile")
    assert r.status_code == 200
    assert r.json()["person"]["full_name"] == "Jane Slash"


def test_the_entity_read_and_its_sources_and_owners_load(slashed):
    assert slashed.get(f"/v1/entities/{EID}").status_code == 200
    assert slashed.get(f"/v1/sources/entity/{EID}").status_code == 200
    assert slashed.get(f"/v1/relationships/owners/{EID}").status_code == 200
    assert slashed.get(f"/v1/relationships/ownership-tree/{EID}").status_code == 200
    assert slashed.get(f"/v1/relationships/history/{EID}").status_code == 200
    assert slashed.get(f"/v1/sources/person/{PID}").status_code == 200


def test_the_greedy_catchall_does_not_shadow_the_static_routes(slashed):
    """Order is the guard: these would all parse as ids if the catch-alls were
    registered first, and 'kept-separate' would 404 as an unknown entity."""
    assert slashed.get("/v1/entities/").status_code == 200
    assert slashed.get("/v1/persons/").status_code == 200
    # These are auth-gated: 401 proves the STATIC route matched (the entity
    # catch-all is public and would answer 404 for an unknown "id").
    for path in ("/v1/entities/kept-separate", "/v1/entities/merge-log",
                 "/v1/persons/kept-separate"):
        assert slashed.get(path).status_code in (200, 401), path


def test_an_unknown_slashed_id_is_a_handler_404_not_a_router_404(slashed):
    r = slashed.get("/v1/search/entity/chpsc:/company/0/x/y/full-profile")
    assert r.status_code == 404
    assert r.json()["detail"] == "Entity not found", \
        "the generic 'Not Found' means the route never matched"
