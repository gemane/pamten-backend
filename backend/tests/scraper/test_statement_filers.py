"""Who filed a Schedule 13D/G about an issuer, from EDGAR's full-text index
(findings item 157): one request lists every such filing with each party's
CIK and name — Newmark's lists Cantor Fitzgerald and Howard Lutnick — where
the 13D/G feed names no filer and every filing would have to be opened."""
from unittest.mock import patch

import httpx

from app.scraper import sec_client, sec_writer

NEWMARK = "0001690680"


def _hit(day, form, filer_cik, filer_name, accession):
    return {"_source": {"file_date": day, "form": form, "adsh": accession,
                        "ciks": [NEWMARK, filer_cik],
                        "display_names": [f"NEWMARK GROUP, INC.  (NMRK)  (CIK {NEWMARK})", f"{filer_name}  (CIK {filer_cik})"]}}


def _page(hits, total=None):
    return {"hits": {"total": {"value": len(hits) if total is None else total}, "hits": hits}}


def _filers(*pages):
    asked = []

    def get(url, params=None):
        asked.append((url, dict(params or {})))
        return pages[len(asked) - 1]
    with patch.object(sec_client, "_get", side_effect=get):
        return sec_writer._statement_filers(NEWMARK), asked


def test_each_filer_with_its_newest_statement_newest_first():
    got, asked = _filers(_page([
        _hit("2026-11-05", "SCHEDULE 13G/A", "0002100121", "VANGUARD PORTFOLIO MANAGEMENT LLC", "0002100121-26-000900"),
        _hit("2025-10-06", "SCHEDULE 13D/A", "0001024896", "CANTOR FITZGERALD, L. P.", "0001024896-25-000010"),
        _hit("2024-02-01", "SC 13D/A", "0001024896", "CANTOR FITZGERALD, L. P.", "0001024896-24-000003"),
        _hit("2025-10-06", "SCHEDULE 13D/A", "0001250975", "LUTNICK HOWARD W", "0001250975-25-000004"),
    ]))
    assert [(f["name"], f["file_date"], f["form_type"]) for f in got] == [
        ("VANGUARD PORTFOLIO MANAGEMENT LLC", "2026-11-05", "SCHEDULE 13G/A"),
        ("CANTOR FITZGERALD, L. P.", "2025-10-06", "SCHEDULE 13D/A"),      # its newest, not the 2024 one
        ("LUTNICK HOWARD W", "2025-10-06", "SCHEDULE 13D/A")]
    cantor = got[1]
    assert (cantor["cik"], cantor["accession"]) == ("0001024896", "0001024896-25-000010")
    assert cantor["index_url"] == sec_client._filing_index_url(NEWMARK, "0001024896-25-000010")
    # one request, by the issuer's CIK, for both namings of the schedule
    [(url, params)] = asked
    assert url == sec_client.SEARCH_URL and params["ciks"] == NEWMARK and params["from"] == 0
    assert {"SC 13D", "SC 13G/A", "SCHEDULE 13D/A", "SCHEDULE 13G"} <= set(params["forms"].split(","))


def test_the_issuer_itself_is_no_filer_of_its_statements():
    got, _ = _filers(_page([_hit("2025-10-06", "SCHEDULE 13D/A", "0001024896", "CANTOR FITZGERALD, L. P.", "a-1")]))
    assert [f["cik"] for f in got] == ["0001024896"]


def test_a_long_list_is_read_page_by_page_up_to_its_cap():
    first = [_hit(f"2026-01-{d:02d}", "SCHEDULE 13G", f"{d:010d}", f"Filer {d}", f"a-{d}") for d in range(1, 29)]
    second = [_hit("2025-06-01", "SC 13G", "0000000099", "Late Filer", "b-1")]
    got, asked = _filers(_page(first, total=29), _page(second, total=29))
    assert len(got) == 29 and [p["from"] for _, p in asked] == [0, 28]
    with patch.object(sec_writer, "STATEMENT_INDEX_MAX", 28):
        got, asked = _filers(_page(first, total=500))
    assert len(asked) == 1 and len(got) == 28


def test_an_issuer_nobody_filed_about_is_an_empty_list():
    got, _ = _filers(_page([]))
    assert got == []


def test_an_index_that_does_not_answer_is_not_nobody_filed():
    with patch.object(sec_client, "_get", side_effect=httpx.ReadTimeout("stalled")):
        assert sec_writer._statement_filers(NEWMARK) is None
    error = httpx.HTTPStatusError("500", request=httpx.Request("GET", "u"), response=httpx.Response(500))
    with patch.object(sec_client, "_get", side_effect=error):
        assert sec_writer._statement_filers(NEWMARK) is None


def test_a_hit_without_a_date_or_an_accession_is_passed_over():
    bad = {"_source": {"form": "SC 13G", "ciks": [NEWMARK, "0000000007"], "display_names": ["x", "y"]}}
    got, _ = _filers(_page([bad, _hit("2025-10-06", "SC 13G", "0000000008", "Good", "a-8")]))
    assert [f["name"] for f in got] == ["Good"]
