"""Nothing of a dissolved company stays open — after a scrape, too.

The rule ran where a writer stored `dissolved_on` and in the bulk chains, but
an edge written later onto a company already known to be dissolved stayed open
(Altaba: 105 open edges after the 2026-10-27 rebuild). The outermost scrape
scope now applies it to every touched company with an end.
"""
from unittest.mock import patch

from app.scraper import graph_writer


def _sql(dissolved):
    calls = []

    def fake(sql, params=None, **_):
        calls.append(params["ids"])
        return [{"id": i} for i in params["ids"] if i in dissolved]
    return fake, calls


def test_the_touched_companies_with_an_end_are_closed():
    fake, calls = _sql({"gone"})
    with patch("app.db.arcadedb.run_sql", side_effect=fake), \
         patch("app.dissolution.end_at_dissolution", return_value={"closed": 7}) as end:
        out = graph_writer._end_dissolved_after_scrape(["live", "gone", "gone", None])
    assert calls == [["live", "gone"]]
    end.assert_called_once_with("gone")
    assert out == {"dissolution": {"closed": 7}}


def test_a_scrape_without_one_reports_nothing():
    fake, _ = _sql(set())
    with patch("app.db.arcadedb.run_sql", side_effect=fake), \
         patch("app.dissolution.end_at_dissolution") as end:
        assert graph_writer._end_dissolved_after_scrape(["live"]) == {}
        assert graph_writer._end_dissolved_after_scrape([]) == {}
    end.assert_not_called()


def test_a_large_scrape_is_looked_up_in_chunks():
    fake, calls = _sql(set())
    with patch("app.db.arcadedb.run_sql", side_effect=fake):
        graph_writer._end_dissolved_after_scrape([f"e{i}" for i in range(1201)])
    assert [len(c) for c in calls] == [500, 500, 201]


def test_a_failure_never_fails_the_scrape():
    with patch("app.db.arcadedb.run_sql", side_effect=RuntimeError("down")):
        out = graph_writer._end_dissolved_after_scrape(["gone"])
    assert out["dissolution"]["status"] == "error"


def test_the_outermost_scope_applies_it_after_the_dedup(monkeypatch):
    order = []
    monkeypatch.setattr(graph_writer, "_run_scoped_autodedup", lambda p, e: order.append("dedup") or {})
    monkeypatch.setattr(graph_writer, "_end_dissolved_after_scrape",
                        lambda e: order.append(("dissolution", sorted(e))) or {"dissolution": {"closed": 1}})

    @graph_writer._with_autodedup
    def inner():
        graph_writer._record_touched_entity("gone")
        return {"status": "ok"}

    @graph_writer._with_autodedup
    def scrape():
        graph_writer._record_touched_entity("live")
        assert "dissolution" not in inner()           # nested: the outer scope applies it
        return {"status": "ok"}

    assert scrape()["dissolution"] == {"closed": 1}
    assert order == ["dedup", ("dissolution", ["gone", "live"])]
