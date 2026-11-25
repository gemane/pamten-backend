"""From a name to its filer (app/scraper/sec_lookup.py): the tickers file, EDGAR's name index,
full-text search, a person's CIKs.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""
from unittest.mock import patch, MagicMock

import pytest

from app.scraper.sec_lookup import _normalize_sec_name, _ticker_normalize, _lookup_in_tickers, search_company


# ── Pure helpers ───────────────────────────────────────────────────────────────

class TestNormalizeSecName:
    """SEC stores individual names as 'LAST FIRST [MIDDLE]' — we flip them."""

    def test_two_word_name(self):
        assert _normalize_sec_name("MUSK ELON") == "Elon Musk"

    def test_three_word_name(self):
        assert _normalize_sec_name("COOK TIMOTHY D") == "Timothy D Cook"

    def test_already_one_word(self):
        # Graceful fallback: title-case it
        assert _normalize_sec_name("SATYA") == "Satya"

    def test_strips_trailing_punctuation(self):
        # Names sometimes have trailing periods or commas from SEC data
        assert _normalize_sec_name("MUSK, ELON.") == "Elon Musk"


class TestTickerNormalize:
    def test_strips_inc(self):
        assert _ticker_normalize("Tesla, Inc.") == "tesla"

    def test_lowercases(self):
        assert _ticker_normalize("APPLE INC") == "apple"

    def test_passthrough(self):
        assert _ticker_normalize("Tesla") == "tesla"

    def test_apostrophes_meet_edgars_spelling(self):
        from app.scraper.sec_13dg_text import _significant_tokens
        from app.scraper.sec_lookup import _name_similarity
        spellings = ("Domino's Pizza, Inc.", "Domino’s Pizza, Inc.", "DOMINOS PIZZA INC")
        assert {_ticker_normalize(n) for n in spellings} == {"dominos pizza"}
        assert all("s" not in _significant_tokens(n) and "dominos" in _significant_tokens(n) for n in spellings)
        assert _name_similarity("Domino's Pizza, Inc.", "DOMINOS PIZZA INC") == 1.0


    @pytest.mark.parametrize("name,key", [
        ("NV Energy", "nv energy"),                     # findings item 5: was "energy"
        ("LENNAR CORP /NEW/", "lennar"),                # EDGAR's tag goes first
        ("Alibaba Group Holding Ltd", "alibaba"),
        ("Group 1 Automotive Inc", "group 1 automotive"),
        ("Innovex Co-Invest Fund II, L.P.", "innovex co-invest fund ii"),
    ])
    def test_a_legal_form_goes_only_at_the_end(self, name, key):
        assert _ticker_normalize(name) == key


class TestLookupInTickers:
    """_lookup_in_tickers searches a cached dict of EDGAR listed companies."""

    MOCK_TICKERS = {
        "0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
        "1": {"cik_str": 789019,  "ticker": "MSFT", "title": "MICROSOFT CORP"},
        "2": {"cik_str": 320193,  "ticker": "AAPL", "title": "Apple Inc."},
    }

    def test_exact_match(self):
        with patch("app.scraper.sec_lookup._tickers_cache", self.MOCK_TICKERS):
            result = _lookup_in_tickers("Tesla")
        assert result is not None
        assert result["cik"] == "0001318605"
        assert result["name"] == "Tesla, Inc."

    def test_case_insensitive_match(self):
        with patch("app.scraper.sec_lookup._tickers_cache", self.MOCK_TICKERS):
            result = _lookup_in_tickers("microsoft")
        assert result is not None
        assert "MICROSOFT" in result["name"]

    def test_no_match_returns_none(self):
        with patch("app.scraper.sec_lookup._tickers_cache", self.MOCK_TICKERS):
            result = _lookup_in_tickers("Berkshire Hathaway")
        assert result is None

    def test_ambiguity_resolved_by_shortest_name(self):
        """
        Regression: searching "Apple" must NOT match "Apple Hospitality REIT".
        The tickers file always has the real Apple Inc., which normalises to
        'apple' (exact), while "Apple Hospitality REIT" normalises to something longer.
        Exact matches win; among them the shortest name is preferred.
        """
        tickers = {
            "0": {"cik_str": 320193,  "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 1418121, "ticker": "APLE", "title": "Apple Hospitality REIT, Inc."},
        }
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            result = _lookup_in_tickers("Apple")
        assert result["name"] == "Apple Inc."


    def test_nv_energy_is_not_energy_fuels(self):
        # findings item 5 (2026-10-30): "nv energy" keyed "energy", a prefix of
        # "energy fuels" at similarity 0.67, and Energy Fuels' filings were scraped
        tickers = {"0": {"cik_str": 1385849, "ticker": "UUUU", "title": "ENERGY FUELS INC"}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("NV Energy") is None

    def test_an_adr_programme_is_not_the_company(self):
        # the user's choice 2026-10-30: MTN Group would take the depositary's CIK
        tickers = {"0": {"cik_str": 1163420, "ticker": "MTNOY", "title": "MTN GROUP LTD/ADR"},
                   "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("MTN Group") is None
            assert _lookup_in_tickers("Apple")["cik"] == "0000320193"

    def test_a_surname_first_title_is_the_same_words_in_another_order(self):
        # findings item 59: the file's "PRICE T ROWE GROUP INC" is "T. Rowe Price Group, Inc.";
        # neither the exact nor the prefix match saw it, and the similarity (0.29) would have refused it
        tickers = {"0": {"cik_str": 1113169, "ticker": "TROW", "title": "PRICE T ROWE GROUP INC"},
                   "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("T. Rowe Price Group, Inc.") == {"cik": "0001113169", "name": "PRICE T ROWE GROUP INC"}
            assert _lookup_in_tickers("T Rowe Price Group") == {"cik": "0001113169", "name": "PRICE T ROWE GROUP INC"}

    PETROBRAS = {"0": {"cik_str": 1119639, "ticker": "PBR", "title": "PETROBRAS - PETROLEO BRASILEIRO SA"},
                 "1": {"cik_str": 1119639, "ticker": "PBR-A", "title": "PETROBRAS - PETROLEO BRASILEIRO SA"},
                 "2": {"cik_str": 1499849, "ticker": "LND", "title": "BrasilAgro - Brazilian Agricultural Real Estate Co"}}

    @pytest.mark.parametrize("query", ["Petrobras", "Petroleo Brasileiro S.A.", "PETROBRAS - PETROLEO BRASILEIRO SA"])
    def test_a_two_name_title_is_found_by_either_name(self, query):
        # findings item 136: 'Petrobras' scored 0.45 against the whole title, the
        # lookup refused the company's own entry and the name index gave
        # Petrobras Argentina S.A. — the wrong company, twice, with 48 subsidiaries
        with patch("app.scraper.sec_lookup._tickers_cache", self.PETROBRAS):
            assert _lookup_in_tickers(query) == {"cik": "0001119639", "name": "PETROBRAS - PETROLEO BRASILEIRO SA"}

    def test_a_two_name_title_by_its_trade_name(self):
        with patch("app.scraper.sec_lookup._tickers_cache", self.PETROBRAS):
            assert _lookup_in_tickers("BrasilAgro")["cik"] == "0001499849"

    def test_a_two_name_title_still_needs_one_of_its_names(self):
        # the second name's words are no key: neither a part of a name ...
        with patch("app.scraper.sec_lookup._tickers_cache", self.PETROBRAS):
            assert _lookup_in_tickers("Brasileiro") is None
            assert _lookup_in_tickers("Brazilian Agricultural") is None
        # ... nor a prefix that is far from both names
        tickers = {"0": {"cik_str": 1, "ticker": "X", "title": "Alpha Beta Gamma Delta Epsilon - Zeta Eta Theta Iota"}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("Alpha") is None

    def test_a_hyphen_inside_a_word_parts_no_names(self):
        # only a spaced dash parts two names: "Coca-Cola" is one
        tickers = {"0": {"cik_str": 21344, "ticker": "KO", "title": "COCA-COLA CONSOLIDATED BOTTLING OF THE CAROLINAS INC"}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("Cola") is None
            assert _lookup_in_tickers("Coca") is None

    def test_a_part_does_not_hide_the_fund_word(self):
        # the fund hold reads the whole title (Baillie Gifford & Co is not its funds)
        tickers = {"0": {"cik_str": 1120543, "ticker": "BGF", "title": "Baillie Gifford - Baillie Gifford Funds"}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("Baillie Gifford") is None

    def test_two_words_swapped_are_not_the_same_company(self):
        # the unordered tier needs three words or more (the writers' rule, item 58)
        tickers = {"0": {"cik_str": 1385849, "ticker": "UUUU", "title": "FUELS ENERGY INC"}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("Energy Fuels") is None

    def test_an_exact_title_wins_over_a_reordered_one(self):
        # both key to the same words ("group" is a form word at the end); the exact order comes first
        tickers = {"0": {"cik_str": 1, "ticker": "AAA", "title": "PRICE T ROWE GROUP INC"},
                   "1": {"cik_str": 1113169, "ticker": "TROW", "title": "T Rowe Price Group Inc"}}
        with patch("app.scraper.sec_lookup._tickers_cache", tickers):
            assert _lookup_in_tickers("T. Rowe Price Group, Inc.")["cik"] == "0001113169"

    @pytest.mark.parametrize("title,adr", [
        ("MTN GROUP LTD/ADR", True), ("FOO PLC /ADR/", True), ("BAR SA \\ADR", True),
        ("ADR HOLDINGS INC", False), ("TOYOTA MOTOR CORP/", False), ("LENNAR CORP /NEW/", False), (None, False),
    ])
    def test_is_adr_listing(self, title, adr):
        from app.scraper.sec_lookup import is_adr_listing
        assert is_adr_listing(title) is adr


class TestSearchCompany:
    """search_company prefers tickers lookup and falls back to full-text search."""

    MOCK_TICKERS = {
        "0": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
    }

    def test_returns_from_tickers(self):
        with patch("app.scraper.sec_lookup._tickers_cache", self.MOCK_TICKERS):
            result = search_company("Tesla")
        assert result is not None
        assert result["cik"] == "0001318605"

    def test_full_text_fallback(self):
        """When tickers miss, falls back to EDGAR full-text search."""
        empty_tickers = {}
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "hits": {"hits": [{
                "_source": {
                    "display_names": ["PrivateCo  (CIK 0009999999)"],
                    "ciks":          ["9999999"],
                    "adsh":          "0009999999-22-000001",
                }
            }]}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.scraper.sec_lookup._tickers_cache", empty_tickers), \
             patch("app.scraper.sec_client._get_client",
                   return_value=MagicMock(get=MagicMock(return_value=mock_response))):
            result = search_company("PrivateCo")

        assert result is not None
        assert result["cik"] == "0009999999"

    def test_returns_none_when_not_found(self):
        empty_tickers = {}
        mock_response = MagicMock()
        mock_response.json.return_value = {"hits": {"hits": []}}
        mock_response.raise_for_status = MagicMock()

        with patch("app.scraper.sec_lookup._tickers_cache", empty_tickers), \
             patch("app.scraper.sec_client._get_client",
                   return_value=MagicMock(get=MagicMock(return_value=mock_response))):
            result = search_company("NonExistentXYZ123")

        assert result is None


    def test_a_fund_vehicle_is_not_its_sponsor(self):
        """2026-10-29: the full-text hit for "Baillie Gifford & Co" (the 13F
        manager, CIK 0001088875) was the proxy of BAILLIE GIFFORD FUNDS, and the
        manager's node took the funds' CIK. Not found beats a wrong CIK."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"hits": {"hits": [{"_source": {
            "display_names": ["BAILLIE GIFFORD FUNDS  (CIK 0001120543)"],
            "ciks": ["1120543"], "adsh": "0001120543-25-000001"}}]}}
        mock_response.raise_for_status = MagicMock()
        mock_response.content = b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
        with patch("app.scraper.sec_lookup._tickers_cache", {}), \
             patch("app.scraper.sec_client.time.sleep"), \
             patch("app.scraper.sec_client._get_client",
                   return_value=MagicMock(get=MagicMock(return_value=mock_response))):
            assert search_company("Baillie Gifford & Co") is None


class TestBrowseEdgarByName:
    """`_lookup_edgar_by_name` (findings item 63): EDGAR's two answer shapes, and
    the type-free pass that finds a registrant whatever it files — by the exact
    name only, never an ADR programme, EDGAR's duplicates by what they file."""

    ATOM = "http://www.w3.org/2005/Atom"
    EMPTY = f'<feed xmlns="{ATOM}"><author><name>Webmaster</name></author></feed>'

    @classmethod
    def many(cls, *ciks):
        """The list shape: one entry per registrant, the CIK in the id, the name
        lost to EDGAR's server-side bug (name="ARRAY(0x1)")."""
        entries = "".join(
            f'<entry><title></title><id>urn:tag:www.sec.gov:cik={c}</id>'
            f'<content type="text/xml"><company-info name="ARRAY(0x1)"><cik>{c}</cik></company-info></content>'
            f'</entry>' for c in ciks)
        return f'<feed xmlns="{cls.ATOM}"><title>Companies</title>{entries}</feed>'

    @classmethod
    def lone(cls, cik, name):
        """The lone shape: the registrant's own filing feed, <company-info> at
        the root, entries that are filings."""
        return (f'<feed xmlns="{cls.ATOM}"><title>{name}</title>'
                f'<company-info><cik>{cik}</cik><conformed-name>{name}</conformed-name></company-info>'
                f'<entry><title>10-K</title><id>urn:tag:sec.gov,2008:accession-number={cik}-18-000006</id></entry>'
                f'</feed>')

    @staticmethod
    def sub(name, forms=("10-K",), dates=("2025-03-01",)):
        return {"name": name, "filings": {"recent": {"form": list(forms), "filingDate": list(dates)}}}

    def _lookup(self, name, browse, submissions=None, fn=None):
        """browse: form type ("10-K", "20-F", "") → feed body; submissions: CIK →
        document. Returns (result, the browse types asked, the CIKs read)."""
        import re
        import httpx
        from app.scraper import sec_client, sec_lookup
        submissions = submissions or {}
        asked, read = [], []

        def get(url, params=None):
            req = httpx.Request("GET", url)
            if "browse-edgar" in url:
                asked.append(params["type"])
                return httpx.Response(200, text=browse.get(params["type"], self.EMPTY), request=req)
            m = re.search(r"CIK(\d{10})\.json", url)
            if m and m.group(1) in submissions:
                read.append(m.group(1))
                return httpx.Response(200, json=submissions[m.group(1)], request=req)
            if "efts" in url:
                return httpx.Response(200, json={"hits": {"hits": []}}, request=req)
            return httpx.Response(404, request=req)
        client = MagicMock()
        client.get.side_effect = get
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"), \
             patch.object(sec_lookup, "_tickers_cache", {}):
            return (fn or sec_lookup._lookup_edgar_by_name)(name), asked, read

    def test_a_lone_registrant_is_found_on_the_filtered_pass(self):
        # one 10-K filer of the name: EDGAR answers with its filing feed, which
        # the lookup never read — Sears Holdings Corp was "not found" (2026-11-06)
        result, asked, read = self._lookup("Sears Holdings Corp",
                                           {"10-K": self.lone("0001310067", "SEARS HOLDINGS CORP")})
        assert result == {"cik": "0001310067", "name": "SEARS HOLDINGS CORP"}
        assert asked == ["10-K"] and read == []        # the feed named it: no submissions request

    def test_a_filer_with_no_annual_report_is_found_on_the_type_free_pass(self):
        # Capital Ventures International files SC 13D/G only: the 10-K and 20-F
        # passes list nothing, the type-free pass answers with its feed
        result, asked, _ = self._lookup("Capital Ventures International",
                                        {"": self.lone("0001011712", "CAPITAL VENTURES INTERNATIONAL")})
        assert result == {"cik": "0001011712", "name": "CAPITAL VENTURES INTERNATIONAL"}
        assert asked == ["10-K", "20-F", ""]

    def _lookup_by_query(self, name, feeds, submissions=None):
        """Like `_lookup`, keyed by (form type, company query): the base-name
        retry asks the index with other words than the name (findings item 69).
        Returns (result, the (type, company) pairs asked)."""
        import re
        import httpx
        from app.scraper import sec_client, sec_lookup
        submissions = submissions or {}
        asked = []

        def get(url, params=None):
            req = httpx.Request("GET", url)
            if "browse-edgar" in url:
                asked.append((params["type"], params["company"]))
                return httpx.Response(200, text=feeds.get((params["type"], params["company"]), self.EMPTY), request=req)
            m = re.search(r"CIK(\d{10})\.json", url)
            if m and m.group(1) in submissions:
                return httpx.Response(200, json=submissions[m.group(1)], request=req)
            return httpx.Response(404, request=req)
        client = MagicMock()
        client.get.side_effect = get
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"), \
             patch.object(sec_lookup, "_tickers_cache", {}):
            return sec_lookup._lookup_edgar_by_name(name), asked

    def test_a_registrant_under_another_form_word_is_found_by_the_base_name(self):
        # Independent Franchise Partners, Llp (a 13F filer, 2026-11-06 guard): the index
        # lists it for "independent franchise partners", never for the name as given
        # (a prefix search); the exact key folds the form
        ifp = "0001483866"
        result, asked = self._lookup_by_query(
            "Independent Franchise Partners, Llp",
            {("", "independent franchise partners"): self.lone(ifp, "Independent Franchise Partners LLP")},
            submissions={ifp: self.sub("Independent Franchise Partners LLP", forms=("13F-HR", "SC 13G"))})
        assert result == {"cik": ifp, "name": "Independent Franchise Partners LLP"}
        assert asked == [("10-K", "Independent Franchise Partners, Llp"), ("20-F", "Independent Franchise Partners, Llp"),
                         ("", "Independent Franchise Partners, Llp"), ("", "independent franchise partners")]

    def test_a_dormant_registrant_the_base_name_lists_alone_is_not_taken(self):
        # "t rowe price group" lists T ROWE PRICE GROUP INC (no-action letters, 2015) and
        # never the filer PRICE T ROWE GROUP INC, conformed surname first (item 59): a lone
        # registrant that files nothing the scraper reads is no answer. YouTube Inc (two
        # REGDEX of 2006) likewise.
        result, asked = self._lookup_by_query(
            "T. Rowe Price Group, Inc.", {("", "t rowe price group"): self.lone("0001214556", "T ROWE PRICE GROUP INC")},
            submissions={"0001214556": self.sub("T ROWE PRICE GROUP INC", forms=("NO ACT", "NO ACT"))})
        assert result is None and asked[-1] == ("", "t rowe price group")
        result, _ = self._lookup_by_query(
            "YouTube LLC", {("", "youtube"): self.lone("0001343726", "YouTube Inc")},
            submissions={"0001343726": self.sub("YouTube Inc", forms=("REGDEX",))})
        assert result is None

    def test_the_base_name_pass_still_holds_the_exact_key(self):
        # "Nestlé Holdings" is not Nestlé: the base words list the parent too,
        # and the hold refuses what is not the query's name
        result, asked = self._lookup_by_query(
            "Nestlé Holdings", {("", "nestle holdings"): self.lone("0000792990", "NESTLE SA")},
            submissions={"0000792990": self.sub("NESTLE SA", forms=("SC 13G", "4"))})     # a filer we read, wrong name
        assert result is None and asked[-1] == ("", "nestle holdings")

    def test_a_name_that_is_its_own_base_asks_once(self):
        # no form word to strip: the type-free pass is not asked twice
        result, asked = self._lookup_by_query("Carlsberg", {})
        assert result is None and asked == [("10-K", "Carlsberg"), ("20-F", "Carlsberg"), ("", "Carlsberg")]

    def test_a_registrant_the_full_name_lists_needs_no_second_ask(self):
        result, asked = self._lookup_by_query("YouTube Inc", {("", "YouTube Inc"): self.lone("0001343726", "YouTube Inc")})
        assert result == {"cik": "0001343726", "name": "YouTube Inc"}
        assert asked[-1] == ("", "YouTube Inc")

    def test_the_type_free_pass_takes_the_exact_name_only(self):
        # "Nestlé" lists the US holding and the Swiss parent at the same similarity
        # (0.83); only the parent's name IS the query's
        subs = {"0000923122": self.sub("NESTLE HOLDINGS INC"), "0000792990": self.sub("NESTLE SA", forms=("4",))}
        result, _, _ = self._lookup("Nestlé", {"": self.many("0000923122", "0000792990")}, subs)
        assert result == {"cik": "0000792990", "name": "NESTLE SA"}

    def test_a_subsidiary_of_the_name_is_not_the_company(self):
        # the adviser is MASSACHUSETTS FINANCIAL SERVICES CO; its Japanese and
        # Canadian subsidiaries score 1.00 and 0.88 by similarity
        subs = {"0001442857": self.sub("MFS Investment Management K.K."),
                "0001140601": self.sub("MFS INVESTMENT MANAGEMENT CANADA LTD")}
        result, _, _ = self._lookup("MFS Investment Management", {"": self.many("0001442857", "0001140601")}, subs)
        assert result is None

    def test_a_form_or_tag_apart_is_still_the_name(self):
        subs = {"0000879316": self.sub("SAMSUNG ELECTRONICS CO LTD /FI", forms=("SC 13G",)),
                "0001087051": self.sub("Bertelsmann SE & Co. KGaA")}
        assert self._lookup("Samsung Electronics", {"": self.many("0000879316")}, subs)[0]["cik"] == "0000879316"
        assert self._lookup("Bertelsmann SE", {"": self.many("0001087051")}, subs)[0]["cik"] == "0001087051"

    def test_the_filtered_passes_keep_the_closest_name(self):
        # the 10-K/20-F passes' rule stays similarity: "Tesla Motors" is TESLA INC
        # at 0.73 though the keys differ — a filtered list has no namesakes to fear
        subs = {"0001318605": self.sub("TESLA INC")}
        result, asked, _ = self._lookup("Tesla Motors", {"10-K": self.many("0001318605")}, subs)
        assert result == {"cik": "0001318605", "name": "TESLA INC"} and asked == ["10-K"]

    def test_an_adr_programme_is_not_the_company(self):
        # the user's rule of 2026-10-30, applied where the type-free pass meets it
        subs = {"0001668748": self.sub("Naspers Ltd/ADR", forms=("F-6",)), "0001106051": self.sub("NASPERS LTD"),
                "0001163420": self.sub("MTN GROUP LTD/ADR", forms=("F-6",))}
        assert self._lookup("Naspers", {"": self.many("0001668748", "0001106051")}, subs)[0]["cik"] == "0001106051"
        assert self._lookup("MTN Group", {"": self.many("0001163420")}, subs)[0] is None

    def test_edgar_duplicates_go_to_the_one_that_files_what_we_read(self):
        # CITADEL SECURITIES LLC twice: the broker-dealer's FOCUS reports (listed
        # first, filed last) and the SC 13D/G + Form 4 filer
        subs = {"0001146184": self.sub("CITADEL SECURITIES LLC", forms=("X-17A-5", "FOCUSN"), dates=("2026-02-27",)),
                "0001307720": self.sub("CITADEL SECURITIES LLC", forms=("SC 13G", "4"), dates=("2022-09-13",))}
        result, _, _ = self._lookup("Citadel Securities Llc", {"": self.many("0001146184", "0001307720")}, subs)
        assert result["cik"] == "0001307720"

    def test_edgar_duplicates_then_to_the_one_that_filed_last(self):
        # NESTLE SA twice, neither with a form we read: the live registrant wins
        subs = {"0000800921": self.sub("NESTLE SA /FI", forms=("ARS",), dates=("2009-07-07",)),
                "0000792990": self.sub("NESTLE SA", forms=("F-6",), dates=("2026-02-20",))}
        result, _, _ = self._lookup("Nestle", {"": self.many("0000800921", "0000792990")}, subs)
        assert result["cik"] == "0000792990"

    def test_search_company_asks_the_index_before_the_full_text_search(self):
        from app.scraper import sec_lookup
        result, asked, _ = self._lookup("Valor Management Corp.",
                                        {"": self.lone("0001512888", "Valor Management Corp.")},
                                        fn=sec_lookup.search_company)
        assert result == {"cik": "0001512888", "name": "Valor Management Corp."}
        assert asked == ["10-K", "20-F", ""]

    def test_the_list_shape_reads_the_cik_from_the_entry_when_the_id_lacks_it(self):
        import xml.etree.ElementTree as ET
        from app.scraper import sec_lookup
        feed = (f'<feed xmlns="{self.ATOM}"><entry><id>urn:tag:www.sec.gov:x</id><content>'
                f'<company-info><cik>51734</cik></company-info></content></entry></feed>')
        assert sec_lookup._browse_registrants(ET.fromstring(feed)) == [("0000051734", None)]


class TestFundVehicles:
    def test_a_fund_word_the_query_lacks_scores_zero(self):
        from app.scraper.sec_lookup import _name_similarity
        assert _name_similarity("Baillie Gifford & Co", "BAILLIE GIFFORD FUNDS") == 0.0
        assert _name_similarity("Blackstone", "Blackstone Private Credit Fund") == 0.0
        assert _name_similarity("iShares", "iShares Trust") == 0.0

    def test_a_fund_word_the_query_has_is_kept(self):
        from app.scraper.sec_lookup import _name_similarity
        assert _name_similarity("Northern Trust", "NORTHERN TRUST CORP") == 1.0
        assert _name_similarity("Vanguard Funds", "VANGUARD FUNDS") == 1.0
        assert _name_similarity("Trustmark", "TRUSTMARK CORP") == 1.0     # a word, not a prefix

    def test_and_co_is_a_legal_form(self):
        from app.scraper.sec_lookup import _ticker_normalize
        assert _ticker_normalize("BAILLIE GIFFORD & CO") == _ticker_normalize("Baillie Gifford") == "baillie gifford"
        assert _ticker_normalize("Procter & Gamble Co") == "procter & gamble"


class TestPersonCikLookup:
    """EDGAR names a person surname first and its name search matches from the
    start: "Robyn Denholm" found nobody, "Denholm Robyn" finds her (findings
    item 29). The two answer shapes are EDGAR's real ones: one match names the
    filer (company-info + conformed-name), several give CIKs alone."""

    ONE = ("<feed><company-info><cik>0001242782</cik>"
           "<conformed-name>DENHOLM ROBYN M</conformed-name></company-info></feed>")
    MANY = ("<feed><entry><content><company-info name=\"ARRAY(0x1)\"><cik>0001145065</cik>"
            "</company-info></content></entry><entry><content><company-info name=\"ARRAY(0x2)\">"
            "<cik>0001235136</cik></company-info></content></entry></feed>")
    NONE = "<feed><author><name>Webmaster</name></author></feed>"

    def _lookup(self, name, answers):
        from app.scraper import sec_client, sec_lookup
        asked = []

        def get(url, params):
            asked.append(params["company"])
            resp = MagicMock()
            resp.text = answers.get(params["company"], self.NONE)
            return resp
        client = MagicMock()
        client.get.side_effect = get
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            return sec_lookup._lookup_person_ciks(name), asked

    def test_surname_first_is_asked_first(self):
        found, asked = self._lookup("Robyn Denholm", {"Denholm Robyn": self.ONE})
        assert found == ["0001242782"] and asked == ["Denholm Robyn"]

    def test_a_middle_initial_and_a_suffix_are_left_out(self):
        _, asked = self._lookup("Thomas S. Murphy, Jr.", {})
        assert asked == ["Murphy Thomas", "Thomas S. Murphy"]

    def test_an_sec_styled_name_is_found_as_given(self):
        found, asked = self._lookup("DENHOLM ROBYN M", {"DENHOLM ROBYN M": self.ONE})
        assert found == ["0001242782"] and asked == ["M DENHOLM", "DENHOLM ROBYN M"]

    def test_one_match_with_another_name_is_refused(self):
        found, _ = self._lookup("Robyn Smith", {"Smith Robyn": self.ONE, "Robyn Smith": self.ONE})
        assert found == []

    def test_several_matches_are_all_candidates(self):
        found, _ = self._lookup("Thomas Murphy", {"Murphy Thomas": self.MANY})
        assert found == ["0001145065", "0001235136"]

    def test_a_failed_search_tries_the_name_as_given(self):
        from app.scraper import sec_client, sec_lookup
        import httpx
        client = MagicMock()
        ok = MagicMock(text=self.ONE)
        client.get.side_effect = [httpx.ConnectError("down"), ok]
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            assert sec_lookup._lookup_person_ciks("Robyn M. Denholm") == ["0001242782"]
