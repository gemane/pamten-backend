"""XBRL business combinations: the fixtures are Alphabet's REAL Q1 2026 10-Q
instance and label linkbase (Wiz closed 2026-03-11 for 29.5 bn, Intersect
2026-03-10 for 5.9 bn) and its FY2025 10-K (both deals ANNOUNCED, tagged with
an agreed / expected price in a month-long period), trimmed verbatim. The
hostile cases are edits of those lines: a second axis on the deal's context,
a nil fact, a period longer than a day, a foreign currency."""
from pathlib import Path

import httpx
import pytest
from unittest.mock import patch

from app.scraper.sec_acquisitions import (
    clean_label, fallback_name, fetch_acquisitions, instance_files, parse_instance,
    parse_labels, recent_filings,
)

FIX = Path(__file__).parent / "fixtures"
Q1 = (FIX / "xbrl_alphabet_10q_2026q1.xml").read_text()
Q1_LAB = (FIX / "xbrl_alphabet_10q_2026q1_lab.xml").read_text()
FY25 = (FIX / "xbrl_alphabet_10k_fy2025.xml").read_text()
CIK = "1652044"

WIZ_FACT = ('<us-gaap:BusinessCombinationConsiderationTransferred1 contextRef="c-302" '
            'decimals="-8" id="f-1060" unitRef="usd">29500000000'
            '</us-gaap:BusinessCombinationConsiderationTransferred1>')
WIZ_MEMBER = ('<xbrldi:explicitMember dimension="us-gaap:BusinessAcquisitionAxis">'
              'goog:WizIncMember</xbrldi:explicitMember>')


def _rows(parsed):
    return {a["member"]: a for a in parsed["acquisitions"]}


class TestParseInstance:
    def test_reads_the_two_closings_with_date_price_and_concept(self):
        p = parse_instance(Q1, CIK)
        rows = _rows(p)
        assert rows["goog:WizIncMember"] == {
            "member": "goog:WizIncMember", "closing": "2026-03-11",
            "value_usd": 29_500_000_000.0,
            "concept": "BusinessCombinationConsiderationTransferred1"}
        assert rows["goog:IntersectAcquisitionMember"]["closing"] == "2026-03-10"
        assert rows["goog:IntersectAcquisitionMember"]["value_usd"] == 5_900_000_000.0
        assert p["pending_only"] == []

    def test_cover_facts_name_the_filer_and_the_form(self):
        p = parse_instance(Q1, CIK)
        assert (p["cik"], p["document_type"], p["period_end"]) == ("0001652044", "10-Q", "2026-03-31")

    def test_a_context_with_a_second_axis_is_a_breakdown_not_the_deal(self):
        # The six intangible-class contexts under Wiz are counted, never rows.
        assert parse_instance(Q1, CIK)["multi_axis_contexts"] == 6
        # Put a second axis on the deal's own context: the consideration fact
        # then describes a slice, and Wiz has no closing to read.
        second = ('<xbrldi:explicitMember dimension="us-gaap:FiniteLivedIntangibleAssetsByMajorClassAxis">'
                  'us-gaap:CustomerRelationshipsMember</xbrldi:explicitMember>')
        p = parse_instance(Q1.replace(WIZ_MEMBER, WIZ_MEMBER + second, 1), CIK)
        assert "goog:WizIncMember" not in _rows(p)
        assert p["multi_axis_contexts"] == 7

    def test_a_nil_fact_is_not_a_fact(self):
        nil = ('<us-gaap:BusinessCombinationConsiderationTransferred1 contextRef="c-302" '
               'id="f-1060" unitRef="usd" xsi:nil="true"/>')
        p = parse_instance(Q1.replace(WIZ_FACT, nil), CIK)
        assert "goog:WizIncMember" not in _rows(p)
        assert "goog:WizIncMember" in p["pending_only"]

    def test_another_filers_contexts_are_not_this_filers_deals(self):
        p = parse_instance(Q1, "320193")
        assert p["acquisitions"] == [] and p["pending_only"] == []

    def test_an_announced_deal_is_not_a_closing(self):
        # FY2025 10-K: Wiz tagged with the agreed 32 bn (PaymentsToAcquireBusinessesGross,
        # a March-2025 month), Intersect with an EXPECTED price. Neither closed.
        p = parse_instance(FY25, CIK)
        assert p["acquisitions"] == []
        assert p["pending_only"] == ["goog:IntersectMember", "goog:WizIncMember"]

    def test_a_period_longer_than_a_day_gives_no_closing_date(self):
        p = parse_instance(Q1.replace("<endDate>2026-03-11</endDate>", "<endDate>2026-03-31</endDate>", 1), CIK)
        wiz = _rows(p)["goog:WizIncMember"]
        assert wiz["closing"] is None
        assert wiz["value_usd"] == 29_500_000_000.0, "still a closing, only undated"

    def test_a_consideration_in_another_currency_is_a_closing_without_a_value(self):
        p = parse_instance(Q1.replace('id="f-1060" unitRef="usd"', 'id="f-1060" unitRef="eur"'), CIK)
        wiz = _rows(p)["goog:WizIncMember"]
        assert wiz["closing"] == "2026-03-11" and wiz["value_usd"] is None

    def test_a_dated_fact_beats_an_undated_one_for_the_same_member(self):
        # a second, quarter-long context for Wiz with its own consideration fact
        extra_ctx = ('<context id="c-999"><entity><identifier scheme="http://www.sec.gov/CIK">0001652044'
                     '</identifier><segment>' + WIZ_MEMBER + '</segment></entity>'
                     '<period><startDate>2026-01-01</startDate><endDate>2026-03-31</endDate></period></context>')
        extra_fact = ('<us-gaap:BusinessCombinationConsiderationTransferred1 contextRef="c-999" '
                      'decimals="-8" id="f-9999" unitRef="usd">1</us-gaap:BusinessCombinationConsiderationTransferred1>')
        xml = Q1.replace('<context id="c-302">', extra_ctx + '<context id="c-302">', 1)
        xml = xml.replace(WIZ_FACT, extra_fact + WIZ_FACT)
        wiz = _rows(parse_instance(xml, CIK))["goog:WizIncMember"]
        assert wiz["closing"] == "2026-03-11" and wiz["value_usd"] == 29_500_000_000.0

    def test_unparseable_is_none(self):
        assert parse_instance("not xml at all", CIK) is None


class TestLabels:
    def test_reads_the_standard_label_per_member(self):
        assert parse_labels(Q1_LAB) == {"goog:IntersectAcquisitionMember": "Intersect Acquisition [Member]",
                                        "goog:WizIncMember": "Wiz Inc [Member]"}

    def test_standard_label_beats_terse_label(self):
        # drop the standard label: the terse one ("Wiz") is the fallback
        no_standard = Q1_LAB.replace(
            '<link:label id="lab_goog_WizIncMember_label_en-US" xlink:label="lab_goog_WizIncMember" '
            'xlink:role="http://www.xbrl.org/2003/role/label" xlink:type="resource" xml:lang="en-US">'
            'Wiz Inc [Member]</link:label>', "")
        assert parse_labels(no_standard)["goog:WizIncMember"] == "Wiz"

    def test_unparseable_linkbase_is_empty(self):
        assert parse_labels("<nope") == {}

    @pytest.mark.parametrize("label,expected", [
        ("Wiz Inc [Member]", "Wiz Inc"),
        ("Intersect Acquisition [Member]", "Intersect"),
        ("Mandiant Merger [Member]", "Mandiant"),
        ("Other Acquisitions [Member]", None),
        ("Business Combinations [Member]", None),
        ("Individually Immaterial Acquisitions [Member]", None),
        ("", None), (None, None),
    ])
    def test_clean_label(self, label, expected):
        assert clean_label(label) == expected

    def test_fallback_name_splits_the_qname(self):
        assert fallback_name("goog:WizIncMember") == "Wiz Inc"
        assert fallback_name("goog:IntersectAcquisitionMember") == "Intersect Acquisition"
        assert clean_label(fallback_name("goog:IntersectAcquisitionMember")) == "Intersect"
        assert fallback_name("msft:ActivisionBlizzardIncMember") == "Activision Blizzard Inc"


def _subs(rows):
    return {"filings": {"recent": {
        "form": [r[0] for r in rows], "accessionNumber": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows], "reportDate": [r[3] for r in rows]}}}


def _err(code):
    req = httpx.Request("GET", "http://x.test")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


class TestRecentFilings:
    ROWS = [("8-K", "0001652044-26-000071", "2026-07-30", "2026-07-30"),
            ("10-Q", "0001652044-26-000048", "2026-04-30", "2026-03-31"),
            ("10-K", "0001652044-26-000018", "2026-02-05", "2025-12-31"),
            ("10-Q", "0001652044-25-000091", "2025-10-30", "2025-09-30")]

    def test_annual_and_quarterly_reports_newest_first_other_forms_ignored(self):
        with patch("app.scraper.sec_acquisitions._get", return_value=_subs(self.ROWS)):
            got = recent_filings(CIK)
        assert [f["form"] for f in got] == ["10-Q", "10-K", "10-Q"]
        assert got[0] == {"form": "10-Q", "accession": "0001652044-26-000048",
                          "filing_date": "2026-04-30", "report_date": "2026-03-31"}

    def test_capped_at_max_filings(self):
        with patch("app.scraper.sec_acquisitions._get", return_value=_subs(self.ROWS)):
            assert len(recent_filings(CIK, max_filings=2)) == 2

    def test_unknown_filer_is_empty_but_a_5xx_surfaces(self):
        with patch("app.scraper.sec_acquisitions._get", side_effect=_err(404)):
            assert recent_filings("1") == []
        with patch("app.scraper.sec_acquisitions._get", side_effect=_err(503)), \
             pytest.raises(httpx.HTTPStatusError):
            recent_filings("1")


class TestInstanceFiles:
    def test_finds_instance_and_labels_in_the_filing_index(self):
        index = {"directory": {"item": [{"name": "goog-20260331.htm"}, {"name": "goog-20260331_htm.xml"},
                                        {"name": "goog-20260331_lab.xml"}, {"name": "R1.htm"}]}}
        with patch("app.scraper.sec_acquisitions._get", return_value=index):
            files = instance_files(CIK, "0001652044-26-000048")
        base = "https://www.sec.gov/Archives/edgar/data/1652044/000165204426000048"
        assert files == {"instance": f"{base}/goog-20260331_htm.xml", "labels": f"{base}/goog-20260331_lab.xml"}

    def test_no_instance_is_none(self):
        index = {"directory": {"item": [{"name": "d10k.htm"}, {"name": "dex2101.htm"}]}}
        with patch("app.scraper.sec_acquisitions._get", return_value=index):
            assert instance_files(CIK, "0001193125-07-044494") is None
        with patch("app.scraper.sec_acquisitions._get", side_effect=_err(404)):
            assert instance_files(CIK, "0001193125-07-044494") is None


BASE = "https://www.sec.gov/Archives/edgar/data/1652044"
Q1_ACC, FY_ACC = "0001652044-26-000048", "0001652044-26-000018"
Q1_URL = f"{BASE}/000165204426000048/goog-20260331_htm.xml"
Q1_LAB_URL = f"{BASE}/000165204426000048/goog-20260331_lab.xml"
FY_URL = f"{BASE}/000165204426000018/goog-20251231_htm.xml"


def _serve(instances: dict, labels: dict, index_names: dict):
    """URL-routing fakes for _get (submissions + index.json) and _get_text."""
    calls = []
    subs = _subs([("10-Q", Q1_ACC, "2026-04-30", "2026-03-31"),
                  ("10-K", FY_ACC, "2026-02-05", "2025-12-31")])

    def get(url, params=None):
        calls.append(url)
        if url.endswith("submissions/CIK0001652044.json"):
            return subs
        for acc, names in index_names.items():
            if acc.replace("-", "") in url and url.endswith("index.json"):
                return {"directory": {"item": [{"name": n} for n in names]}}
        raise httpx.ConnectError(f"unexpected {url}")

    def get_text(url):
        calls.append(url)
        if url in instances:
            return instances[url]
        if url in labels:
            v = labels[url]
            if isinstance(v, Exception):
                raise v
            return v
        raise httpx.ConnectError(f"unexpected {url}")
    return get, get_text, calls


class TestFetchAcquisitions:
    INDEX = {Q1_ACC: ["goog-20260331_htm.xml", "goog-20260331_lab.xml"],
             FY_ACC: ["goog-20251231_htm.xml", "goog-20251231_lab.xml"]}

    def test_names_the_deals_from_the_labels_newest_filing_first(self):
        get, get_text, calls = _serve({Q1_URL: Q1, FY_URL: FY25}, {Q1_LAB_URL: Q1_LAB}, self.INDEX)
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", get_text):
            got = fetch_acquisitions(CIK)
        assert got["newest_url"] == Q1_URL
        q1, fy = got["filings"]
        assert (q1["form"], q1["accession"], q1["filing_date"], q1["url"]) == ("10-Q", Q1_ACC, "2026-04-30", Q1_URL)
        assert [(a["name"], a["closing"], a["value_usd"]) for a in q1["acquisitions"]] == [
            ("Wiz Inc", "2026-03-11", 29_500_000_000.0), ("Intersect", "2026-03-10", 5_900_000_000.0)]
        assert fy["acquisitions"] == [] and fy["pending_only"] == ["goog:IntersectMember", "goog:WizIncMember"]
        # labels fetched once, and only for the filing that states a deal
        assert calls.count(Q1_LAB_URL) == 1
        assert not any(u.endswith("goog-20251231_lab.xml") for u in calls)

    def test_missing_labels_fall_back_to_the_qname(self):
        get, get_text, _ = _serve({Q1_URL: Q1, FY_URL: FY25}, {Q1_LAB_URL: _err(404)}, self.INDEX)
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", get_text):
            got = fetch_acquisitions(CIK)
        assert [a["name"] for a in got["filings"][0]["acquisitions"]] == ["Wiz Inc", "Intersect"]

    def test_an_aggregate_member_is_skipped_and_counted(self):
        lab = Q1_LAB.replace("Wiz Inc [Member]", "Other Acquisitions [Member]")
        get, get_text, _ = _serve({Q1_URL: Q1, FY_URL: FY25}, {Q1_LAB_URL: lab}, self.INDEX)
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", get_text):
            got = fetch_acquisitions(CIK)
        q1 = got["filings"][0]
        assert [a["name"] for a in q1["acquisitions"]] == ["Intersect"]
        assert q1["skipped_aggregate"] == 1

    def test_a_filing_without_an_instance_is_counted_not_fatal(self):
        index = {Q1_ACC: ["goog-20260331_htm.xml", "goog-20260331_lab.xml"], FY_ACC: ["d10k.htm"]}
        get, get_text, _ = _serve({Q1_URL: Q1}, {Q1_LAB_URL: Q1_LAB}, index)
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", get_text):
            got = fetch_acquisitions(CIK)
        assert got["no_instance"] == 1 and len(got["filings"]) == 1

    def test_a_5xx_on_the_instance_surfaces(self):
        get, get_text, _ = _serve({}, {}, self.INDEX)
        def boom(url):
            raise _err(503)
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", boom), \
             pytest.raises(httpx.HTTPStatusError):
            fetch_acquisitions(CIK)

    def test_no_filings_is_empty(self):
        with patch("app.scraper.sec_acquisitions._get", return_value=_subs([("8-K", "x", "2026-01-01", "")])):
            got = fetch_acquisitions(CIK)
        assert got == {"filings": [], "newest_url": None, "no_instance": 0, "unparseable": 0}
