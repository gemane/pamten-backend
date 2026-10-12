"""XBRL business combinations: the fixtures are Alphabet's REAL Q1 2026 10-Q
instance and label linkbase (Wiz closed 2026-03-11 for 29.5 bn, Intersect
2026-03-10 for 5.9 bn) and its FY2025 10-K (both deals ANNOUNCED, tagged with
an agreed / expected price in a month-long period), trimmed verbatim. The
hostile cases are edits of those lines: a second axis on the deal's context,
a nil fact, a period longer than a day, a foreign currency."""
import json
from pathlib import Path

import httpx
import pytest
from unittest.mock import patch

from app.scraper.sec_acquisitions import (
    clean_label, fallback_name, fetch_acquisitions, instance_files, parse_instance,
    parse_labels, parse_metalinks, recent_filings,
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

    def test_a_combined_filing_names_the_primary_registrant_first(self):
        # Eversource files with its utilities: one EntityCentralIndexKey per
        # registrant, the primary first — the cover is the first, not the last
        first = '<dei:EntityCentralIndexKey contextRef="c-1" id="f-81">0001652044</dei:EntityCentralIndexKey>'
        second = '<dei:EntityCentralIndexKey contextRef="c-1" id="f-82">0000023426</dei:EntityCentralIndexKey>'
        assert parse_instance(Q1.replace(first, first + second), CIK)["cik"] == "0001652044"

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

    @pytest.mark.parametrize("figure", ["0", "-29500000000"])
    def test_a_zero_or_negative_consideration_is_a_closing_without_a_figure(self, figure):
        p = parse_instance(Q1.replace('unitRef="usd">29500000000<', f'unitRef="usd">{figure}<'), CIK)
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

    def test_metalinks_labels_from_microsofts_real_file(self):
        # Donnelley-filed: no linkbase in the filing, MetaLinks.json carries the labels
        data = json.loads((FIX / "xbrl_microsoft_10k_fy2026_metalinks.json").read_text())
        labels = parse_metalinks(data)
        assert labels["msft:ActivisionBlizzardIncMember"] == "Activision Blizzard Inc [Member]"
        assert labels["us-gaap:BusinessAcquisitionAxis"] == "Business Combination [Axis]"
        assert clean_label(labels["msft:ActivisionBlizzardIncMember"]) == "Activision Blizzard Inc"

    def test_metalinks_terse_label_is_the_fallback_and_junk_is_empty(self):
        data = {"instance": {"x.htm": {"tag": {"a_BMember": {"lang": {"en-us": {"role": {"terseLabel": "B"}}}},
                                               "nounderscore": {"lang": {"en-us": {"role": {"label": "x"}}}},
                                               "a_C": {}}}}}
        assert parse_metalinks(data) == {"a:BMember": "B"}
        assert parse_metalinks([]) == {} and parse_metalinks({}) == {}

    def test_entities_in_labels_are_unescaped(self):
        # Herc's and Innovex's MetaLinks carry "H&amp;E", "Rieber &amp; Son"
        meta = _metalinks(hri_HEEquipmentServicesIncMember="H&amp;E Equipment Services Inc [Member]")
        assert parse_metalinks(meta) == {"hri:HEEquipmentServicesIncMember": "H&E Equipment Services Inc [Member]"}
        lab = Q1_LAB.replace("Wiz Inc [Member]", "Rieber &amp;amp; Son AS LLC [Member]")
        assert parse_labels(lab)["goog:WizIncMember"] == "Rieber & Son AS LLC [Member]"

    @pytest.mark.parametrize("label,expected", [
        ("Wiz Inc [Member]", "Wiz Inc"),
        ("Intersect Acquisition [Member]", "Intersect"),
        ("Mandiant Merger [Member]", "Mandiant"),
        ("Activision Blizzard Inc [Member]", "Activision Blizzard Inc"),
        ("Walmart Advanced Systems & Robotics Inc. [Member]", "Walmart Advanced Systems & Robotics Inc."),
        ("Horizon Acquisition Parent LLC [Member]", "Horizon Acquisition Parent LLC"),
        ("Precision Design And Trim LLC [Member]", "Precision Design And Trim LLC"),
        ("Jackson Labs Technologies, LLC [Member]", "Jackson Labs Technologies, LLC"),
        ("Hess Corporation [Member]", "Hess Corporation"),
        # several deals at once
        ("Other Acquisitions [Member]", None),
        ("Business Combinations [Member]", None),
        ("Individually Immaterial Acquisitions [Member]", None),
        ("Business Combination, Series of Individually Immaterial Business Combinations [Member]", None),
        ("Current Year Acquisitions [Member]", None),
        ("Prior Year Acquisitions [Member]", None),
        ("Water Acquisitions [Member]", None),
        # two companies in one member (Builders FirstSource's baskets)
        ("Alpine Lumber Company And OC Cluss Lumber Company [Member]", None),
        ("Premium Building Components Inc And Precision Design And Trim LLC [Member]", None),
        ("Alpine Lumber, O.C. Cluss, Truckee Tahoe, St. George Truss, Stately Las Vegas, Rystin, "
         "Lengefeld Lumber And Pleasant Valley [Member]", None),
        ("Quality Door Hanson Truss Rpm Schoeneman Trsmi Western Truss Cri Wyoming Millwork Sunrise "
         "Wood Designs Reno Truss And High Mountain [Member]", None),
        ("Premium Building Components [Member]", "Premium Building Components"),
        ("NJOY Holdings, Inc. [Member]", "NJOY Holdings, Inc."),
        # assets and business lines, no legal form
        ("Industrial Technologies [Member]", None),
        ("Fiber-To-The-Home [Member]", None),
        ("Gulf Coast NGL Pipelines Acquisition [Member]", None),
        ("Outpatient Mental Health Practices [Member]", None),
        ("Acquired Property [Member]", None),
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
        assert files == {"instance": f"{base}/goog-20260331_htm.xml", "metalinks": None,
                         "labels": f"{base}/goog-20260331_lab.xml"}

    def test_a_donnelley_filing_has_metalinks_and_no_linkbase(self):
        index = {"directory": {"item": [{"name": "msft-20260630.htm"}, {"name": "msft-20260630_htm.xml"},
                                        {"name": "MetaLinks.json"}, {"name": "FilingSummary.xml"}]}}
        with patch("app.scraper.sec_acquisitions._get", return_value=index):
            files = instance_files("789019", "0001193125-26-323660")
        base = "https://www.sec.gov/Archives/edgar/data/789019/000119312526323660"
        assert files == {"instance": f"{base}/msft-20260630_htm.xml", "metalinks": f"{base}/MetaLinks.json",
                         "labels": None}

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


def _serve(instances: dict, labels: dict, index_names: dict, metalinks: dict | None = None):
    """URL-routing fakes for _get (submissions, index.json, MetaLinks.json) and
    _get_text (instances, label linkbases). A MetaLinks URL not in `metalinks`
    answers 404, the way EDGAR does for a filing without one."""
    calls = []
    subs = _subs([("10-Q", Q1_ACC, "2026-04-30", "2026-03-31"),
                  ("10-K", FY_ACC, "2026-02-05", "2025-12-31")])

    def get(url, params=None):
        calls.append(url)
        if url.endswith("submissions/CIK0001652044.json"):
            return subs
        if url.endswith("MetaLinks.json"):
            v = (metalinks or {}).get(url)
            if v is None:
                raise _err(404)
            if isinstance(v, Exception):
                raise v
            return v
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


Q1_META_URL = f"{BASE}/000165204426000048/MetaLinks.json"


def _metalinks(**labels):
    """A MetaLinks.json shaped like EDGAR's, for the members given as prefix_Local=label."""
    return {"instance": {"goog-20260331.htm": {"tag": {
        k: {"lang": {"en-us": {"role": {"label": v}}}} for k, v in labels.items()}}}}


class TestFetchAcquisitions:
    # a Workiva-style filing: instance + linkbase + MetaLinks.json all present
    INDEX = {Q1_ACC: ["goog-20260331_htm.xml", "goog-20260331_lab.xml", "MetaLinks.json"],
             FY_ACC: ["goog-20251231_htm.xml", "goog-20251231_lab.xml", "MetaLinks.json"]}

    def test_names_the_deals_from_the_labels_newest_filing_first(self):
        # MetaLinks 404s here, so the linkbase names them
        get, get_text, calls = _serve({Q1_URL: Q1, FY_URL: FY25}, {Q1_LAB_URL: Q1_LAB}, self.INDEX)
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", get_text):
            got = fetch_acquisitions(CIK)
        assert got["newest_url"] == Q1_URL
        q1, fy = got["filings"]
        assert (q1["form"], q1["accession"], q1["filing_date"], q1["url"]) == ("10-Q", Q1_ACC, "2026-04-30", Q1_URL)
        assert [(a["name"], a["closing"], a["value_usd"], a["name_source"]) for a in q1["acquisitions"]] == [
            ("Wiz Inc", "2026-03-11", 29_500_000_000.0, "label"),
            ("Intersect", "2026-03-10", 5_900_000_000.0, "label")]
        assert q1["acquisitions"][1]["label"] == "Intersect Acquisition [Member]"
        assert fy["acquisitions"] == [] and fy["pending_only"] == ["goog:IntersectMember", "goog:WizIncMember"]
        # labels fetched once, and only for the filing that states a deal
        assert calls.count(Q1_META_URL) == 1 and calls.count(Q1_LAB_URL) == 1
        assert not any(u.endswith("goog-20251231_lab.xml") or "000165204426000018/MetaLinks" in u for u in calls)

    def test_metalinks_name_the_deals_before_the_linkbase(self):
        # Donnelley-filed instances (Microsoft, Berkshire) ship no linkbase;
        # every inline filing ships MetaLinks.json — it is read first
        meta = _metalinks(goog_WizIncMember="Wiz Incorporated [Member]",
                          goog_IntersectAcquisitionMember="Intersect Power Acquisition [Member]")
        get, get_text, calls = _serve({Q1_URL: Q1, FY_URL: FY25}, {Q1_LAB_URL: Q1_LAB}, self.INDEX,
                                      metalinks={Q1_META_URL: meta})
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", get_text):
            got = fetch_acquisitions(CIK)
        assert [a["name"] for a in got["filings"][0]["acquisitions"]] == ["Wiz Incorporated", "Intersect Power"]
        assert Q1_LAB_URL not in calls, "the linkbase is not fetched when MetaLinks answered"

    def test_missing_labels_fall_back_to_the_qname(self):
        get, get_text, _ = _serve({Q1_URL: Q1, FY_URL: FY25}, {Q1_LAB_URL: _err(404)}, self.INDEX)
        with patch("app.scraper.sec_acquisitions._get", get), \
             patch("app.scraper.sec_acquisitions._get_text", get_text):
            got = fetch_acquisitions(CIK)
        assert [(a["name"], a["name_source"], a["label"]) for a in got["filings"][0]["acquisitions"]] == [
            ("Wiz Inc", "qname", None), ("Intersect", "qname", None)]

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
