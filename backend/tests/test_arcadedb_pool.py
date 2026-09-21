"""Tests for the pooled ArcadeDB HTTP client."""

import httpx
import pytest
from unittest.mock import MagicMock, patch

from app.db import arcadedb


@pytest.fixture(autouse=True)
def _reset_client():
    arcadedb.close_client()
    yield
    arcadedb.close_client()


def test_get_client_is_a_reused_singleton():
    c1 = arcadedb._get_client()
    c2 = arcadedb._get_client()
    assert c1 is c2  # pooled, not rebuilt per call


def test_client_is_configured_with_keepalive_limits():
    c = arcadedb._get_client()
    assert isinstance(c, httpx.Client)
    pool = c._transport._pool
    assert pool._max_connections == 10
    assert pool._max_keepalive_connections == 5


def test_close_client_disposes_and_rebuilds():
    c1 = arcadedb._get_client()
    arcadedb.close_client()
    c2 = arcadedb._get_client()
    assert c1 is not c2  # a fresh client after close


def _fake_client(response=None, raise_exc=None):
    client = MagicMock()
    if raise_exc is not None:
        client.post.side_effect = raise_exc
    else:
        client.post.return_value = response
    return client


def test_post_returns_result_list_on_success():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result": [{"id": "1"}]}
    with patch.object(arcadedb, "_get_client", return_value=_fake_client(resp)):
        assert arcadedb.run_query("MATCH (n) RETURN n") == [{"id": "1"}]


def test_post_reuses_client_across_many_calls():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result": []}
    fake = _fake_client(resp)
    with patch.object(arcadedb, "_get_client", return_value=fake):
        for _ in range(50):
            arcadedb.run_command("CREATE (n)")
    assert fake.post.call_count == 50  # all issued through the one client


def test_post_raises_runtime_error_on_non_2xx():
    resp = MagicMock(status_code=500, text="boom")
    with patch.object(arcadedb, "_get_client", return_value=_fake_client(resp)):
        with pytest.raises(RuntimeError):
            arcadedb.run_query("MATCH (n) RETURN n")


def test_post_maps_request_error_to_connection_error():
    exc = httpx.ConnectError("refused")
    with patch.object(arcadedb, "_get_client", return_value=_fake_client(raise_exc=exc)):
        with pytest.raises(ConnectionError):
            arcadedb.run_query("MATCH (n) RETURN n")


def test_explain_sql_returns_the_plan_text_not_the_empty_result():
    """EXPLAIN answers with an empty `result` and the plan under `explain`;
    the plain helpers drop it, this one is for tests that pin a query to its
    index (`FETCH FROM INDEX` vs `FETCH FROM TYPE`)."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result": [], "explain": "+ FETCH FROM INDEX Entity[id] ()\n  id > ''"}
    fake = _fake_client(resp)
    with patch.object(arcadedb, "_get_client", return_value=fake):
        plan = arcadedb.explain_sql("SELECT id FROM Entity WHERE id > '' ORDER BY id")
    assert plan.startswith("+ FETCH FROM INDEX")
    body = fake.post.call_args.kwargs["json"]
    assert body["language"] == "sql"
    assert body["command"].startswith("EXPLAIN SELECT id FROM Entity")


def test_a_response_without_a_result_key_is_an_empty_list():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"user": "root"}
    with patch.object(arcadedb, "_get_client", return_value=_fake_client(resp)):
        assert arcadedb.run_sql("CREATE VERTEX TYPE X") == []
