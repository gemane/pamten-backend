"""Calendar-week arithmetic and the digest's rendering — pure, no database."""
from datetime import datetime, timezone

import pytest

from app.weekly import iso_week, previous_week, week_bounds, week_label
from app.weekly_report import format_report_html, format_report_text


class TestWeeks:
    def test_iso_week_id(self):
        assert iso_week(datetime(2026, 9, 16, tzinfo=timezone.utc)) == "2026-W38"
        # ISO year boundary: 29 Dec 2025 is Monday of 2026-W01
        assert iso_week(datetime(2025, 12, 29, tzinfo=timezone.utc)) == "2026-W01"

    def test_bounds_are_monday_to_monday_utc(self):
        start, end = week_bounds("2026-W38")
        assert start == datetime(2026, 9, 14, tzinfo=timezone.utc)
        assert end == datetime(2026, 9, 21, tzinfo=timezone.utc)

    def test_previous_week_is_the_last_completed_one(self):
        # A Monday-morning run reports on the week that ended Sunday night.
        assert previous_week(datetime(2026, 9, 21, 6, tzinfo=timezone.utc)) == "2026-W38"
        # …and a mid-week run still reports on the last COMPLETED week.
        assert previous_week(datetime(2026, 9, 24, tzinfo=timezone.utc)) == "2026-W38"

    def test_label(self):
        assert week_label("2026-W38") == "week 38 (14–20 Sep 2026)"
        assert week_label("2026-W40") == "week 40 (28 Sep – 04 Oct 2026)"

    def test_bad_ids_are_refused(self):
        with pytest.raises(ValueError):
            week_bounds("2026-38")


REPORT = {
    "week": "2026-W38", "label": "week 38 (14–20 Sep 2026)",
    "from": "2026-09-14", "to": "2026-09-20", "generated_at": "2026-09-21T06:00:00+00:00",
    "searches": {"total": 12, "distinct_queries": 3, "zero_results": 2, "selected": 9,
                 "top": [{"query": "SpaceX", "country": None, "searches": 8, "zero_results": 0},
                         {"query": "Alphabet", "country": "FR", "searches": 2, "zero_results": 2}],
                 "usage": {"export.csv": 1}},
    "scrapes": {"runs": 5, "by_source": {"wikidata": {"ok": 3}, "sec-13f": {"ok": 1, "failed": 1}},
                "records_written": 301,
                "first_scrapes": [{"entity_id": "e1", "target": "Gigafund", "records": 4, "name": "Gigafund Management Company, LLC"}],
                "refreshed": [{"entity_id": "e2", "target": "SpaceX", "records": 292, "name": "SPACE EXPLORATION TECHNOLOGIES CORP."}],
                "failures": [{"source": "sec-13f", "target": "SpaceX", "error": "500 from efts"}],
                "sec_enrichments": {"sec-13f": [{"target": "SpaceX", "total": 292}]}},
    "imports": {"gleif-update": {"runs": 7, "ok": 7, "failed": 0, "skipped": 0, "records": 184}},
    "graph": {"totals": {"companies": 6773, "people": 385, "relationships": 7638, "roles": 440, "claims": 8198},
              "new_relationships": {"owns": 12}, "since": "2026-W37",
              "delta": {"companies": 5, "people": -1, "relationships": 300, "roles": 9, "claims": 12}},
}


class TestFailures:
    def test_errors_are_trimmed_to_their_first_clause(self):
        from app.weekly_report import _short_error
        assert _short_error("Server error '500 Internal Server Error' for url 'https://efts.sec.gov/LATEST/x'") \
            == "Server error '500 Internal Server Error'"
        assert _short_error("The read operation timed out") == "The read operation timed out"
        assert _short_error("first line\nsecond line") == "first line"

    def test_identical_failures_collapse_with_a_count(self):
        from app.weekly_report import _collapse_failures
        f = {"source": "sec-13f", "target": "SpaceX", "error": "500"}
        g = {"source": "wikidata", "target": "Acme", "error": "timeout"}
        out = _collapse_failures([f, f, g, f])
        assert out == [{**f, "count": 3}, {**g, "count": 1}], "most frequent first"

    def test_the_renderers_show_the_count(self):
        r = {**REPORT, "scrapes": {**REPORT["scrapes"],
             "failures": [{"source": "sec-13f", "target": "SpaceX", "error": "500", "count": 3}]}}
        assert "failures (3):" in format_report_text(r) and "sec-13f SpaceX: 500  ×3" in format_report_text(r)
        h = format_report_html(r)
        assert "failures (3)" in h and "×3" in h


class TestRendering:
    def test_text_carries_every_section_and_the_numbers(self):
        t = format_report_text(REPORT)
        assert t.startswith("Owlgraph Report — week 38 (14–20 Sep 2026)")
        assert "Internal; not for redistribution." in t
        assert "12 searches for 3 distinct queries; 2 found nothing; 9 led to a result" in t
        assert "   8  SpaceX" in t and "Alphabet [FR]  (no result ×2)" in t
        assert "scraped for the first time (1):" in t and "    Gigafund Management Company, LLC\n" in t
        assert "refreshed (1):" in t and "    SPACE EXPLORATION TECHNOLOGIES CORP.\n" in t
        assert "(4 records)" not in t and "(292 records)" not in t, "names only — the counts stay in the app"
        assert "sec-13f SpaceX: 500 from efts" in t
        assert "gleif-update: 7 runs" in t
        assert "companies           6,773 (+5)" in t and "people                385 (-1)" in t
        assert "first asserted this week: owns 12" in t

    def test_a_first_week_has_no_delta(self):
        r = {**REPORT, "graph": {**REPORT["graph"], "since": None, "delta": None}}
        t = format_report_text(r)
        assert "GRAPH\n" in t and "(+" not in t

    def test_html_is_a_laid_out_email_with_names_only(self):
        r = {**REPORT, "scrapes": {**REPORT["scrapes"],
                                   "failures": [{"source": "x", "target": "<b>", "error": "a<b"}]}}
        h = format_report_html(r)
        assert h.startswith("<html>") and "<pre" not in h, "a layout, not a text dump"
        # headline numbers, then one card per section
        assert ">12<" in h and ">searches<" in h and ">5<" in h and ">scrape runs<" in h
        for heading in ("Searches", "Scrapes", "Imports", "Graph"):
            assert f">{heading}<" in h or f">{heading} " in h
        assert "Gigafund Management Company, LLC" in h and "SPACE EXPLORATION TECHNOLOGIES CORP." in h
        assert "292" not in h.split("Refreshed")[1].split("</ul>")[0], "no per-company counts"
        assert "6,773" in h and "(+5)" in h and "(-1)" in h
        # user content is escaped, nothing is executable
        assert "&lt;b&gt;" in h and "a&lt;b" in h and "<script" not in h
        assert "Owlgraph Report — week 38" in h and "2026-09-14 to 2026-09-20" in h
        assert "Internal; not for redistribution." in h

    def test_the_footer_names_the_recipient_when_sent(self):
        h = format_report_html({**REPORT, "recipient": "ops@example.com"})
        assert "sent to ops@example.com" in h
        assert "sent to" not in format_report_html(REPORT), "no recipient when only printed"
