"""The DEF 14A reader asks sec.gov through the SEC scraper's one request helper
(findings item 42): the pooled client, the retry, the cache and the status check.
It used to call `httpx.get` itself, with no status check, so a 503 page was
parsed as the proxy statement. No network here."""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.scraper import proxy_statement, sec_edgar

CIK = "0001652044"
DOC = "https://www.sec.gov/Archives/edgar/data/1652044/000130817926000342/goog-20260424.htm"

SUBMISSIONS = json.dumps({"name": "Alphabet Inc.", "filings": {"recent": {
    "form": ["DEF 14A"], "accessionNumber": ["0001308179-26-000342"],
    "primaryDocument": ["goog-20260424.htm"], "filingDate": ["2026-04-24"]}}})

TABLE = """<html><body><table>
<tr><td>Name of Beneficial Owner</td><td>Shares</td><td>% of Total Voting Power</td></tr>
<tr><td>Larry Page</td><td>389,051,160</td><td>27.4</td></tr>
</table></body></html>"""


def _client(doc_answers):
    """submissions → the JSON, the proxy → `doc_answers` in turn (a status
    code, an exception or a body). The company search is sec_edgar's own,
    stubbed in `_fetch` (findings item 47)."""
    calls = []
    doc = iter(doc_answers)

    def get(url, params=None):
        calls.append((url, params))
        req = httpx.Request("GET", url)
        if "submissions" in url:
            return httpx.Response(200, text=SUBMISSIONS, request=req)
        a = next(doc)
        if isinstance(a, Exception):
            raise a
        if isinstance(a, int):
            return httpx.Response(a, text="<html>Service Unavailable</html>",
                                  headers={"Retry-After": "0"}, request=req)
        return httpx.Response(200, text=a, request=req)
    client = MagicMock()
    client.get.side_effect = get
    return client, calls


def _fetch(*doc_answers):
    client, calls = _client(doc_answers)
    with patch.object(sec_edgar, "_get_client", return_value=client), \
         patch.object(sec_edgar, "search_company", return_value={"cik": CIK}), \
         patch.object(settings, "SEC_CACHE_DIR", None), \
         patch("app.objectstore.enabled", return_value=False), patch("time.sleep"):
        return proxy_statement.fetch_proxy_ownership("Alphabet Inc."), calls


def test_every_request_goes_through_the_pooled_client():
    out, calls = _fetch(TABLE)
    assert [u for u, _ in calls] == [f"{proxy_statement.SUBMISSIONS_URL}/CIK{CIK}.json", DOC]
    assert out["filing_url"] == DOC and out["cik"] == CIK


def test_a_503_page_is_not_read_as_the_proxy():
    out, calls = _fetch(503, 503)
    assert out["owners"] == [] and out["error"].startswith("Failed to fetch filing")
    assert len(calls) == 3            # submissions, the proxy and its one retry


def test_a_stall_is_retried():
    out, calls = _fetch(httpx.ReadTimeout("stalled"), TABLE)
    # the proxy arrived (whether its table parses is the parser's business, item 47)
    assert not out.get("error", "").startswith("Failed to fetch") and out["filing_url"] == DOC
    assert len(calls) == 3


def test_a_failed_search_is_an_error_result_not_an_exception():
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("down")
    with patch.object(sec_edgar, "_get_client", return_value=client), \
         patch.object(sec_edgar, "search_company", return_value={"cik": CIK}), \
         patch.object(settings, "SEC_CACHE_DIR", None), \
         patch("app.objectstore.enabled", return_value=False), patch("time.sleep"):
        out = proxy_statement.fetch_proxy_ownership("Alphabet Inc.")
    assert out["owners"] == [] and out["error"].startswith("EDGAR search failed")


def test_the_lost_request_is_counted():
    client, _ = _client([503, 503])
    with patch.object(sec_edgar, "_get_client", return_value=client), \
         patch.object(sec_edgar, "search_company", return_value={"cik": CIK}), \
         patch.object(settings, "SEC_CACHE_DIR", None), \
         patch("app.objectstore.enabled", return_value=False), patch("time.sleep"), \
         sec_edgar.collect_lost_requests() as lost:
        proxy_statement.fetch_proxy_ownership("Alphabet Inc.")
    assert lost == [DOC]


@pytest.mark.parametrize("name", ["HEADERS", "_TIMEOUT"])
def test_no_second_http_setup_is_left(name):
    assert not hasattr(proxy_statement, name)
