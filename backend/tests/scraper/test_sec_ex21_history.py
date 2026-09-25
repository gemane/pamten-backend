"""The subsidiary history: which years' annual lists name a subsidiary, and the
lower bound that follows ("owned since at least")."""
from unittest.mock import patch

from app.scraper import sec_ex21
from app.scraper.sec_ex21 import earliest_listing


def _hist(*years_and_names):
    """History entries newest first: (filing_date, names as the exhibit spells
    them, or None for an unreadable year) — normalised the way the fetcher does."""
    from app.scraper.mapper import normalize_entity_name
    return [{"filing_date": d, "url": f"https://www.sec.gov/{d}.htm",
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

    def test_the_proof_url_comes_with_the_date(self):
        h = _hist(("2026-08-07", {"Storyful"}), ("2020-08-11", {"Storyful"}))
        assert earliest_listing(h, "Storyful")["url"] == "https://www.sec.gov/2020-08-11.htm"


class TestAnnualFilings:
    SUBS = {"filings": {"recent": {"form": ["10-K", "8-K", "10-K/A", "10-K"],
                                   "accessionNumber": ["a1", "a2", "a3", "a4"],
                                   "filingDate": ["2026-08-07", "2026-05-01", "2025-09-01", "2025-08-08"]},
                        "files": [{"name": "CIK0001564708-submissions-001.json"}]}}
    OLDER = {"form": ["10-K", "4"], "accessionNumber": ["a5", "a6"],
             "filingDate": ["2014-08-14", "2014-02-01"]}

    def _get(self, url):
        return self.OLDER if url.endswith("-001.json") else self.SUBS

    def test_newest_first_annual_forms_only(self):
        with patch.object(sec_ex21, "_get", side_effect=self._get):
            got = sec_ex21.annual_filings("1564708")
        assert [a for _, a, _ in got] == ["a1", "a4"]            # 10-K/A and 8-K left out

    def test_older_pages_only_when_asked(self):
        with patch.object(sec_ex21, "_get", side_effect=self._get):
            got = sec_ex21.annual_filings("1564708", include_older=True)
        assert [a for _, a, _ in got] == ["a1", "a4", "a5"]

    def test_a_dead_cik_is_no_filings(self):
        with patch.object(sec_ex21, "_get", side_effect=RuntimeError("404")):
            assert sec_ex21.annual_filings("1") == []


class TestFetchHistory:
    def test_one_entry_per_filing_and_an_unreadable_one_marked(self):
        filings = [("10-K", "a1", "2026-08-07"), ("10-K", "a2", "2025-08-08"), ("10-K", "a3", "2002-09-01")]

        def candidates(cik, form, acc, filed):
            return [] if acc == "a3" else [{"url": f"https://x/{acc}.htm", "form": form,
                                            "filing_date": filed, "accession": acc}]
        with patch.object(sec_ex21, "annual_filings", return_value=filings), \
             patch.object(sec_ex21, "exhibit_candidates", side_effect=candidates), \
             patch.object(sec_ex21, "_get_text", return_value="<html/>"), \
             patch.object(sec_ex21, "parse_exhibit",
                          return_value=[{"name": "Dow Jones & Company, Inc.", "jurisdiction": "Delaware"}]):
            h = sec_ex21.fetch_subsidiary_history("1564708")
        assert [e["filing_date"] for e in h] == ["2026-08-07", "2025-08-08", "2002-09-01"]
        assert h[0]["names"] and h[2]["names"] is None and h[2]["url"] is None

    def test_max_filings_caps_the_walk(self):
        filings = [("10-K", f"a{i}", f"20{26 - i:02d}-08-01") for i in range(10)]
        with patch.object(sec_ex21, "annual_filings", return_value=filings), \
             patch.object(sec_ex21, "exhibit_candidates", return_value=[]) as cand:
            h = sec_ex21.fetch_subsidiary_history("1", max_filings=3)
        assert len(h) == 3 and cand.call_count == 3
