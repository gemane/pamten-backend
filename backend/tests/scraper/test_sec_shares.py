"""Shares outstanding (app/scraper/sec_shares.py): the denominator is dated and per class.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""
from unittest.mock import patch




class TestTheDenominatorIsDatedAndPerClass:
    # the plain-tag series Berkshire stopped keeping in 2011
    OLD = {"units": {"shares": [{"end": "2010-07-30", "val": 976414}, {"end": "2011-04-29", "val": 941481}]}}
    FRESH = {"units": {"shares": [{"end": "2026-06-30", "val": 15_000_000_000}]}}

    def test_a_series_that_stopped_is_not_the_current_count(self):
        from app.scraper import sec_client, sec_shares
        cover = sec_shares.SharesOutstanding(1_408_523_611.0, "2026-07-29", {"A": 488_450.0, "B": 1_408_035_161.0})
        with patch.object(sec_client, "_get", return_value=self.OLD), \
             patch.object(sec_shares, "_shares_outstanding_from_cover", return_value=cover) as fromcover:
            d = sec_shares.fetch_shares_outstanding_detail("0001067983", on="2026-10-10")
            total = sec_shares.fetch_shares_outstanding("0001067983", on="2026-10-10")
        assert d == cover and fromcover.called
        assert total == 1_408_523_611.0                      # the 13F path's bare total: the classes summed

    def test_a_fresh_series_is_taken_as_it_was(self):
        from app.scraper import sec_client, sec_shares
        with patch.object(sec_client, "_get", return_value=self.FRESH), \
             patch.object(sec_shares, "_shares_outstanding_from_cover") as fromcover:
            d = sec_shares.fetch_shares_outstanding_detail("0000320193", on="2026-10-10")
        assert d == sec_shares.SharesOutstanding(15_000_000_000.0, "2026-06-30", {}) and not fromcover.called

    def test_the_floor_is_fifteen_months_before_the_day_asked_for(self):
        from app.scraper import sec_client, sec_shares
        assert sec_shares._freshness_floor("2026-10-10") == "2025-07-17"
        # the same 2011 value IS current when asked about from 2011
        with patch.object(sec_client, "_get", return_value=self.OLD), \
             patch.object(sec_shares, "_shares_outstanding_from_cover") as fromcover:
            d = sec_shares.fetch_shares_outstanding_detail("0001067983", on="2011-08-01")
        assert d.total == 941481.0 and not fromcover.called

    def test_the_cover_tells_the_classes_apart_by_their_contexts(self):
        from app.scraper.sec_shares import cover_counts
        html = """<xbrli:context id="C_a"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001067983</xbrli:identifier>
          <xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember></xbrli:segment>
          </xbrli:entity><xbrli:period><xbrli:instant>2026-07-29</xbrli:instant></xbrli:period></xbrli:context>
          <xbrli:context id="C_b"><xbrli:entity><xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassBMember</xbrldi:explicitMember></xbrli:segment>
          </xbrli:entity><xbrli:period><xbrli:instant>2026-07-29</xbrli:instant></xbrli:period></xbrli:context>"""
        d = cover_counts({"C_a": 488_450.0, "C_b": 1_408_035_161.0}, html)
        assert d.by_class == {"A": 488_450.0, "B": 1_408_035_161.0}
        assert d.total == 1_408_523_611.0 and d.as_of == "2026-07-29"

    def test_a_single_class_cover_is_an_unclassed_count(self):
        from app.scraper.sec_shares import cover_counts
        html = '<context id="c1"><entity><identifier>1</identifier></entity><period><instant>2026-03-31</instant></period></context>'
        d = cover_counts({"c1": 15_000_000_000.0}, html)
        assert d.by_class == {} and d.total == 15_000_000_000.0 and d.as_of == "2026-03-31"
        assert cover_counts({}, html) is None

    # Trimmed from Toast's 10-Q for 2026-06-30 (tost-20260630.htm): the cover
    # states millions, `scale="6"`.
    TOAST = (
        '<xbrli:context id="c-2"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001650164</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassAMember</xbrldi:explicitMember>'
        '</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-07-30</xbrli:instant></xbrli:period></xbrli:context>'
        '<xbrli:context id="c-3"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0001650164</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonClassBMember</xbrldi:explicitMember>'
        '</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-07-30</xbrli:instant></xbrli:period></xbrli:context>'
        '<td><ix:nonFraction unitRef="shares" contextRef="c-2" decimals="-6" name="dei:EntityCommonStockSharesOutstanding" scale="6" id="f-24">514</ix:nonFraction></td>'
        '<td><ix:nonFraction unitRef="shares" contextRef="c-3" decimals="-6" name="dei:EntityCommonStockSharesOutstanding" scale="6" id="f-25">64</ix:nonFraction></td>')

    def test_a_scaled_cover_figure_is_multiplied_out(self):
        """Read as written, a Toast director's 3,566 shares were 693.77 % of
        Class A, the database refused the edge and the whole scrape failed."""
        from app.scraper.sec_insiders import stake_for
        from app.scraper.sec_shares import cover_counts, cover_facts
        assert cover_facts(self.TOAST) == {"c-2": 514_000_000.0, "c-3": 64_000_000.0}
        d = cover_counts(cover_facts(self.TOAST), self.TOAST)
        assert d.by_class == {"A": 514_000_000.0, "B": 64_000_000.0} and d.as_of == "2026-07-30"
        pick = stake_for({"Class A Common Stock": 3566.0}, d)
        assert pick["stake_percent"] == 0.0007 and pick["shares_outstanding"] == 514_000_000.0

    def test_more_shares_than_the_class_has_is_no_percentage(self):
        from app.scraper.sec_13dg_text import _pct_of
        from app.scraper.sec_insiders import stake_for
        assert _pct_of(3566.0, 514.0) is None
        assert _pct_of(514.0, 514.0) == 100.0
        pick = stake_for({"Class A Common Stock": 3566.0}, {"total": 578.0, "as_of": "2026-07-30",
                                                             "by_class": {"A": 514.0, "B": 64.0}})
        assert pick["stake_percent"] is None and pick["shares_owned"] == 3566.0

    def test_an_unscaled_or_decimal_figure_reads_as_written(self):
        from app.scraper.sec_shares import cover_facts
        tag = '<ix:nonFraction contextRef="c1" name="dei:EntityCommonStockSharesOutstanding"{}>{}</ix:nonFraction>'
        assert cover_facts(tag.format("", "1,408,035,161")) == {"c1": 1_408_035_161.0}
        assert cover_facts(tag.format(' scale="9"', "13.18")) == {"c1": 13_180_000_000.0}
        assert cover_facts(tag.format(' scale="0"', "941,481")) == {"c1": 941_481.0}
