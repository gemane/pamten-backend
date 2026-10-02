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

    def test_writes_the_basis_and_rewrites_it_when_a_re_read_knows_more(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _seed(it_db)
        assert set_since_lower_bound("nc", "dj", "2025-06-30", "u", "newly_listed") is True
        assert _edge(it_db, "dj")["basis"] == "newly_listed"
        assert _claim(it_db, "dj")["since_basis"] == "newly_listed"
        # the same date and basis again: nothing to do
        assert set_since_lower_bound("nc", "dj", "2025-06-30", "u", "newly_listed") is False
        # an older list turned up naming it: the same date is only a lower bound again
        assert set_since_lower_bound("nc", "dj", "2025-06-30", "u", "first_listed") is True
        assert _edge(it_db, "dj")["basis"] == "first_listed"
        assert _claim(it_db, "dj")["since_basis"] == "first_listed"

    def test_a_stated_start_on_the_same_day_is_never_relabelled(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        _seed(it_db)
        assert set_since_lower_bound("nc", "old", "2001-01-01", "u", "newly_listed") is False
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
        # News Corp's fiscal year ends in June; each list is as of that year-end
        return {"as_of": d[:4] + "-06-30", "filing_date": d, "form": "10-K",
                "url": f"https://www.sec.gov/{d}.htm",
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
        # Dated by the fiscal year-end of the oldest list in the unbroken run:
        # Dow Jones back to FY2024 (then an unreadable year), HarperCollins to
        # FY2025, Storyful listed only this year → this year's year-end ("since
        # 2026 or earlier" is true too). The earlier start stays; the 13F and
        # the closed edge are not Exhibit 21 subsidiaries.
        # What the history knows about BEFORE: Dow Jones' run ends at an
        # unreadable year → a lower bound; for HarperCollins and Storyful the
        # list before was read and does not name them → first listed that year.
        assert (_edge(it_db, "dj")["since"], _edge(it_db, "dj")["basis"]) == ("2024-06-30", "first_listed")
        assert (_edge(it_db, "hc")["since"], _edge(it_db, "hc")["basis"]) == ("2025-06-30", "newly_listed")
        assert _edge(it_db, "st") == {"since": "2026-06-30", "basis": "newly_listed",
                                      "url": "https://www.sec.gov/2026-08-07.htm"}
        assert _claim(it_db, "st")["since_basis"] == "newly_listed"
        assert _edge(it_db, "old") == {"since": "2001-01-01", "basis": None, "url": None}
        assert res["newly_listed"] == 3            # incl. the one whose earlier stated start stays
        assert res["total"] == 3 and res["subsidiaries"] == 4 and res["unmatched"] == 0
        assert res["readable"] == 3 and res["earliest_dated"] == "2024-06-30"

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


class TestTheClaimKeepsItsListingDate:
    """A re-read of the list rewrites the whole claim and states no start. It
    wiped the listing date from 148 of News Corp's claims while their edges kept
    it, and the history run did not put it back because the edge had not moved."""

    def _claim_of(self, it_db, to_id):
        r = it_db.run_sql("SELECT since, since_basis, since_source_url, source_date FROM Claim "
                          "WHERE from_id = 'nc' AND to_id = :t AND source_id = 'sec'", {"t": to_id})[0]
        return {k: r.get(k) for k in ("since", "since_basis", "since_source_url", "source_date")}

    def _scrape(self, to_id, **over):
        """What the Exhibit 21 scrape records for a subsidiary: no start date."""
        from app.claims import KIND_OWNS, record_claim
        record_claim(**{**dict(kind=KIND_OWNS, from_id="nc", to_id=to_id, source_id="sec",
                               filing_type="EX-21", source_date="2026-08-07"), **over})

    def test_a_re_read_that_states_no_start_leaves_the_listing_date(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        it_db.run_command("CREATE (:Entity {id:'nc', name:'News Corp'})")
        it_db.run_command("CREATE (:Entity {id:'dj', name:'Dow Jones'})")
        it_db.run_command("MATCH (a:Entity {id:'nc'}), (b:Entity {id:'dj'}) CREATE (a)-[:OWNS "
                          "{filing_type:'EX-21', source_id:'sec', source_date:'2026-08-07'}]->(b)")
        self._scrape("dj")
        assert self._claim_of(it_db, "dj")["since"] is None
        assert set_since_lower_bound("nc", "dj", "2013-06-30", "https://sec.example.test/2013", "first_listed")
        dated = {"since": "2013-06-30", "since_basis": "first_listed",
                 "since_source_url": "https://sec.example.test/2013"}
        assert {k: v for k, v in self._claim_of(it_db, "dj").items() if k != "source_date"} == dated

        self._scrape("dj", source_date="2027-08-06")                      # next year's list, re-read
        after = self._claim_of(it_db, "dj")
        assert {k: v for k, v in after.items() if k != "source_date"} == dated
        assert after["source_date"] == "2027-08-06"                       # the rest of the claim IS rewritten

    def test_a_write_that_states_a_start_replaces_the_listing_date(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        it_db.run_command("CREATE (:Entity {id:'nc', name:'News Corp'})")
        it_db.run_command("CREATE (:Entity {id:'dj', name:'Dow Jones'})")
        it_db.run_command("MATCH (a:Entity {id:'nc'}), (b:Entity {id:'dj'}) CREATE (a)-[:OWNS "
                          "{filing_type:'EX-21', source_id:'sec'}]->(b)")
        self._scrape("dj")
        set_since_lower_bound("nc", "dj", "2013-06-30", "u", "first_listed")
        self._scrape("dj", since="2010-01-01")                            # the source now states a start
        assert {k: v for k, v in self._claim_of(it_db, "dj").items() if k != "source_date"} == {
            "since": "2010-01-01", "since_basis": None, "since_source_url": None}

    def test_a_stated_start_is_still_cleared_by_a_write_without_one(self, it_db):
        """Only a LISTING date is kept; a stated start the source no longer gives goes, as before."""
        it_db.run_command("CREATE (:Entity {id:'nc', name:'News Corp'})")
        self._scrape("x", since="2019-01-01")
        assert self._claim_of(it_db, "x")["since"] == "2019-01-01"
        self._scrape("x")
        assert self._claim_of(it_db, "x")["since"] is None

    def test_the_history_run_repairs_a_claim_whose_edge_is_already_right(self, it_db):
        from app.scraper.sec_writer import set_since_lower_bound
        it_db.run_command("CREATE (:Entity {id:'nc', name:'News Corp'})")
        it_db.run_command("CREATE (:Entity {id:'dj', name:'Dow Jones'})")
        # the state found on dev: the edge dated, its claim not
        it_db.run_command("MATCH (a:Entity {id:'nc'}), (b:Entity {id:'dj'}) CREATE (a)-[:OWNS "
                          "{filing_type:'EX-21', source_id:'sec', since:'2013-06-30', "
                          "since_basis:'first_listed', since_source_url:'u'}]->(b)")
        self._scrape("dj")
        assert set_since_lower_bound("nc", "dj", "2013-06-30", "u", "first_listed") is False   # edge unchanged
        assert {k: v for k, v in self._claim_of(it_db, "dj").items() if k != "source_date"} == {
            "since": "2013-06-30", "since_basis": "first_listed", "since_source_url": "u"}
