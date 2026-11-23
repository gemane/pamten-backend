"""A filer's record is requested once per scrape (findings item 135).

Of 774 records read for 48 targets, 663 had been read a moment before in the
same scrape — a holder's own record once per holding (Bank of New York Mellon's
87 times). `one_filer_record_per_scrape` memoises `_submissions` for the block;
the next scrape reads anew, so it sees the filings made since."""
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.scraper import runner, sec_edgar

APPLE, BNY = "0000320193", "0001390777"


@pytest.fixture
def edgar():
    """`_get` answering each request with a NEW document, and the URLs asked."""
    asked: list[str] = []

    def get(url, params=None):
        asked.append(url)
        return {"name": f"document {len(asked)}", "filings": {"recent": {"form": []}}}
    with patch.object(sec_edgar, "_get", side_effect=get):
        yield asked


def test_outside_a_scrape_every_read_is_a_request(edgar):
    a, b = sec_edgar._submissions(APPLE), sec_edgar._submissions(APPLE)
    assert len(edgar) == 2 and a["name"] != b["name"]


def test_inside_a_scrape_a_record_is_requested_once(edgar):
    with sec_edgar.one_filer_record_per_scrape():
        a = sec_edgar._submissions(APPLE)
        assert sec_edgar._submissions(APPLE) is a
        assert sec_edgar._submissions("320193") is a          # the same filer, unpadded
        other = sec_edgar._submissions(BNY)
    assert edgar == [f"{sec_edgar.SUBMISSIONS_URL}/CIK{APPLE}.json", f"{sec_edgar.SUBMISSIONS_URL}/CIK{BNY}.json"]
    assert other["name"] != a["name"]


def test_the_next_scrape_reads_anew(edgar):
    # the reason a process-long cache was refused: a later scrape must see the
    # filings made since the earlier one
    with sec_edgar.one_filer_record_per_scrape():
        first = sec_edgar._submissions(APPLE)
    with sec_edgar.one_filer_record_per_scrape():
        second = sec_edgar._submissions(APPLE)
    assert len(edgar) == 2 and second["name"] != first["name"]
    assert sec_edgar._filer_records.get() is None


def test_a_nested_block_shares_the_outer_memo(edgar):
    with sec_edgar.one_filer_record_per_scrape():
        a = sec_edgar._submissions(APPLE)
        with sec_edgar.one_filer_record_per_scrape():
            assert sec_edgar._submissions(APPLE) is a
            sec_edgar._submissions(BNY)
        # the inner block's end leaves the outer memo standing
        assert sec_edgar._submissions(BNY)["name"] == "document 2"
        assert sec_edgar._submissions(APPLE) is a
    assert len(edgar) == 2


def test_a_failed_read_is_not_kept():
    calls = []

    def get(url, params=None):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return {"name": "Apple Inc."}
    with patch.object(sec_edgar, "_get", side_effect=get), sec_edgar.one_filer_record_per_scrape():
        with pytest.raises(httpx.ConnectError):
            sec_edgar._submissions(APPLE)
        assert sec_edgar._submissions(APPLE) == {"name": "Apple Inc."}
        sec_edgar._submissions(APPLE)
    assert len(calls) == 2


def test_the_memo_ends_when_the_scrape_fails(edgar):
    with pytest.raises(RuntimeError):
        with sec_edgar.one_filer_record_per_scrape():
            sec_edgar._submissions(APPLE)
            raise RuntimeError("the scrape broke")
    assert sec_edgar._filer_records.get() is None
    sec_edgar._submissions(APPLE)
    assert len(edgar) == 2


def test_the_readers_that_asked_directly_share_it(edgar):
    # the name lookup, the Form 3/4 window and the 8-K departures built the
    # URL themselves and would have passed the memo by (the sibling path)
    with sec_edgar.one_filer_record_per_scrape():
        sec_edgar._submissions(APPLE)
        assert sec_edgar._registrant(APPLE, None)[0] == "document 1"
        assert sec_edgar.fetch_departures(APPLE, ["Tim Cook"]) == []
    assert len(edgar) == 1


def test_no_reader_builds_the_record_url_itself():
    import pathlib
    import re
    src = pathlib.Path(sec_edgar.__file__).read_text()
    built = re.findall(r"SUBMISSIONS_URL\}/CIK\{", src)
    assert len(built) == 1, "a filer record is read through _submissions, or the scrape's memo misses it"


def test_a_company_scrape_opens_it(edgar):
    def scrape(name, limit, cik):
        return {"names": [sec_edgar._submissions(APPLE)["name"] for _ in range(3)]}
    with patch.object(sec_edgar, "_scrape_company", side_effect=scrape):
        out = sec_edgar.scrape_company("Apple Inc.", cik=APPLE)
    assert out == {"names": ["document 1"] * 3} and len(edgar) == 1
    assert sec_edgar._filer_records.get() is None


def test_the_runner_shares_it_between_the_scrape_and_its_writer(edgar):
    # the writer's lookups come after scrape_company returned, inside the run
    def scrape_and_write(company, country):
        with sec_edgar.one_filer_record_per_scrape():        # the scrape's own block
            sec_edgar._submissions(BNY)
        sec_edgar._submissions(BNY)                          # the writer, afterwards
        return {"status": "ok"}
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch.object(runner, "get_source_enabled", return_value=True), \
         patch.object(runner, "_scrape_sec_edgar", side_effect=scrape_and_write):
        fn = getattr(runner.run_scrape_sec_edgar, "__wrapped__", runner.run_scrape_sec_edgar)
        out = fn("Bank Of New York Mellon Corp")
    assert out["status"] == "ok" and len(edgar) == 1
