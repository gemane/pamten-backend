"""Why an edge is dimmed, and since when: every stale mark carries its
reason and evidence date, every clearing drops them, and the history hands
them to the timeline so a reader can see why a holding went grey."""
from app.scraper.edge_schema import OWNS_PROPS, STALE_CLEAR, STALE_CLEAR_CYPHER, STALE_REASONS, stale_props


class TestTheVocabulary:
    def test_the_three_fields_are_schema_fields_and_the_clear_names_them_all(self):
        for f in ("stale_since", "stale_reason", "stale_source_url"):
            assert f in OWNS_PROPS
            assert f in STALE_CLEAR and f"r.{f} = null" in STALE_CLEAR_CYPHER
        assert STALE_CLEAR["stale"] is False and "r.stale = false" in STALE_CLEAR_CYPHER

    def test_stale_props_names_a_known_reason_and_trims_the_date(self):
        p = stale_props("13f_not_confirmed", "2026-06-30T00:00:00+00:00")
        assert p == {"stale": True, "stale_since": "2026-06-30", "stale_reason": "13f_not_confirmed",
                     "stale_source_url": None}
        assert stale_props("unconfirmed", None)["stale_since"] is None
        import pytest
        with pytest.raises(AssertionError):
            stale_props("made_up", "2026-01-01")
        assert set(STALE_REASONS) == {"13f_not_confirmed", "ex21_not_listed", "unconfirmed", "lei_lapsed"}


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

    def test_the_history_hands_the_timeline_why_and_since_when(self):
        from app.routers.relationships import _staleness
        r = {"stale": True, "stale_since": "2026-06-30", "stale_reason": "13f_not_confirmed",
             "stale_source_url": None, "filing_type": "13F", "source_date": "2026-05-15"}
        assert _staleness(r) == {"stale": True, "stale_since": "2026-06-30",
                                 "stale_reason": "13f_not_confirmed", "stale_source_url": None,
                                 "filing_type": "13F", "source_date": "2026-05-15"}
        assert _staleness({"stale": False, "stale_reason": "old"}) == {}
        assert _staleness({}) == {}
