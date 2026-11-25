"""The SEC client (app/scraper/sec_client.py): CIK helpers, the retry after a stall, the page sizes
browse-edgar accepts.

Part of test_sec_edgar.py until 2026-11-25, when sec_edgar.py was split into modules."""
from unittest.mock import patch, MagicMock

import pytest

from app.scraper.sec_client import _cik_int, _cik_from_accession, _filing_index_url
# the module, not the class: a test class imported by name would be collected here a second time
from tests.scraper import test_sec_lookup


class TestCikHelpers:
    def test_cik_from_accession(self):
        assert _cik_from_accession("0001318605-22-000032") == "0001318605"

    def test_cik_int_strips_zeros(self):
        assert _cik_int("0001318605") == "1318605"

    def test_filing_index_url(self):
        # Readable EDGAR filing index page: /data/{cik-int}/{acc-nodash}/{acc}-index.htm
        assert _filing_index_url("0000320193", "0001104659-24-021466") == (
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000110465924021466/0001104659-24-021466-index.htm"
        )

    def test_filing_index_url_none_when_incomplete(self):
        assert _filing_index_url("", "0001104659-24-021466") is None
        assert _filing_index_url("320193", "") is None


class TestStallRetry:
    """sec.gov leaves ~1 request in 30 hanging until the read timeout, and the
    next request, on a new connection, answers at once (findings item 38). Every
    SEC request gets one retry after a stall or a dropped connection, and the
    three calls that went around _response now go through it."""

    @staticmethod
    def _client(*answers):
        import httpx
        calls = []

        def get(url, params=None):
            calls.append((url, params))
            a = answers[len(calls) - 1]
            if isinstance(a, Exception):
                raise a
            if isinstance(a, int):
                return httpx.Response(a, headers={"Retry-After": "0"},
                                      request=httpx.Request("GET", url))
            return httpx.Response(200, text=a, request=httpx.Request("GET", url))
        client = MagicMock()
        client.get.side_effect = get
        return client, calls

    def _get_text(self, *answers):
        from app.scraper import sec_client
        client, calls = self._client(*answers)
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            return sec_client._get_text("https://www.sec.gov/x"), calls

    @pytest.mark.parametrize("error", ["ReadTimeout", "ReadError", "RemoteProtocolError"])
    def test_a_stall_or_a_dropped_connection_is_retried_once(self, error):
        import httpx
        text, calls = self._get_text(getattr(httpx, error)("stalled"), "ok")
        assert text == "ok" and len(calls) == 2

    def test_a_second_stall_raises(self):
        import httpx
        with pytest.raises(httpx.ReadTimeout):
            self._get_text(httpx.ReadTimeout("stalled"), httpx.ReadTimeout("again"), "never")

    def test_one_retry_in_all_a_stall_then_a_503_raises(self):
        import httpx
        with pytest.raises(httpx.HTTPStatusError):
            self._get_text(httpx.ReadTimeout("stalled"), 503, "never")

    def test_a_connect_error_is_not_retried(self):
        # a refused or unroutable connection is not the stall this retry is for
        import httpx
        with pytest.raises(httpx.ConnectError):
            self._get_text(httpx.ConnectError("down"), "never")

    def test_the_read_timeout_is_ten_seconds(self):
        from app.scraper import sec_client
        with patch.object(sec_client, "_client", None):
            client = sec_client._get_client()
            try:
                assert client.timeout.read == sec_client.READ_TIMEOUT == 10.0
            finally:
                client.close()

    def test_the_tickers_file_is_retried(self):
        from app.scraper import sec_client, sec_lookup
        import httpx
        client, calls = self._client(httpx.ReadTimeout("stalled"), '{"0": {"cik_str": 1}}')
        with patch.object(sec_client, "_get_client", return_value=client), \
             patch.object(sec_lookup, "_tickers_cache", None), patch("time.sleep"):
            assert sec_lookup._get_tickers() == {"0": {"cik_str": 1}}
        assert len(calls) == 2

    def test_the_company_name_browse_is_retried(self):
        from app.scraper import sec_client, sec_lookup
        import httpx
        client, calls = self._client(httpx.ReadTimeout("stalled"), "<feed/>", "<feed/>", "<feed/>")
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            sec_lookup._lookup_edgar_by_name("Gitlab")
        assert [p["type"] for _, p in calls] == ["10-K", "10-K", "20-F", ""]     # "" = any form (item 63)

    def test_the_person_name_browse_is_retried(self):
        from app.scraper import sec_client, sec_lookup
        import httpx
        one = test_sec_lookup.TestPersonCikLookup.ONE
        client, calls = self._client(httpx.ReadTimeout("stalled"), one)
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            assert sec_lookup._lookup_person_ciks("Robyn Denholm") == ["0001242782"]
        assert [p["company"] for _, p in calls] == ["Denholm Robyn", "Denholm Robyn"]


class TestBrowsePageSize:
    """browse-edgar answers HTTP 400 to a `count` outside its own list since
    November 2026 ("Invalid count, Received: 5, Count must be one of: 10, 20,
    …"); the 5 the name searches sent lost all 540 of a rebuild's (findings
    item 132). Every request asks for a page EDGAR accepts, and the caller
    keeps as many entries as it always did."""

    ALLOWED = {"10", "20", "30", "40", "50", "60", "70", "80", "90", "100", "500"}

    @staticmethod
    def _entries(n, shape="list"):
        if shape == "person":
            return "<feed>" + "".join(
                f'<entry><content><company-info name="ARRAY(0x{i})"><cik>{1000 + i:010d}</cik>'
                "</company-info></content></entry>" for i in range(n)) + "</feed>"
        return ('<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(
            f"<entry><id>urn:tag:www.sec.gov:cik={1000 + i:010d}</id></entry>"
            for i in range(n)) + "</feed>")

    def _client(self, text):
        sent = []

        def get(url, params=None):
            sent.append(dict(params or {}))
            resp = MagicMock(status_code=200, text=text, content=text.encode())
            resp.headers = {}
            return resp
        client = MagicMock()
        client.get.side_effect = get
        return client, sent

    @pytest.mark.parametrize("wanted, sent", [
        (1, "10"), (5, "10"), (10, "10"), (11, "20"), (20, "20"), (95, "100"),
        (100, "100"), (101, "500"), (500, "500"), (5000, "500"),
    ])
    def test_the_smallest_page_that_holds_what_is_wanted(self, wanted, sent):
        from app.scraper import sec_client
        assert sec_client.browse_count(wanted) == sent

    def test_the_page_sizes_are_edgars_own_list(self):
        from app.scraper import sec_client
        assert {str(n) for n in sec_client.BROWSE_PAGE_SIZES} == self.ALLOWED

    @pytest.mark.parametrize("form_type, kept", [("10-K", 5), ("20-F", 5), ("", 20)])
    def test_a_company_name_search_asks_an_allowed_page_and_keeps_its_own_number(self, form_type, kept):
        from app.scraper import sec_client, sec_lookup
        client, sent = self._client(self._entries(30))
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            found = sec_lookup._browse_index("Nestle", form_type)
        assert sent[0]["count"] in self.ALLOWED
        assert int(sent[0]["count"]) >= kept        # the page holds what is kept
        assert [cik for cik, _ in found] == [f"{1000 + i:010d}" for i in range(kept)]

    def test_a_person_name_search_asks_an_allowed_page_and_keeps_five(self):
        from app.scraper import sec_client, sec_lookup
        client, sent = self._client(self._entries(9, "person"))
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            found = sec_lookup._lookup_person_ciks("Thomas Murphy")
        assert sent[0]["count"] in self.ALLOWED
        assert found == [f"{1000 + i:010d}" for i in range(5)]

    def test_the_holder_feed_asks_an_allowed_page_and_steps_by_its_own(self):
        from app.scraper import sec_13dg, sec_client
        client, sent = self._client("<feed xmlns='http://www.w3.org/2005/Atom'/>")
        with patch.object(sec_client, "_get_client", return_value=client), patch("time.sleep"):
            list(sec_13dg._ownership_feed_entries("0000320193"))
        assert sent[0]["count"] in self.ALLOWED
        assert sent[0]["count"] == str(sec_13dg.OWNERSHIP_FEED_PAGE)   # the step and the page agree
