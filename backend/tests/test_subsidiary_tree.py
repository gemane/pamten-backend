"""The subsidiary-tree route: 404 for a missing company, the truncation header,
and the node cap's bounds. The walk itself runs against a real ArcadeDB in
tests/integration/test_subsidiary_tree_it.py."""
from unittest.mock import patch

from app.routers.relationships import SUBTREE_MAX_NODES


def test_a_missing_company_is_404(client):
    with patch("app.routers.relationships.subsidiary_tree_of", return_value=None):
        assert client.get("/relationships/subsidiary-tree/nope").status_code == 404


def test_the_tree_comes_back_with_the_truncation_header(client):
    tree = {"root_id": "e1", "nodes": [], "edges": [], "truncated": True}
    with patch("app.routers.relationships.subsidiary_tree_of", return_value=tree) as walk:
        r = client.get("/relationships/subsidiary-tree/lei:ABC", params={"max_nodes": 50})
    assert r.status_code == 200 and r.json() == tree
    assert r.headers["X-Result-Truncated"] == "true"
    walk.assert_called_once_with("lei:ABC", 50)


def test_the_node_cap_is_bounded(client):
    assert client.get("/relationships/subsidiary-tree/e1", params={"max_nodes": 0}).status_code == 422
    assert client.get("/relationships/subsidiary-tree/e1",
                      params={"max_nodes": SUBTREE_MAX_NODES + 1}).status_code == 422
