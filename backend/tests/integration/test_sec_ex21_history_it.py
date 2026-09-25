"""Dating Exhibit 21 subsidiaries by their oldest listing, against a real ArcadeDB.

The writer only ever moves a start date EARLIER and only on an active Exhibit
21/8.1 edge; the runner dates only subsidiaries listed before the newest
filing; the timeline endpoint says the date is a lower bound.
"""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    it_db.run_command("CREATE (:Entity {id:'nc', name:'News Corp', sec_cik:'0001564708', "
                      "search_text:'News Corp'})")
    subs = [("dj", "Dow Jones & Company, Inc.", "EX-21", None, None),
            ("hc", "HarperCollins Publishers L.L.C.", "EX-21", "2026-08-07", None),  # the invented date
            ("st", "Storyful Limited", "EX-21", None, None),
            ("old", "Earlier Start Ltd", "EX-21", "2001-01-01", None),               # already earlier
            ("sold", "Sold Off Inc.", "EX-21", None, "2020-01-01"),                  # closed
            ("fund", "A 13F Holding", "13F", None, None)]                           # not a list edge
    for sid, name, ft, since, until in subs:
        it_db.run_command("CREATE (:Entity {id:$id, name:$n})", {"id": sid, "n": name})
        it_db.run_command(
            "MATCH (a:Entity {id:'nc'}), (b:Entity {id:$id}) CREATE (a)-[:OWNS {filing_type:$ft, "
            "since:$since, until:$until, source_date:'2026-08-07', source_id:'sec'}]->(b)",
            {"id": sid, "ft": ft, "since": since, "until": until})
        it_db.run_sql("INSERT INTO Claim SET claim_key = :k, kind = 'owns', from_id = 'nc', to_id = :id, "
                      "filing_type = :ft, since = :since, source_date = '2026-08-07'",
                      {"k": f"owns|nc|{sid}", "id": sid, "ft": ft, "since": since})


def _edge(it_db, sid):
    return it_db.run_command(
        "MATCH (:Entity {id:'nc'})-[r:OWNS]->(:Entity {id:$id}) "
        "RETURN r.since AS since, r.since_basis AS basis, r.since_source_url AS url", {"id": sid})[0]


def _claim_since(it_db, sid):
    return it_db.run_sql("SELECT since FROM Claim WHERE to_id = :id", {"id": sid})[0].get("since")


def _claim(it_db, sid):
    r = it_db.run_sql("SELECT since, since_basis, since_source_url FROM Claim WHERE to_id = :id",
                      {"id": sid})[0]
    return {k: r.get(k) for k in ("since", "since_basis", "since_source_url")}


class TestTheWriter:
    def test_dates_an_undated_edge_and_its_claim(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _seed(it_db)
        assert set_since_lower_bound("nc", "dj", "2014-08-14", "https://www.sec.gov/2014.htm") is True
        assert _edge(it_db, "dj") == {"since": "2014-08-14", "basis": "first_listed",
                                      "url": "https://www.sec.gov/2014.htm"}
        assert _claim(it_db, "dj") == {"since": "2014-08-14", "since_basis": "first_listed",
                                       "since_source_url": "https://www.sec.gov/2014.htm"}

    def test_replaces_a_later_date_but_never_moves_one_later(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _seed(it_db)
        assert set_since_lower_bound("nc", "hc", "2014-08-14", None) is True
        assert _edge(it_db, "hc")["since"] == "2014-08-14"
        assert set_since_lower_bound("nc", "hc", "2019-08-13", None) is False
        assert _edge(it_db, "hc")["since"] == "2014-08-14"
        assert set_since_lower_bound("nc", "old", "2014-08-14", None) is False
        assert _edge(it_db, "old") == {"since": "2001-01-01", "basis": None, "url": None}

    def test_leaves_closed_and_non_list_edges_alone(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _seed(it_db)
        assert set_since_lower_bound("nc", "sold", "2014-08-14", None) is False
        assert set_since_lower_bound("nc", "fund", "2014-08-14", None) is False
        assert _edge(it_db, "fund")["since"] is None and _claim_since(it_db, "fund") is None


def _history():
    from app.scraper.mapper import normalize_entity_name as n
    def entry(d, names):
        return {"filing_date": d, "form": "10-K", "url": f"https://www.sec.gov/{d}.htm",
                "names": None if names is None else {n(x) for x in names}}
    return [entry("2026-08-07", ["Dow Jones & Company, Inc.", "HarperCollins Publishers L.L.C.",
                                 "Storyful Limited", "Earlier Start Ltd"]),
            entry("2025-08-08", ["DOW JONES & COMPANY INC", "HarperCollins Publishers L.L.C.",
                                 "Earlier Start Ltd"]),
            entry("2024-08-08", ["Dow Jones & Company, Inc."]),
            entry("2003-09-02", None)]                                  # unreadable old exhibit


class TestTheRunner:
    def test_dates_what_predates_the_newest_filing(self, it_db, monkeypatch):
        from app.config import settings
        from app.scraper import runner
        monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
        monkeypatch.setattr(settings, "SCRAPER_SEC_EDGAR_ENABLED", True)
        _seed(it_db)
        with patch("app.routers.search.resolve_best_entity",
                   return_value={"id": "nc", "sec_cik": "0001564708"}), \
             patch("app.scraper.sec_ex21.fetch_subsidiary_history", return_value=_history()):
            res = runner.run_sec_ex21_history("News Corp")
        assert res["status"] == "ok"
        # Dow Jones listed back to 2024 (then an unreadable year), HarperCollins
        # to 2025; Storyful only this year → nothing to add; the earlier start
        # stays; the 13F and the closed edge are not Exhibit 21 subsidiaries.
        assert _edge(it_db, "dj")["since"] == "2024-08-08"
        assert _edge(it_db, "hc")["since"] == "2025-08-08"
        assert _edge(it_db, "st")["since"] is None
        assert _edge(it_db, "old")["since"] == "2001-01-01"
        assert res["total"] == 2 and res["subsidiaries"] == 4 and res["unmatched"] == 0
        assert res["readable"] == 3 and res["earliest_dated"] == "2024-08-08"

    def test_the_timeline_says_it_is_a_lower_bound(self, it_db):
        from app.routers.relationships import ownership_history_of
        from app.scraper.sec_writer import set_since_lower_bound
        _seed(it_db)
        set_since_lower_bound("nc", "dj", "2014-08-14", "https://www.sec.gov/2014.htm")
        events, _ = ownership_history_of("nc", 100)
        by_party = {e["party"]["id"]: e for e in events if e["kind"] == "ownership_out"}
        assert by_party["dj"]["since"] == "2014-08-14"
        assert by_party["dj"]["since_basis"] == "first_listed"
        assert by_party["st"]["since_basis"] is None       # undated, not a lower bound
        # …and from the subsidiary's side: "held by News Corp since at least 2014"
        owned_by = [e for e in ownership_history_of("dj", 100)[0] if e["kind"] == "ownership_in"]
        assert [(e["party"]["id"], e["since"], e["since_basis"]) for e in owned_by] == \
            [("nc", "2014-08-14", "first_listed")]
