"""Nothing of a dissolved company stays open — after a scrape, too.

The rule ran where a writer stored `dissolved_on` and in the bulk chains, but
an edge written later onto a company already known to be dissolved stayed open
(Altaba: 105 open edges after the 2026-10-27 rebuild). The outermost scrape
scope now applies it to every touched company with an end — and, since
2026-10-29, to every company an edge was written onto, which a runner that
only resolves its company (Exhibit 21: Altaba's 78 holdings after the
2026-10-28 rebuild) never marks touched.
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


def test_an_edge_party_reaches_the_rule_but_not_the_dedup(monkeypatch):
    seen = {}
    monkeypatch.setattr(graph_writer, "_run_scoped_autodedup",
                        lambda p, e: seen.update(dedup=sorted(e)) or {})
    monkeypatch.setattr(graph_writer, "_end_dissolved_after_scrape",
                        lambda e: seen.update(dissolution=sorted(set(e))) or {})

    @graph_writer._with_autodedup
    def ex21_run():
        graph_writer._record_edge_parties("parent", "sub", None)   # resolved, never upserted
        graph_writer._record_touched_entity("sub")
        return {"status": "ok"}

    ex21_run()
    assert seen == {"dedup": ["sub"], "dissolution": ["parent", "sub"]}
    assert graph_writer._edge_entities.get() is None              # the scope is reset


def test_outside_a_scope_the_edge_parties_are_dropped():
    graph_writer._record_edge_parties("parent")
    assert graph_writer._edge_entities.get() is None


def test_an_acquisition_enriching_an_exhibit_21_edge_names_its_companies():
    """The enrich branch never reaches `_upsert_owns_sec`, so the acquisition
    writer has to name the pair itself."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock
    from app.scraper import sec_writer
    session = MagicMock()
    session.run.return_value.single.return_value = {"r": {"filing_type": "EX-21", "since": None}}

    @contextmanager
    def _ctx():
        yield session

    token = graph_writer._edge_entities.set(set())
    try:
        with patch.object(sec_writer.db, "get_session", _ctx), patch.object(sec_writer, "record_claim"), \
             patch.object(sec_writer, "_upsert_owns_sec") as upsert:
            out = sec_writer.write_acquisition_sec("altaba", "sub", "s", closing="2017-06-16", value_usd=None,
                                                   file_date="2017-08-09", source_url="u",
                                                   filing_type="10-Q XBRL")
        parties = graph_writer._edge_entities.get()
    finally:
        graph_writer._edge_entities.reset(token)
    assert out == "enriched" and not upsert.called
    assert parties == {"altaba", "sub"}
