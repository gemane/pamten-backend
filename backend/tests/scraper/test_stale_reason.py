"""Why an edge is dimmed, and since when: every stale mark carries its
reason and evidence date, every clearing drops them, and the history hands
them to the timeline so a reader can see why a holding went grey."""
from app.scraper.edge_schema import OWNS_PROPS, STALE_CLEAR, STALE_CLEAR_CYPHER, STALE_REASONS


class TestTheVocabulary:
    def test_the_three_fields_are_schema_fields_and_the_clear_names_them_all(self):
        for f in ("stale_since", "stale_reason", "stale_source_url"):
            assert f in OWNS_PROPS
            assert f in STALE_CLEAR and f"r.{f} = null" in STALE_CLEAR_CYPHER
        assert STALE_CLEAR["stale"] is False and "r.stale = false" in STALE_CLEAR_CYPHER

    def test_the_six_reasons(self):
        assert set(STALE_REASONS) == {"13f_not_confirmed", "ex21_not_listed", "unconfirmed",
                                      "lei_lapsed", "not_restated", "final_report"}

    def test_the_sql_clear_names_the_same_four_fields(self):
        from app.scraper.edge_schema import STALE_CLEAR_SQL
        for f in STALE_CLEAR:
            assert f"{f} = " in STALE_CLEAR_SQL


class TestTheRestatementWindow:
    """A Schedule 13G is restated every February while it lasts, a 13D only on
    a material change; a statement older than its window no longer speaks
    for the holding — dimmed, never closed."""
    def setup_method(self):
        from app.scraper.maintenance import RESTATEMENT_WINDOW_DAYS, restated, restatement_family
        self.restated, self.family, self.windows = restated, restatement_family, RESTATEMENT_WINDOW_DAYS

    def test_the_families(self):
        assert self.family("13G") == "13G" and self.family("13G/A") == "13G"
        assert self.family("13D/A") == "13D"
        assert self.family("EX-21") is None and self.family("13F") is None
        assert self.family("Form 4") is None and self.family(None) is None

    def test_a_13g_speaks_for_fifteen_months(self):
        assert self.windows["13G"] == 455
        assert self.restated("13G/A", "2025-11-14", "2026-10-25") is True
        assert self.restated("13G/A", "2014-02-14", "2026-10-25") is False
        assert self.restated("13G", "2025-07-27", "2026-10-25") is True     # 455 days
        assert self.restated("13G", "2025-07-26", "2026-10-25") is False

    def test_a_13d_speaks_for_five_years(self):
        assert self.windows["13D"] == 1826
        assert self.restated("13D/A", "2022-01-01", "2026-10-25") is True
        assert self.restated("13D", "2009-04-30", "2026-10-25") is False

    def test_other_records_and_undated_ones_are_not_judged(self):
        assert self.restated("EX-21", "2014-02-14", "2026-10-25") is None
        assert self.restated("13G", None, "2026-10-25") is None
        assert self.restated("13G", "2014", "2026-10-25") is None


class TestTheWriters:
    def test_a_newer_exhibit_dims_with_the_list_it_is_missing_from(self, fake_db):
        from app.scraper.sec_writer import mark_ex21_stale
        fake_db.queue([{"n": 3}])
        url = "https://www.sec.gov/Archives/edgar/data/1234/000123/ex21.htm"
        assert mark_ex21_stale(["e1"], url, "2025-12-31") == 3
        cypher, params = fake_db.calls[0]
        assert "r.stale_reason = 'ex21_not_listed'" in cypher
        assert "r.stale_since = $as_of" in cypher and "r.stale_source_url = $url" in cypher
        assert params["as_of"] == "2025-12-31" and params["url"] == url

    def test_a_sec_refresh_clears_the_reason_with_the_mark(self):
        import inspect
        from app.scraper import sec_writer
        src = inspect.getsource(sec_writer)
        # no bare `stale=False` / `r.stale = false` survives: every clearing goes
        # through STALE_CLEAR, which drops the reason and date too
        assert "stale=False)" not in src.replace("**STALE_CLEAR)", "")
        assert "r.stale = false" not in src

    def test_the_community_pass_clears_the_reason_with_the_mark(self):
        import inspect
        from app.scraper import maintenance
        src = inspect.getsource(maintenance.mark_stale_ownership)
        assert "STALE_CLEAR_CYPHER" in src and "r.stale = false" not in src

    def test_the_gleif_delta_clears_the_reason_with_the_mark(self):
        import inspect
        from app.scraper import gleif_incremental
        src = inspect.getsource(gleif_incremental)
        assert 'sets["stale"] = False' not in src and "STALE_CLEAR" in src
        assert 'stale_reason="lei_lapsed"' in src


class TestTheMergeAndTheHistory:
    def test_a_merge_that_un_dims_drops_the_reason(self):
        from app.scraper.owns_merge import fold
        stale = {"source_id": "g", "credibility_score": 92, "stale": True, "stale_reason": "lei_lapsed",
                 "stale_since": "2023-06-02", "stale_source_url": None}
        fresh = {"source_id": "s", "credibility_score": 98, "stale": False}
        out = fold([stale, fresh], stale)
        assert out["stale"] is False
        assert out["stale_reason"] is None and out["stale_since"] is None and out["stale_source_url"] is None

    def test_a_real_acquisition_is_marked_as_such_with_its_price(self):
        """The SEC acquisition reader's closing date is the one start a source
        STATES as a purchase; the timeline may say "Acquired" for it."""
        from app.routers.relationships import _start
        deal = {"filing_type": "10-Q XBRL", "since": "2026-03-11", "since_basis": None,
                "value_usd": 2.95e10, "since_source_url": "https://www.sec.gov/Archives/edgar/data/1/0002/goog-20260331_htm.xml"}
        assert _start(deal) == {"start_kind": "acquired", "value_usd": 2.95e10,
                                "since_source_url": deal["since_source_url"]}
        # an Exhibit 21 edge the reader enriched: its own filing type, the deal's date and instance
        enriched = {"filing_type": "EX-21", "since": "2026-03-11", "since_basis": None, "value_usd": 5.9e9,
                    "since_source_url": "https://www.sec.gov/Archives/edgar/data/1/0003/goog-20260630_htm.xml"}
        assert _start(enriched)["start_kind"] == "acquired"
        # a 13F holding carries a market value, not a price — never "acquired"
        assert _start({"filing_type": "13F", "since": "2020-03-31", "since_basis": "first_reported",
                       "value_usd": 1.2e9}) == {}
        # a list's lower bound, a register's start: a holding existed by then
        assert _start({"filing_type": "EX-21", "since": "2014-12-31", "since_basis": "first_listed"}) == {}
        assert _start({"filing_type": "RR", "since": "2015-01-01", "since_basis": None, "since_source_url": None}) == {}

    def test_an_ended_holding_carries_how_the_source_says_it_ended(self):
        from app.routers.relationships import _end
        assert _end({"until": "2024-02-13", "until_reason": None, "filing_type": "13G/A",
                     "source_url": "https://www.sec.gov/Archives/edgar/data/1/0001/x.xml"}) == {
            "until_reason": None, "filing_type": "13G/A",
            "source_url": "https://www.sec.gov/Archives/edgar/data/1/0001/x.xml"}
        assert _end({"until": "2021-03-31", "until_reason": "ceased", "filing_type": "PSC"})["until_reason"] == "ceased"
        assert _end({"until": None, "until_reason": "ceased"}) == {}       # not ended: nothing to say

    def test_the_history_hands_the_timeline_why_and_since_when(self):
        from app.routers.relationships import _staleness
        r = {"stale": True, "stale_since": "2026-06-30", "stale_reason": "13f_not_confirmed",
             "stale_source_url": None, "filing_type": "13F", "source_date": "2026-05-15"}
        assert _staleness(r) == {"stale": True, "stale_since": "2026-06-30",
                                 "stale_reason": "13f_not_confirmed", "stale_source_url": None,
                                 "filing_type": "13F", "source_date": "2026-05-15"}
        assert _staleness({"stale": False, "stale_reason": "old"}) == {}
        assert _staleness({}) == {}
