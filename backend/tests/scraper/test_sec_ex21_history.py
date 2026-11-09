"""The subsidiary history: which years' annual lists name a subsidiary, and the
lower bound that follows ("owned since at least")."""
from unittest.mock import patch

from app.scraper import sec_ex21
from app.scraper.sec_ex21 import earliest_listing


def _hist(*years_and_names):
    """History entries newest first: (filing_date, names as the exhibit spells
    them, or None for an unreadable year) — normalised the way the fetcher does."""
    from app.scraper.mapper import normalize_entity_name
    return [{"as_of": d, "filing_date": d, "url": f"https://www.sec.gov/{d}.htm",
             "names": None if names is None else {normalize_entity_name(x) for x in names}}
            for d, names in years_and_names]


class TestEarliestListing:
    def test_the_oldest_filing_of_the_unbroken_run(self):
        h = _hist(("2026-08-07", {"Dow Jones & Company", "HarperCollins"}),
                  ("2025-08-08", {"Dow Jones & Company", "HarperCollins"}),
                  ("2024-08-08", {"Dow Jones & Company"}),
                  ("2023-08-10", {"Dow Jones & Company"}))
        assert earliest_listing(h, "Dow Jones & Company")["filing_date"] == "2023-08-10"
        assert earliest_listing(h, "HarperCollins")["filing_date"] == "2025-08-08"

    def test_a_gap_ends_the_run_even_if_it_is_listed_again_before(self):
        """Filers may omit insignificant subsidiaries, so a gap proves nothing —
        stopping there keeps the result a TRUE lower bound."""
        h = _hist(("2026-08-07", {"Storyful"}), ("2025-08-08", {"Storyful"}),
                  ("2024-08-08", {"Other Co"}),                        # missing this year
                  ("2023-08-10", {"Storyful"}))
        assert earliest_listing(h, "Storyful")["filing_date"] == "2025-08-08"

    def test_an_unreadable_year_is_a_gap(self):
        h = _hist(("2026-08-07", {"Storyful"}), ("2025-08-08", None), ("2024-08-08", {"Storyful"}))
        assert earliest_listing(h, "Storyful")["filing_date"] == "2026-08-07"

    def test_not_in_the_newest_list_means_nothing(self):
        h = _hist(("2026-08-07", {"Other Co"}), ("2025-08-08", {"Storyful"}))
        assert earliest_listing(h, "Storyful") is None
        assert earliest_listing([], "Storyful") is None

    def test_names_match_across_spellings_through_normalisation(self):
        h = _hist(("2026-08-07", {"Dow Jones & Company, Inc."}), ("2019-08-13", {"Dow Jones & Company, Inc."}))
        assert earliest_listing(h, "DOW JONES & COMPANY INC")["filing_date"] == "2019-08-13"

    def test_the_bound_is_the_fiscal_year_end_the_list_describes(self):
        """A December-year company files in the NEXT calendar year: its FY2025
        list, filed 2026-02, proves the subsidiary was held at 2025-12-31."""
        h = [{"as_of": "2025-12-31", "filing_date": "2026-02-20", "url": "u1", "names": {"storyful"}},
             {"as_of": "2024-12-31", "filing_date": "2025-02-21", "url": "u2", "names": {"storyful"}}]
        got = earliest_listing(h, "Storyful")
        assert got["as_of"] == "2024-12-31" and got["filing_date"] == "2025-02-21"

    def test_newly_listed_when_the_list_before_was_read_and_no_older_one_names_it(self):
        h = _hist(("2026-08-07", {"Storyful", "Dow Jones"}), ("2025-08-08", {"Storyful", "Dow Jones"}),
                  ("2024-08-08", {"Dow Jones"}), ("2023-08-10", {"Dow Jones"}))
        got = earliest_listing(h, "Storyful")
        assert (got["filing_date"], got["basis"]) == ("2025-08-08", "newly_listed")

    def test_first_listed_when_the_history_cannot_say_what_was_before(self):
        # the run reaches the oldest list: nothing older to check
        h = _hist(("2026-08-07", {"Dow Jones"}), ("2025-08-08", {"Dow Jones"}))
        assert earliest_listing(h, "Dow Jones")["basis"] == "first_listed"
        # the list before could not be read
        h = _hist(("2026-08-07", {"Storyful"}), ("2025-08-08", None), ("2024-08-08", {"Other Co"}))
        assert earliest_listing(h, "Storyful")["basis"] == "first_listed"
        # dropped and back: an OLDER list names it, so the gap was not a start
        h = _hist(("2026-08-07", {"Storyful"}), ("2025-08-08", {"Storyful"}),
                  ("2024-08-08", {"Other Co"}), ("2023-08-10", None), ("2022-08-10", {"Storyful"}))
        got = earliest_listing(h, "Storyful")
        assert (got["filing_date"], got["basis"]) == ("2025-08-08", "first_listed")

    def test_the_proof_url_comes_with_the_date(self):
        h = _hist(("2026-08-07", {"Storyful"}), ("2020-08-11", {"Storyful"}))
        assert earliest_listing(h, "Storyful")["url"] == "https://www.sec.gov/2020-08-11.htm"


class TestAnnualFilings:
    SUBS = {"filings": {"recent": {"form": ["10-K", "8-K", "10-K/A", "10-K"],
                                   "accessionNumber": ["a1", "a2", "a3", "a4"],
                                   "filingDate": ["2026-08-07", "2026-05-01", "2025-09-01", "2025-08-08"],
                                   "reportDate": ["2026-06-30", "", "2025-06-30", "2025-06-30"]},
                        "files": [{"name": "CIK0001564708-submissions-001.json"}]}}
    OLDER = {"form": ["10-K", "4"], "accessionNumber": ["a5", "a6"],
             "filingDate": ["2014-08-14", "2014-02-01"]}          # an old page without reportDate

    def _get(self, url):
        return self.OLDER if url.endswith("-001.json") else self.SUBS

    def test_newest_first_annual_forms_only(self):
        with patch.object(sec_ex21, "_get", side_effect=self._get):
            got = sec_ex21.annual_filings("1564708")
        assert [a for _, a, _, _ in got] == ["a1", "a4"]         # 10-K/A and 8-K left out
        assert got[0][3] == "2026-06-30"                          # the fiscal year-end it covers

    def test_older_pages_only_when_asked(self):
        with patch.object(sec_ex21, "_get", side_effect=self._get):
            got = sec_ex21.annual_filings("1564708", include_older=True)
        assert [a for _, a, _, _ in got] == ["a1", "a4", "a5"]
        assert got[2][3] == ""                                    # no report date given → empty

    def test_a_dead_cik_is_no_filings(self):
        with patch.object(sec_ex21, "_get", side_effect=RuntimeError("404")):
            assert sec_ex21.annual_filings("1") == []


class TestFetchHistory:
    def test_one_entry_per_filing_and_an_unreadable_one_marked(self):
        filings = [("10-K", "a1", "2026-08-07", "2026-06-30"), ("10-K", "a2", "2025-08-08", "2025-06-30"),
                   ("10-K", "a3", "2002-09-01", "")]

        def candidates(cik, form, acc, filed, period=""):
            return [] if acc == "a3" else [{"url": f"https://x/{acc}.htm", "form": form,
                                            "filing_date": filed, "accession": acc}]
        with patch.object(sec_ex21, "annual_filings", return_value=filings), \
             patch.object(sec_ex21, "exhibit_candidates", side_effect=candidates), \
             patch.object(sec_ex21, "_get_text", return_value="<html/>"), \
             patch.object(sec_ex21, "parse_exhibit",
                          return_value=[{"name": "Dow Jones & Company, Inc.", "jurisdiction": "Delaware"}]):
            h = sec_ex21.fetch_subsidiary_history("1564708")
        assert [e["filing_date"] for e in h] == ["2026-08-07", "2025-08-08", "2002-09-01"]
        # as_of = the fiscal year-end the list describes; the filing date without one
        assert [e["as_of"] for e in h] == ["2026-06-30", "2025-06-30", "2002-09-01"]
        assert h[0]["names"] and h[2]["names"] is None and h[2]["url"] is None

    def test_max_filings_caps_the_walk(self):
        filings = [("10-K", f"a{i}", f"20{26 - i:02d}-08-01", f"20{26 - i:02d}-06-30") for i in range(10)]
        with patch.object(sec_ex21, "annual_filings", return_value=filings), \
             patch.object(sec_ex21, "exhibit_candidates", return_value=[]) as cand:
            h = sec_ex21.fetch_subsidiary_history("1", max_filings=3)
        assert len(h) == 3 and cand.call_count == 3


class TestTheHistoryReachesTheRegistrationStatement:
    """Findings item 79: the walk continues from the oldest annual list into
    the S-1 / F-1 / Form 10 filed before it — Tesla's S-1 of 2010-06 is older
    than its first 10-K's year-end — dated by the filing date, newest first,
    at most REGISTRATION_MAX_FILINGS; a registration statement filed AFTER an
    annual report (a follow-on offering) is not walked."""
    ANNUAL = [("10-K", "a-2012", "2012-02-27", "2011-12-31"), ("10-K", "a-2011", "2011-03-03", "2010-12-31")]
    REGS = [("S-1/A", "r-3", "2010-06-28", ""), ("S-1", "r-1", "2010-01-29", ""),
            ("S-1", "follow-on", "2011-06-01", "")]                       # after the first 10-K: a follow-on

    @staticmethod
    def _parse(candidates, cik=None):
        meta = candidates[0]
        names = {"a-2012": ["Tesla Motors Netherlands B.V.", "SolarCity Corp"], "a-2011": ["Tesla Motors Netherlands B.V."],
                 "r-3": ["Tesla Motors Netherlands B.V."], "r-1": None}.get(meta["accession"], ["Tesla Motors Netherlands B.V."])
        if names is None:
            return None
        return [{"name": n} for n in names], {"filing_date": meta["filed"], "url": f"https://sec.gov/{meta['accession']}.htm"}

    def _history(self, regs=None, annual=None):
        with patch.object(sec_ex21, "annual_filings", return_value=self.ANNUAL if annual is None else annual), \
             patch.object(sec_ex21, "registration_filings", return_value=self.REGS if regs is None else regs), \
             patch.object(sec_ex21, "exhibit_candidates",
                          side_effect=lambda cik, form, acc, filed, period=None: [{"accession": acc, "filed": filed, "form": form, "url": "u"}]), \
             patch.object(sec_ex21, "_parse_first", side_effect=self._parse):
            return sec_ex21.fetch_subsidiary_history("0001318605")

    def test_the_registration_lists_follow_the_annual_ones_dated_by_their_filing_day(self):
        h = self._history()
        assert [(e["form"], e["as_of"]) for e in h] == [
            ("10-K", "2011-12-31"), ("10-K", "2010-12-31"), ("S-1/A", "2010-06-28")]
        assert h[2]["names"] == {"tesla motors netherlands"} and h[2]["url"] == "https://sec.gov/r-3.htm"
        # the bound reaches the S-1/A; SolarCity, listed only in 2012, stays at that year-end
        assert earliest_listing(h, "Tesla Motors Netherlands B.V.")["as_of"] == "2010-06-28"
        assert earliest_listing(h, "SolarCity Corp")["as_of"] == "2011-12-31"

    def test_an_amendment_without_a_list_is_left_out_not_a_gap(self):
        # Tesla's five S-1/As of June 2010 re-file no Exhibit 21; the S-1/A of 2010-05-27
        # does. Walked newest first, a "gap" for each amendment would end the run
        # before the list — the guard of 2026-11-09 moved 0 bounds that way.
        regs = [("S-1/A", "r-1", "2010-06-28", ""), ("S-1/A", "r-1", "2010-06-15", ""), ("S-1/A", "r-3", "2010-05-27", "")]
        h = self._history(regs=regs)
        assert [(e["form"], e["as_of"]) for e in h] == [("10-K", "2011-12-31"), ("10-K", "2010-12-31"), ("S-1/A", "2010-05-27")]
        assert earliest_listing(h, "Tesla Motors Netherlands B.V.")["as_of"] == "2010-05-27"
        # an annual filing whose exhibit cannot be read is still a gap
        annual = self.ANNUAL + [("10-K", "r-1", "2010-03-01", "2009-12-31")]
        h = self._history(annual=annual, regs=[])
        assert h[2]["names"] is None and earliest_listing(h, "Tesla Motors Netherlands B.V.")["as_of"] == "2010-12-31"

    def test_a_registration_statement_filed_after_an_annual_report_is_not_walked(self):
        assert "follow-on" not in [e["url"] or "" for e in self._history()] and all(
            e["form"] == "10-K" or e["as_of"] < "2011-03-03" for e in self._history())

    def test_without_an_annual_filing_nothing_is_read_here(self):
        # a company with no 10-K yet is `run_sec_ex21`'s registration path (item 77), not the history's
        assert self._history(annual=[]) == []

    def test_the_registration_walk_is_capped(self):
        regs = [("S-1/A", f"x-{i}", f"2010-0{9 - i}-01", "") for i in range(1, 9)]       # eight readable, newest first
        with patch.object(sec_ex21, "REGISTRATION_MAX_FILINGS", 2):
            h = self._history(regs=regs)
        assert [e["form"] for e in h].count("S-1/A") == 2
