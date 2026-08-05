"""
Bounds on the graph-walking read endpoints.

/relationships/{ownership-tree,owners,history} used to return every row the query
produced. On a hub node — a nominee custodian, a large holding — that is tens of
thousands of rows: a slow query and a payload no phone can use. Each now has a
bounded default and reports truncation in the X-Result-Truncated header.

The header exists because these endpoints return bare JSON arrays. Wrapping them
in an envelope would break every already-released client, since the unversioned
mount still serves them (see main.py).
"""
import pytest

from app.routers.relationships import (
    TRUNCATED_HEADER,
    TREE_DEFAULT_LIMIT, TREE_MAX_LIMIT,
    OWNERS_DEFAULT_LIMIT, OWNERS_MAX_LIMIT,
    HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT,
)
from app.routers.search import SEARCH_MAX_LIMIT


class _FakePath:
    """Minimal stand-in for a Cypher path record."""
    def __init__(self, i: int):
        self.nodes = [{"id": f"n{i}"}]
        self.relationships = [{"stake_percent": 1}]


def _paths(n: int) -> list[dict]:
    return [{"path": _FakePath(i)} for i in range(n)]


def _owner_rows(n: int) -> list[dict]:
    return [{"owner": {"id": f"o{i}", "name": f"Owner {i}"}, "r": {"until": None}} for i in range(n)]


# The three history queries return different keys: inbound ownership binds `owner`,
# outbound binds `owned`, roles bind `p`. A single row shape won't do.
def _owned_rows(n: int) -> list[dict]:
    return [{"owned": {"id": f"s{i}", "name": f"Sub {i}"}, "r": {"until": None}} for i in range(n)]


def _role_rows(n: int) -> list[dict]:
    return [{"p": {"id": f"p{i}", "full_name": f"Person {i}"}, "r": {"until": None, "role": "CEO"}}
            for i in range(n)]


# ── ownership-tree ────────────────────────────────────────────────────────────

def test_tree_returns_everything_when_under_the_limit(client, fake_db):
    fake_db.queue(_paths(3))
    r = client.get("/relationships/ownership-tree/e1", params={"limit": 10})
    assert r.status_code == 200
    assert len(r.json()) == 3
    assert r.headers[TRUNCATED_HEADER] == "false"


def test_tree_trims_to_the_limit_and_flags_truncation(client, fake_db):
    # The handler asks for limit+1 rows to detect "there was more" without a
    # second count query; the extra row must not reach the caller.
    fake_db.queue(_paths(6))
    r = client.get("/relationships/ownership-tree/e1", params={"limit": 5})
    assert len(r.json()) == 5
    assert r.headers[TRUNCATED_HEADER] == "true"


def test_tree_exactly_at_the_limit_is_not_reported_as_truncated(client, fake_db):
    # The boundary the +1 fetch exists to get right: exactly `limit` rows means
    # nothing was cut, even though the response is full.
    fake_db.queue(_paths(5))
    r = client.get("/relationships/ownership-tree/e1", params={"limit": 5})
    assert len(r.json()) == 5
    assert r.headers[TRUNCATED_HEADER] == "false"


def test_tree_applies_a_default_limit(client, fake_db):
    fake_db.queue(_paths(TREE_DEFAULT_LIMIT + 1))
    r = client.get("/relationships/ownership-tree/e1")
    assert len(r.json()) == TREE_DEFAULT_LIMIT
    assert r.headers[TRUNCATED_HEADER] == "true"


def test_tree_sends_the_limit_to_the_database(client, fake_db):
    # Trimming in Python alone would still make ArcadeDB materialise every path.
    fake_db.queue(_paths(2))
    client.get("/relationships/ownership-tree/e1", params={"limit": 7})
    cypher = fake_db.calls[0][0]
    assert "LIMIT 8" in cypher  # limit + 1


def test_tree_rejects_a_limit_over_the_ceiling(client):
    assert client.get("/relationships/ownership-tree/e1",
                      params={"limit": TREE_MAX_LIMIT + 1}).status_code == 422


def test_tree_still_caps_depth(client, fake_db):
    # Depth is interpolated into the Cypher, so the clamp is what keeps a caller
    # from asking for an unbounded traversal.
    fake_db.queue(_paths(1))
    client.get("/relationships/ownership-tree/e1", params={"depth": 99})
    assert "OWNS*1..10" in fake_db.calls[0][0]


# ── owners ────────────────────────────────────────────────────────────────────

def test_owners_trims_and_flags_truncation(client, fake_db):
    fake_db.queue(_owner_rows(4), [], [], [])  # rows, suppressions, hidden, pins
    r = client.get("/relationships/owners/e1", params={"limit": 3})
    assert r.status_code == 200
    assert len(r.json()) == 3
    assert r.headers[TRUNCATED_HEADER] == "true"


def test_owners_applies_a_default_limit(client, fake_db):
    fake_db.queue(_owner_rows(OWNERS_DEFAULT_LIMIT + 1), [], [], [])
    r = client.get("/relationships/owners/e1")
    assert len(r.json()) == OWNERS_DEFAULT_LIMIT
    assert r.headers[TRUNCATED_HEADER] == "true"


def test_owners_rejects_a_limit_over_the_ceiling(client):
    assert client.get("/relationships/owners/e1",
                      params={"limit": OWNERS_MAX_LIMIT + 1}).status_code == 422


# ── history ───────────────────────────────────────────────────────────────────

def test_history_limit_applies_per_category(client, fake_db):
    # Inbound ownership, outbound ownership and roles are three queries. Limiting
    # the merged total would let one noisy category crowd out the others, so the
    # response can hold up to 3 x limit events.
    fake_db.queue(_owner_rows(2), _owned_rows(2), [])
    r = client.get("/relationships/history/e1", params={"limit": 2})
    assert r.status_code == 200
    assert len(r.json()) == 4
    assert r.headers[TRUNCATED_HEADER] == "false"


def test_history_flags_truncation_from_any_category(client, fake_db):
    # Only the third query overflows — the flag must still be set.
    fake_db.queue([], [], _role_rows(3))
    r = client.get("/relationships/history/e1", params={"limit": 2})
    assert r.headers[TRUNCATED_HEADER] == "true"


def test_history_rejects_a_limit_over_the_ceiling(client):
    assert client.get("/relationships/history/e1",
                      params={"limit": HISTORY_MAX_LIMIT + 1}).status_code == 422


def test_history_default_limit_is_bounded():
    assert 0 < HISTORY_DEFAULT_LIMIT <= HISTORY_MAX_LIMIT


# ── search ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [0, SEARCH_MAX_LIMIT + 1])
def test_search_rejects_out_of_range_limits(client, bad):
    assert client.get("/search/", params={"q": "abc", "limit": bad}).status_code == 422


# ── Callable directly, not only over HTTP ─────────────────────────────────────
#
# These functions are called in-process as well as served (integration tests do,
# and app code could). FastAPI only resolves `Query(...)` defaults when a request
# comes through the router, so `limit: int = Query(20)` handed a direct caller a
# Query *object*: the first comparison raised
# "'>=' not supported between instances of 'int' and 'Query'".
# The mocked suite missed it entirely because it goes through TestClient; only
# the real-ArcadeDB job, which calls the functions directly, caught it.
#
# The fix is two-part: Annotated[...] so the default is a real int, and a plain
# core function for the endpoints that need a Response to set the header.

import inspect  # noqa: E402

from app.routers.search import search, SEARCH_DEFAULT_LIMIT  # noqa: E402
from app.routers.relationships import (  # noqa: E402
    owners_of, ownership_tree_of, ownership_history_of,
)


@pytest.mark.parametrize("fn", [search, owners_of, ownership_tree_of, ownership_history_of])
def test_defaults_are_real_values_not_fastapi_objects(fn):
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is inspect.Parameter.empty:
            continue
        assert isinstance(param.default, (int, str, bool, type(None))), (
            f"{fn.__name__}({name}=...) defaults to {type(param.default).__name__}; "
            "a direct caller would receive that object instead of a value"
        )


@pytest.mark.parametrize("fn", [owners_of, ownership_tree_of, ownership_history_of])
def test_core_functions_take_no_response_argument(fn):
    # The Response is the route's business. A core function requiring one can't be
    # called in-process without inventing a fake.
    assert "response" not in inspect.signature(fn).parameters


def test_search_limit_default_matches_the_route(client, fake_db):
    assert inspect.signature(search).parameters["limit"].default == SEARCH_DEFAULT_LIMIT
