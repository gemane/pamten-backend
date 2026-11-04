"""The 13D/G holder list reads EDGAR's browse feed past its first page (findings item 49).

The feed lists the SC filings where a company is the subject OR the filer, 100 a
page. Bank of America's first page was 98 of its own filings to 2 about it, and
reached back only nine months; three outbound filings on 2026-11-03 pushed
Vanguard Group's 13G/A of 2026-01-30 to position 101, and the closed holding it
carried vanished between two rebuilds. These tests drive `fetch_ownership_filings`
over a paged feed: real Vanguard filings (GCI Liberty, 10.84 % then 0) behind a
page of the issuer's own filings.
"""
from unittest.mock import patch

import httpx

from app.scraper import sec_edgar

ISSUER = "0002057463"           # GCI Liberty, the subject of the two real Vanguard filings
ZERO_XML = ("https://www.sec.gov/Archives/edgar/data/2057463/"
            "000010290926000394/primary_doc.xml")
HELD_XML = ("https://www.sec.gov/Archives/edgar/data/2057463/"
            "000010290926000024/primary_doc.xml")


def _fixture(name: str) -> str:
    import os
    return open(os.path.join(os.path.dirname(__file__), "fixtures", name)).read()


def _entry(acc: str, date: str, form: str = "SCHEDULE 13G/A") -> str:
    return (f'<entry><category term="{form}"/><content type="text/xml">'
            f'<filing-href>https://x.test/{acc}.htm</filing-href>'
            f'<filing-date>{date}</filing-date>'
            f'<accession-number>{acc}</accession-number>'
            f'</content></entry>')


def _page(entries: list[str]) -> str:
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{"".join(entries)}</feed>'


def _outbound(n: int, start: int = 0) -> list[str]:
    """The issuer's own filings — what crowds a bank's page."""
    return [_entry(f"{ISSUER}-26-{start + i:06d}", "2026-10-01") for i in range(n)]


class _Feed:
    """A fake `_get_text`: browse pages by `start`, documents by URL, 404 otherwise."""

    def __init__(self, pages: list[str], docs: dict | None = None, fail_at: int | None = None):
        self.pages, self.docs, self.fail_at = pages, docs or {}, fail_at
        self.starts: list[int] = []

    def __call__(self, url, params=None):
        if params is not None:                       # the browse call
            start = int(params.get("start", 0))
            assert params["count"] == str(sec_edgar.OWNERSHIP_FEED_PAGE)
            self.starts.append(start)
            index = start // sec_edgar.OWNERSHIP_FEED_PAGE
            if index == self.fail_at:
                raise httpx.ReadTimeout("stalled")
            return self.pages[index] if index < len(self.pages) else _page([])
        if url not in self.docs:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url),
                                        response=httpx.Response(404))
        return self.docs[url]


def _run(feed: _Feed, **kw):
    with patch.object(sec_edgar, "_get_text", side_effect=feed), \
         patch.object(sec_edgar, "fetch_former_names", return_value=[]):
        return sec_edgar.fetch_ownership_filings("GCI Liberty Inc", ISSUER, **kw)


VANGUARD_DOCS = {ZERO_XML: _fixture("13ga_vanguard_exit.xml"),
                 HELD_XML: _fixture("13ga_vanguard_held.xml")}


def test_a_holding_behind_a_full_page_of_the_issuers_own_filings_is_read():
    """Bank of America's case: the exit sits on page one, the holding it closes on page two."""
    feed = _Feed([
        _page(_outbound(99) + [_entry("0000102909-26-000394", "2026-03-26")]),   # 100 entries
        _page([_entry("0000102909-26-000024", "2026-01-07")]),
    ], VANGUARD_DOCS)
    res = _run(feed)
    assert feed.starts == [0, 100]
    assert len(res) == 1
    assert res[0]["stake_percent"] == 10.84
    assert res[0]["until"] == "2026-03-13"          # closed by the exit on page one


def test_a_short_page_is_the_last_one():
    feed = _Feed([_page(_outbound(3) + [_entry("0000102909-26-000024", "2026-01-07")])],
                 VANGUARD_DOCS)
    _run(feed)
    assert feed.starts == [0]


def test_no_further_page_once_enough_holders_are_found():
    feed = _Feed([
        _page([_entry("0000102909-26-000024", "2026-01-07")] + _outbound(99)),
        _page(_outbound(100, start=100)),
    ], VANGUARD_DOCS)
    res = _run(feed, limit=1)
    assert len(res) == 1
    assert feed.starts == [0]


def test_paging_stops_at_the_page_cap():
    feed = _Feed([_page(_outbound(100, start=i * 100)) for i in range(30)])
    assert _run(feed) == []
    assert len(feed.starts) == sec_edgar.OWNERSHIP_FEED_MAX_PAGES


def test_the_filings_opened_stay_within_the_old_budget():
    """Berkshire's own SC 13s go in under filing agents' accession prefixes, so
    they look inbound and are dropped only once opened: paging must not turn
    one page's 100 openings into a thousand."""
    pages = [_page([_entry(f"0000950170-26-{i * 100 + j:06d}", "2026-05-01")
                    for j in range(100)]) for i in range(5)]
    feed = _Feed(pages)
    with patch.object(sec_edgar, "_fetch_filing_index", return_value=(None, None, None)) as index:
        _run(feed)
    assert index.call_count == sec_edgar.OWNERSHIP_MAX_OPENED
    assert len(feed.starts) <= 2


def test_a_failed_later_page_keeps_what_was_read_and_marks_the_list_partial():
    feed = _Feed([
        _page([_entry("0000102909-26-000024", "2026-01-07")] + _outbound(99)),
        _page([]),
    ], VANGUARD_DOCS, fail_at=1)
    token = sec_edgar._failed_sections.set([])
    try:
        res = _run(feed)
        failed = sec_edgar._failed_sections.get()
    finally:
        sec_edgar._failed_sections.reset(token)
    assert [r["stake_percent"] for r in res] == [10.84]
    assert failed == [sec_edgar.SECTION_OWNERS]


def test_a_failed_first_page_loses_the_section():
    feed = _Feed([_page([])], fail_at=0)
    token = sec_edgar._failed_sections.set([])
    try:
        assert _run(feed) == []
        assert sec_edgar._failed_sections.get() == [sec_edgar.SECTION_OWNERS]
    finally:
        sec_edgar._failed_sections.reset(token)
