"""What a filer's own record says (app/scraper/sec_filer.py): former names, LEI, website, the
annual report's cover, where it is incorporated.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""
from unittest.mock import patch

import pytest

from app.scraper.sec_filer import fetch_former_names, fetch_company_lei


class TestFetchFormerNames:
    _SUBS = {"name": "Meta Platforms, Inc.", "formerNames": [
        {"name": "Facebook Inc", "from": "2005-05-06T00:00:00.000Z", "to": "2021-10-27T00:00:00.000Z"},
        {"name": "TheFacebook, Inc.", "from": "2004-01-01T00:00:00.000Z", "to": "2005-05-05T00:00:00.000Z"},
    ]}

    def test_returns_former_names_in_order_with_the_day_each_ended(self):
        with patch("app.scraper.sec_client._get", return_value=self._SUBS):
            assert fetch_former_names("0001326801") == ["Facebook Inc|2021-10-27", "TheFacebook, Inc.|2005-05-05"]

    def test_dedupes_case_insensitively_and_tolerates_a_missing_date(self):
        subs = {"formerNames": [{"name": "Square, Inc."}, {"name": "SQUARE, INC.", "to": "2021-12-10T05:00:00.000Z"},
                                {"name": ""}]}
        with patch("app.scraper.sec_client._get", return_value=subs):
            assert fetch_former_names("x") == ["Square, Inc.|"]

    def test_empty_or_missing(self):
        with patch("app.scraper.sec_client._get", return_value={"name": "Acme"}):
            assert fetch_former_names("x") == []

    def test_swallows_fetch_error(self):
        with patch("app.scraper.sec_client._get", side_effect=RuntimeError("404")):
            assert fetch_former_names("x") == []


class TestFetchCompanyLei:
    """The LEI from EDGAR submissions bridges a SEC entity to its GLEIF node."""

    def test_returns_reported_lei(self):
        with patch("app.scraper.sec_client._get",
                   return_value={"name": "X", "lei": "5493001KJTIIGC8Y1R12"}):
            assert fetch_company_lei("x") == "5493001KJTIIGC8Y1R12"

    def test_null_or_missing_lei(self):
        # Microsoft-style: field present but null, or absent entirely → None
        with patch("app.scraper.sec_client._get", return_value={"lei": None}):
            assert fetch_company_lei("x") is None
        with patch("app.scraper.sec_client._get", return_value={"name": "X"}):
            assert fetch_company_lei("x") is None

    def test_swallows_fetch_error(self):
        with patch("app.scraper.sec_client._get", side_effect=RuntimeError("404")):
            assert fetch_company_lei("x") is None


class TestSecWebsite:
    def test_the_corporate_site_wins_over_investor_relations(self):
        from app.scraper.sec_filer import sec_website
        assert sec_website({"website": "https://www.tesla.com",
                            "investorWebsite": "https://ir.tesla.com"}) == "https://www.tesla.com"

    def test_investor_site_is_the_fallback(self):
        from app.scraper.sec_filer import sec_website
        assert sec_website({"website": "", "investorWebsite": "https://ir.tesla.com"}) \
            == "https://ir.tesla.com"

    def test_junk_is_rejected_like_wikidatas(self):
        from app.scraper.sec_filer import sec_website
        assert sec_website({"website": "tesla.com"}) is None    # no scheme guessing
        assert sec_website({}) is None

    def test_the_fetch_wrapper_swallows_errors(self):
        from unittest.mock import patch
        import httpx
        from app.scraper import sec_client, sec_filer
        with patch.object(sec_client, "_submissions",
                          side_effect=httpx.ConnectError("down")):
            assert sec_filer.fetch_filer_website("0001318605") is None


class TestIncorporationPlace:
    """Where a filer is incorporated — Exhibit 21 tells its own row from a
    namesake abroad by it (findings item 17)."""

    @pytest.mark.parametrize("subs, expected", [
        ({"stateOfIncorporation": "NY", "stateOfIncorporationDescription": "NY"}, "US-NY"),
        ({"stateOfIncorporation": "de"}, "US-DE"),
        ({"stateOfIncorporation": "E9", "stateOfIncorporationDescription": "Cayman Islands"}, "KY"),
        ({"stateOfIncorporation": "", "addresses": {"business": {"country": "Germany"}}}, None),
        ({}, None),
    ])
    def test_incorporation_place(self, subs, expected):
        from app.scraper.sec_filer import incorporation_place
        assert incorporation_place(subs) == expected


class TestStatedIncorporation:
    """The newest tagged cover (10-K, 20-F, 40-F, 10-Q, 8-K) says where the
    filer is incorporated; EDGAR's header field only when it does not
    (findings item 19, 2026-10-30; the 10-Q and 8-K since item 23)."""

    @staticmethod
    def _subs(forms=("10-K",), **header):
        return {"cik": "1404655", **header,
                "filings": {"recent": {"form": list(forms),
                                       "accessionNumber": [f"0001193125-26-04664{i}" for i in range(len(forms))],
                                       "primaryDocument": [f"doc{i}.htm" for i in range(len(forms))]}}}

    @staticmethod
    def _tag(value, fmt=None):
        attr = f' format="{fmt}"' if fmt else ""
        return (f'<html><ix:nonNumeric contextRef="c-1" name="dei:EntityIncorporationStateCountryCode"{attr}>'
                f'<span style="font-weight:bold">{value}</span></ix:nonNumeric></html>')

    @pytest.mark.parametrize("value, fmt, expected", [
        ("Delaware", "ixt-sec:stateprovnameen", "US-DE"),          # HubSpot, whose header says NY
        ("Cayman Islands", "ixt-sec:edgarprovcountryen", "KY"),    # Alibaba, whose header says Hong Kong
        ("United Mexican States", "ixt-sec:edgarprovcountryen", "MX"),
        ("Brazil", "ixt-sec:edgarprovcountryen", "BR"),
        ("DE", None, "US-DE"),                                     # EDGAR's own code, untransformed
        ("E9", None, None),
    ])
    def test_the_cover_tag(self, value, fmt, expected):
        from app.scraper import sec_client, sec_filer
        with patch.object(sec_client, "_get_text", return_value=self._tag(value, fmt)):
            assert sec_filer.stated_incorporation(self._subs()) == expected

    def test_the_newest_tagged_cover_is_read_whatever_the_form(self):
        from app.scraper import sec_client, sec_filer
        with patch.object(sec_client, "_get_text", return_value=self._tag("Delaware", "ixt-sec:stateprovnameen")) as get:
            sec_filer.stated_incorporation(self._subs(forms=("6-K", "8-K/A", "8-K", "10-Q", "10-K")))
        get.assert_called_once_with("https://www.sec.gov/Archives/edgar/data/1404655/000119312526046642/doc2.htm")

    def test_a_change_of_state_after_the_annual_report_is_read(self):
        # Liberty Media: Delaware → Nevada on 2026-05-12, after its 10-K of
        # February; the 8-Ks and the 10-Q since say Nevada (findings item 23)
        from app.scraper import sec_client, sec_filer
        pages = {"doc0.htm": self._tag("Nevada", "ixt-sec:stateprovnameen"),
                 "doc1.htm": self._tag("Nevada", "ixt-sec:stateprovnameen"),
                 "doc2.htm": self._tag("Delaware", "ixt-sec:stateprovnameen")}
        with patch.object(sec_client, "_get_text", side_effect=lambda url: pages[url.rsplit("/", 1)[1]]):
            assert sec_filer.stated_incorporation(self._subs(forms=("8-K", "10-Q", "10-K"))) == "US-NV"

    def test_an_untagged_cover_passes_to_the_next_within_the_limit(self):
        from app.scraper import sec_client, sec_filer
        pages = iter(["<html>untagged</html>", self._tag("New York", "ixt-sec:stateprovnameen")])
        with patch.object(sec_client, "_get_text", side_effect=lambda url: next(pages)):
            assert sec_filer.stated_incorporation(self._subs(forms=("8-K", "10-K"))) == "US-NY"
        sec_filer._cover_of.cache_clear()                    # the same URLs again below
        untagged = ["<html>untagged</html>"] * sec_filer._MAX_COVERS
        pages = iter(untagged + [self._tag("New York", "ixt-sec:stateprovnameen")])
        with patch.object(sec_client, "_get_text", side_effect=lambda url: next(pages)) as get:
            assert sec_filer.stated_incorporation(
                self._subs(forms=("8-K",) * sec_filer._MAX_COVERS + ("10-K",))) is None
        assert get.call_count == sec_filer._MAX_COVERS

    def test_a_joint_filing_gives_each_registrant_its_own_cover(self):
        # Eversource files its 10-Qs and 8-Ks jointly with CL&P, NSTAR Electric
        # and PSNH; the first cover is the parent's, Massachusetts
        from app.scraper import sec_client, sec_filer
        doc = ("<html>"
               '<ix:nonNumeric name="dei:EntityRegistrantName" contextRef="c-1">EVERSOURCE ENERGY</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="c-1">0000072741</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityIncorporationStateCountryCode" contextRef="c-1">Massachusetts</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityRegistrantName" contextRef="c-2">THE CONNECTICUT LIGHT AND POWER COMPANY</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="c-2">0000023426</ix:nonNumeric>'
               '<ix:nonNumeric name="dei:EntityIncorporationStateCountryCode" contextRef="c-2">Connecticut</ix:nonNumeric>'
               "</html>")
        with patch.object(sec_client, "_get_text", return_value=doc):
            assert sec_filer.stated_incorporation({**self._subs(), "cik": "23426"}) == "US-CT"
            assert sec_filer.stated_incorporation({**self._subs(), "cik": "72741"}) == "US-MA"

    @pytest.mark.parametrize("subs", [
        {"cik": "1404655"},                                        # no filings listed
        {"filings": {"recent": {"form": ["10-K"], "accessionNumber": ["x"], "primaryDocument": ["d.htm"]}}},
    ])
    def test_nothing_to_read(self, subs):
        from app.scraper import sec_client, sec_filer
        with patch.object(sec_client, "_get_text") as get:
            assert sec_filer.stated_incorporation(subs) is None
        get.assert_not_called()

    def test_the_header_answers_when_the_report_does_not(self):
        from app.scraper import sec_client, sec_filer
        subs = self._subs(stateOfIncorporation="NY", stateOfIncorporationDescription="NY")
        with patch.object(sec_client, "_get_text", return_value="<html>untagged</html>"):
            assert sec_filer.incorporation_place(subs) == "US-NY"
        with patch.object(sec_client, "_get_text", side_effect=RuntimeError("down")):
            assert sec_filer.incorporation_place(subs) == "US-NY"

    def test_the_report_beats_the_header(self):
        from app.scraper import sec_client, sec_filer
        subs = self._subs(stateOfIncorporation="NY", stateOfIncorporationDescription="NY")
        with patch.object(sec_client, "_get_text", return_value=self._tag("Delaware", "ixt-sec:stateprovnameen")):
            assert sec_filer.incorporation_place(subs) == "US-DE"


class TestReportCover:
    """Country and headquarters from the newest annual report's cover, the
    company record only when it states none (2026-10-30)."""
    SUBS = {"cik": "1577552", "stateOfIncorporation": "K3", "stateOfIncorporationDescription": "Hong Kong",
            "addresses": {"business": {"street1": "26/F TOWER ONE TIMES SQUARE", "city": "CAUSEWAY BAY",
                                       "stateOrCountry": "K3", "stateOrCountryDescription": "Hong Kong",
                                       "zipCode": "00000"}},
            "filings": {"recent": {"form": ["6-K", "20-F"], "accessionNumber": ["x-1", "0001193125-26-231755"],
                                   "primaryDocument": ["a.htm", "baba-20260331.htm"]}}}

    @staticmethod
    def _cover(*facts, context="c-1"):
        tags = "".join(f'<ix:nonNumeric name="dei:{n}" contextRef="{c}">{v}</ix:nonNumeric>'
                       for n, v, c in [(*f, context) if len(f) == 2 else f for f in facts])
        return f"<html>{tags}</html>"

    ALIBABA = staticmethod(lambda: TestReportCover._cover(
        ("EntityRegistrantName", "Alibaba Group Holding Limited"), ("EntityIncorporationStateCountryCode", "Cayman Islands"),
        ("EntityAddressAddressLine1", "26/F Tower One"), ("EntityAddressAddressLine2", "Times Square"),
        ("EntityAddressAddressLine3", "1 Matheson Street"), ("EntityAddressCityOrTown", "Causeway Bay"),
        ("EntityAddressPostalZipCode", "00000"), ("EntityAddressCountry", "Hong Kong")))

    def test_country_and_office_from_the_report(self):
        from app.scraper import sec_client, sec_filer
        with patch.object(sec_client, "_get_text", return_value=self.ALIBABA()) as get:
            assert sec_filer.sec_country(self.SUBS) == "KY"                  # the record says Hong Kong
            hq = sec_filer.sec_headquarters(self.SUBS)
        assert hq == {"address": "26/F Tower One Times Square 1 Matheson Street, Causeway Bay, HK",
                      "city": "Causeway Bay", "country": "HK", "street": "26/F Tower One Times Square 1 Matheson Street",
                      "postcode": None, "state": None}                      # "00000" is no postcode
        get.assert_called_once()                                              # one report, read once

    def test_a_us_office_carries_its_state(self):
        from app.scraper import sec_client, sec_filer
        page = self._cover(("EntityRegistrantName", "HubSpot, Inc."), ("EntityAddressAddressLine1", "Two Canal Park"),
                           ("EntityAddressCityOrTown", "Cambridge"), ("EntityAddressStateOrProvince", "Massachusetts"),
                           ("EntityAddressPostalZipCode", "02141"))
        with patch.object(sec_client, "_get_text", return_value=page):
            hq = sec_filer.stated_headquarters({**self.SUBS, "cik": "1404655"})
        assert (hq["city"], hq["state"], hq["country"], hq["postcode"]) == ("Cambridge", "MA", "US", "02141")

    def test_the_registrants_own_context_not_the_business_contact(self):
        # AB InBev's 20-F tags a second address under an axis
        from app.scraper import sec_client, sec_filer
        page = self._cover(("EntityAddressCityOrTown", "Elsewhere", "contact"),
                           ("EntityRegistrantName", "Anheuser-Busch InBev SA/NV", "own"),
                           ("EntityAddressAddressLine1", "Brouwerijplein 1", "own"),
                           ("EntityAddressCityOrTown", "Leuven", "own"), ("EntityAddressCountry", "Belgium", "own"))
        with patch.object(sec_client, "_get_text", return_value=page):
            assert sec_filer.stated_headquarters(self.SUBS)["city"] == "Leuven"

    def test_a_po_box_is_no_office(self):
        from app.scraper import sec_client, sec_filer
        page = self._cover(("EntityRegistrantName", "Inter & Co, Inc."),
                           ("EntityAddressAddressLine1", "PO Box 309, Ugland House"),
                           ("EntityAddressCityOrTown", "Grand Cayman"), ("EntityAddressCountry", "Cayman Islands"))
        subs = {**self.SUBS, "addresses": {"business": {"street1": "AV BARBACENA 1219", "city": "BELO HORIZONTE",
                                                        "stateOrCountry": "D5", "stateOrCountryDescription": "Brazil"}}}
        with patch.object(sec_client, "_get_text", return_value=page):
            assert sec_filer.sec_headquarters(subs)["city"] == "BELO HORIZONTE"

    def test_no_report_keeps_the_record(self):
        from app.scraper import sec_client, sec_filer
        subs = {k: v for k, v in self.SUBS.items() if k != "filings"}
        with patch.object(sec_client, "_get_text") as get:
            assert sec_filer.sec_country(subs) == "HK"
            assert sec_filer.sec_headquarters(subs)["city"] == "CAUSEWAY BAY"
        get.assert_not_called()

    def test_an_unreadable_report_keeps_the_record_and_is_asked_again(self):
        from app.scraper import sec_client, sec_filer
        with patch.object(sec_client, "_get_text", side_effect=RuntimeError("down")):
            assert sec_filer.sec_country(self.SUBS) == "HK"
        with patch.object(sec_client, "_get_text", return_value=self.ALIBABA()):
            assert sec_filer.sec_country(self.SUBS) == "KY"                  # the failure was not kept


class TestFilerIdentity:
    def test_filer_identity_from_one_request(self):
        from app.scraper.sec_filer import fetch_filer_identity
        with patch("app.scraper.sec_client._submissions",
                   return_value={"name": "NEW YORK TIMES CO", "stateOfIncorporation": "NY"}) as subs:
            assert fetch_filer_identity("71691") == ("NEW YORK TIMES CO", "US-NY")
        assert subs.call_count == 1
        with patch("app.scraper.sec_client._submissions", side_effect=RuntimeError("503")):
            assert fetch_filer_identity("71691") == (None, None)
