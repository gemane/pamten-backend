"""
Conformance test — the minimum requirement every HTTP-API scraper must meet:
identify the application (and a contact) via a User-Agent on every request.

Wikimedia and SEC EDGAR both reject/refuse anonymous traffic (we've hit their
403), and it's the baseline courtesy for any polite scraper. This checks the
User-Agent is actually SENT on each scraper's real request path — not merely
declared as a constant — so a new scraper that forgets it fails here.

Note: BODS pulls bulk files from S3 (no identification required) and is gated /
tested separately; the sources that talk to identifying APIs are covered here.
"""
from unittest.mock import patch, MagicMock

import pytest


def _fake_response(json_val=None, text=""):
    r = MagicMock()
    r.json.return_value = {} if json_val is None else json_val
    r.text = text
    r.status_code = 200
    r.raise_for_status.return_value = None
    return r


def _call_wikidata():
    from app.scraper import wikidata
    with patch("httpx.get", return_value=_fake_response({"search": []})) as g, patch("time.sleep"):
        wikidata.search_entity("acme")
    return g


def _call_sec_edgar():
    from app.scraper import sec_edgar
    with patch("httpx.get", return_value=_fake_response({})) as g, patch("time.sleep"):
        sec_edgar._get("https://data.sec.gov/x")
    return g


def _call_open_corporates():
    from app.scraper import open_corporates
    with patch("httpx.get", return_value=_fake_response({})) as g, patch("time.sleep"):
        open_corporates._get("/companies/search")
    return g


# name → a callable that triggers exactly one outbound GET and returns the mock
API_SCRAPERS = {
    "wikidata":         _call_wikidata,
    "sec_edgar":        _call_sec_edgar,
    "open_corporates":  _call_open_corporates,
}


@pytest.mark.parametrize("name", list(API_SCRAPERS))
class TestScraperSendsIdentifyingUserAgent:
    def _sent_user_agent(self, name: str) -> str:
        mock_get = API_SCRAPERS[name]()
        assert mock_get.called, f"{name}: expected an HTTP request"
        headers = mock_get.call_args.kwargs.get("headers") or {}
        return headers.get("User-Agent", "")

    def test_user_agent_is_sent(self, name):
        assert self._sent_user_agent(name), f"{name}: request sent with no User-Agent header"

    def test_user_agent_identifies_the_app(self, name):
        ua = self._sent_user_agent(name)
        assert "Pamten" in ua, f"{name}: User-Agent must identify the app — got {ua!r}"

    def test_user_agent_includes_a_contact(self, name):
        # ToS require a reachable contact so the source can get in touch.
        ua = self._sent_user_agent(name)
        assert "@" in ua or "http" in ua, \
            f"{name}: User-Agent must include a contact (email or URL) — got {ua!r}"
