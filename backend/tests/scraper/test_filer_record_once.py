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
    # every module of the scrapers: the first version of this test read sec_edgar.py
    # alone, and four readers beside it built the URL themselves (2026-11-24)
    built = [f"{path.name}:{n}" for path in sorted(pathlib.Path(sec_edgar.__file__).parent.glob("*.py"))
             for n, line in enumerate(path.read_text().splitlines(), 1)
             if re.search(r"submissions/CIK\{|SUBMISSIONS_URL\}/CIK\{", line)]
    assert len(built) == 1 and built[0].startswith("sec_edgar.py:"), (
        f"a filer record is read through _submissions / _filer_record, or the memo misses it: {built}")


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


# ── A whole-graph pass keeps the records it used last (2026-11-24) ────────────
#
# The table pass asked for 276 filer records on 40 companies, for 125 filers:
# the holders written at company after company — FMR's 20 times, BlackRock's 19.

def _cik(n: int) -> str:
    return f"{n:010d}"


def test_a_pass_keeps_a_record_across_its_companies(edgar):
    with sec_edgar.filer_records_for_a_pass():
        blackrock = sec_edgar._submissions(BNY)
        for issuer in range(1, 6):                      # five companies, the same holder at each
            sec_edgar._submissions(_cik(issuer))
            assert sec_edgar._submissions(BNY) is blackrock
    assert len(edgar) == 6
    assert sec_edgar._filer_records.get() is None


def test_a_pass_keeps_only_the_records_used_last(edgar):
    with sec_edgar.filer_records_for_a_pass(keep=3):
        a = sec_edgar._submissions(_cik(1))
        sec_edgar._submissions(_cik(2))
        sec_edgar._submissions(_cik(3))
        assert sec_edgar._submissions(_cik(1)) is a     # used again: the newest now
        sec_edgar._submissions(_cik(4))                 # … so this one pushes out filer 2, not filer 1
        assert len(sec_edgar._filer_records.get()) == 3
        assert sec_edgar._submissions(_cik(1)) is a
        assert len(edgar) == 4
        sec_edgar._submissions(_cik(2))                 # gone: asked again
        assert len(edgar) == 5
        sec_edgar._submissions(_cik(3))                 # pushed out by filer 2's return
        assert len(edgar) == 6


def test_a_pass_is_bounded_by_default(edgar):
    assert sec_edgar.FILER_RECORDS_KEPT == 64
    with sec_edgar.filer_records_for_a_pass():
        for n in range(1, 101):
            sec_edgar._submissions(_cik(n))
        assert len(sec_edgar._filer_records.get()) == 64
        sec_edgar._submissions(_cik(100))               # among the newest: at hand
        sec_edgar._submissions(_cik(37))                # … the oldest kept
        assert len(edgar) == 100
        sec_edgar._submissions(_cik(36))                # pushed out
        assert len(edgar) == 101


def test_a_scrape_s_memo_is_not_bounded(edgar):
    with sec_edgar.one_filer_record_per_scrape():
        for n in range(1, 101):
            sec_edgar._submissions(_cik(n))
        sec_edgar._submissions(_cik(1))
    assert len(edgar) == 100


def test_the_next_pass_reads_anew(edgar):
    with sec_edgar.filer_records_for_a_pass():
        first = sec_edgar._submissions(APPLE)
    with sec_edgar.filer_records_for_a_pass():
        assert sec_edgar._submissions(APPLE)["name"] != first["name"]
    assert len(edgar) == 2


def test_a_scrape_inside_a_pass_uses_the_pass_s_records(edgar):
    with sec_edgar.filer_records_for_a_pass(keep=2):
        a = sec_edgar._submissions(APPLE)
        with sec_edgar.one_filer_record_per_scrape():
            assert sec_edgar._submissions(APPLE) is a
            assert sec_edgar._filer_records.get().keep == 2
        assert sec_edgar._submissions(APPLE) is a       # the inner block did not end the pass's
    assert len(edgar) == 1


def test_a_pass_that_fails_ends_its_block(edgar):
    with pytest.raises(RuntimeError):
        with sec_edgar.filer_records_for_a_pass():
            sec_edgar._submissions(APPLE)
            raise RuntimeError("the database went away")
    assert sec_edgar._filer_records.get() is None


def test_the_table_pass_opens_the_block_around_all_its_companies():
    from app.scraper import sec_writer
    seen: list = []

    def read(eid, cik, **kw):
        seen.append(sec_edgar._filer_records.get())
        return {"filing": None, "rows": 0, "closed": 0, "written": None, "statements": None}
    rows = [{"id": f"sec:{_cik(n)}", "cik": _cik(n), "name": f"Issuer {n}", "filing_type": "13G"} for n in (1, 2, 3)]
    with patch("app.db.arcadedb.run_sql", return_value=rows), \
         patch("app.scraper.graph_writer._ensure_source", return_value="src"), \
         patch.object(sec_writer, "read_table_and_close", side_effect=read):
        res = sec_writer.close_absent_from_tables()
    assert res["read"] == 3 and len(seen) == 3
    assert seen[0] is not None and seen[0] is seen[1] is seen[2]       # one block for the whole run
    assert seen[0].keep == sec_edgar.FILER_RECORDS_KEPT
    assert sec_edgar._filer_records.get() is None


@pytest.mark.parametrize("fn", ["run_sec_ex21", "run_sec_ex21_history"])
def test_reading_a_subsidiary_list_asks_for_the_filer_record_once(fn, edgar):
    """The readers asked for the filer's own record three times a company."""
    inner = getattr(runner, fn).__wrapped__                     # under the auto-dedup wrapper
    assert inner.__wrapped__ is not None                        # … `_one_filer_record`'s

    @runner._one_filer_record
    def reads_three_times(company):
        return [sec_edgar._submissions(APPLE) for _ in range(3)], sec_edgar._filer_records.get()
    docs, memo = reads_three_times("Apple Inc.")
    assert docs[0] is docs[1] is docs[2] and memo is not None and memo.keep is None
    assert len(edgar) == 1 and sec_edgar._filer_records.get() is None


def test_a_reader_with_its_own_fetch_shares_the_record(edgar):
    """The Exhibit 21 and Form D readers fetch with their own module's `_get`
    (their tests stub it); they built the URL themselves and went past the
    memo — a subsidiary list asked for its filer's record three and four times."""
    own: list[str] = []

    def own_get(url, params=None):
        own.append(url)
        return {"name": "by the reader's own fetch"}
    with sec_edgar.one_filer_record_per_scrape():
        a = sec_edgar._filer_record("320193", own_get)
        assert sec_edgar._filer_record(APPLE, own_get) is a
        assert sec_edgar._submissions(APPLE) is a                   # and the scraper's own reads
    assert own == [f"{sec_edgar.SUBMISSIONS_URL}/CIK{APPLE}.json"] and edgar == []
    sec_edgar._filer_record(APPLE, own_get)                         # outside a block: a request
    assert len(own) == 2


def test_the_subsidiary_list_readers_and_form_d_use_the_one_read_path():
    from app.scraper import sec_ex21, sec_formd
    asked: list[str] = []
    record = {"filings": {"recent": {"form": [], "accessionNumber": [], "filingDate": [], "reportDate": [],
                                     "primaryDocument": [], "fileNumber": []}}}

    def get(url, params=None):
        asked.append(url)
        return record
    with patch.object(sec_ex21, "_get", side_effect=get), patch.object(sec_formd, "_get", side_effect=get), \
         sec_edgar.one_filer_record_per_scrape():
        sec_ex21.annual_filings(APPLE)
        sec_ex21.annual_filings(APPLE)
        sec_ex21._all_filings(APPLE)
        sec_formd.latest_form_d(APPLE)
    assert asked == [f"{sec_edgar.SUBMISSIONS_URL}/CIK{APPLE}.json"]


def test_a_pass_inside_a_scrape_uses_the_scrape_s_memo(edgar):
    with sec_edgar.one_filer_record_per_scrape():
        a = sec_edgar._submissions(APPLE)
        with sec_edgar.filer_records_for_a_pass(keep=1):
            assert sec_edgar._submissions(APPLE) is a
            sec_edgar._submissions(BNY)
            assert sec_edgar._submissions(APPLE) is a           # the scrape's memo, which pushes nothing out
        assert sec_edgar._submissions(BNY)["name"] == "document 2"
    assert len(edgar) == 2
