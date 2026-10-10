"""The full profile stamps `descendants` on each subsidiary's relationship from
the tree walk — the same walk, as of the same day — and loses the figures, not
the profile, when the walk cannot run. Mocked session; the walk itself and the
real numbers are pinned in tests/integration/test_profile_counts_it.py."""
from unittest.mock import patch

import pytest

ENTITY = {"e": {"id": "e1", "name": "E", "type": "company"}}
SUBS = [{"node": {"id": "s1", "name": "S1", "type": "company"}, "rels": [{"stake_percent": 100.0, "source_id": "s"}]},
        {"node": {"id": "s2", "name": "S2", "type": "company"}, "rels": [{"source_id": "s"}]}]


def _tree(truncated=False):
    return {"root_id": "e1", "truncated": truncated,
            "nodes": [{"entity": {"id": "s1"}, "parent_id": "e1", "depth": 1},
                      {"entity": {"id": "g"}, "parent_id": "s1", "depth": 2}],
            "edges": [{"from_id": "e1", "to_id": "s1", "depth": 1, "relationship": {"descendants": 1}},
                      {"from_id": "s1", "to_id": "g", "depth": 2, "relationship": {"descendants": 0}}]}


@pytest.fixture
def no_sql():
    # the profile's claim lookups go through run_sql, outside the fake session —
    # a mocked suite must never reach a real server (the brute-force lockout)
    with patch("app.routers.search.run_sql", return_value=[]), \
         patch("app.claims.run_sql", return_value=[], create=True), \
         patch("app.db.arcadedb.run_sql", return_value=[]):
        yield


def _profile(client, fake_db, **params):
    fake_db.queue([ENTITY], [], SUBS)          # head, owners, subsidiaries; the rest empty
    r = client.get("/search/entity/e1/full-profile", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_each_subsidiary_carries_the_companies_below_it_as_of_the_same_day(client, fake_db, no_sql):
    with patch("app.routers.relationships.subsidiary_tree_of", return_value=_tree()) as walk:
        body = _profile(client, fake_db, as_of="2019-12-31")
    walk.assert_called_once_with("e1", as_of="2019-12-31")
    by = {s["entity"]["id"]: s["relationship"] for s in body["subsidiaries"]}
    assert by["s1"]["descendants"] == 1
    assert by["s2"]["descendants"] == 0            # not in the walk's edges: nothing below it
    assert body["descendants_truncated"] is False


def test_a_capped_walk_is_said_so(client, fake_db, no_sql):
    with patch("app.routers.relationships.subsidiary_tree_of", return_value=_tree(truncated=True)):
        assert _profile(client, fake_db)["descendants_truncated"] is True


def test_the_present_walks_the_present(client, fake_db, no_sql):
    with patch("app.routers.relationships.subsidiary_tree_of", return_value=_tree()) as walk:
        _profile(client, fake_db)
    walk.assert_called_once_with("e1", as_of=None)


def test_nothing_below_means_no_walk(client, fake_db, no_sql):
    with patch("app.routers.relationships.subsidiary_tree_of") as walk:
        fake_db.queue([ENTITY])
        body = client.get("/search/entity/e1/full-profile").json()
    walk.assert_not_called()
    assert body["subsidiaries"] == [] and body["descendants_truncated"] is False


@pytest.mark.parametrize("outcome", [RuntimeError("db down"), None])
def test_a_failed_walk_loses_the_figures_not_the_profile(client, fake_db, no_sql, outcome):
    kw = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    with patch("app.routers.relationships.subsidiary_tree_of", **kw):
        body = _profile(client, fake_db)
    assert [s["entity"]["id"] for s in body["subsidiaries"]] == ["s1", "s2"]
    assert all("descendants" not in s["relationship"] for s in body["subsidiaries"])
    assert body["descendants_truncated"] is False
