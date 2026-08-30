"""Form 13F: institutional holders of one issuer (network mocked).

Fixtures are trimmed from real filings, never invented — Gigafund's Q2-2026
info table (plain XML) and BNP Paribas's (ns1-prefixed, 20 of its 21 SpaceX
rows are options). The 13D/G schema split stayed invisible exactly as long as
fixtures were imagined; these carry the two shapes EDGAR actually serves.
"""
import pathlib
from unittest.mock import patch

import pytest

from app.scraper import sec_edgar
from app.scraper.sec_edgar import fetch_13f_holders

FIX = pathlib.Path(__file__).parent / "fixtures"


def _fts_hit(accession: str, cik: str, name: str, date: str) -> dict:
    return {"_id": f"{accession}:infotable.xml",
            "_source": {"ciks": [cik.zfill(10)], "file_date": date,
                        "display_names": [f"{name}  (CIK {cik.zfill(10)})"]}}


def _serve_13f(fts_hits, docs: dict, total=None):
    """Mock _get (JSON: FTS + index.json) and _get_text (XML docs) together.

    Unknown URLs raise, the way EDGAR does — a fallback that silently serves
    the wrong document makes tests pass for the wrong reason.
    """
    import httpx

    def _get(url, params=None):
        if url == sec_edgar.SEARCH_URL:
            frm = int((params or {}).get("from", 0))
            return {"hits": {"total": {"value": total if total is not None else len(fts_hits)},
                             "hits": fts_hits[frm:frm + 10]}}
        if url.endswith("/index.json"):
            key = url.split("/data/")[1].rsplit("/", 1)[0]
            names = docs.get(f"{key}/index", [])
            return {"directory": {"item": [{"name": n} for n in names]}}
        raise httpx.ConnectError(f"unexpected JSON fetch: {url}")

    def _get_text(url, params=None):
        key = url.split("/data/")[1]
        if key in docs:
            return docs[key]
        raise httpx.ConnectError(f"unexpected text fetch: {url}")

    return patch.object(sec_edgar, "_get", side_effect=_get), \
           patch.object(sec_edgar, "_get_text", side_effect=_get_text)


GIGA_ACC = "0001140361-26-032507"
GIGA_DIR = f"1713833/{GIGA_ACC.replace('-', '')}"
BNP_ACC = "0001166588-26-000008"
BNP_DIR = f"1166588/{BNP_ACC.replace('-', '')}"


def _giga_docs():
    return {f"{GIGA_DIR}/index": ["informationtable.xml", "primary_doc.xml"],
            f"{GIGA_DIR}/informationtable.xml": (FIX / "13f_gigafund_infotable.xml").read_text(),
            f"{GIGA_DIR}/primary_doc.xml": (FIX / "13f_gigafund_primary.xml").read_text()}


class TestFetch13FHolders:
    def test_holders_come_back_with_counts_and_dollars_never_percent(self):
        g, t = _serve_13f([_fts_hit(GIGA_ACC, "1713833", "Gigafund Management Company, LLC",
                                    "2026-08-12")], _giga_docs())
        with g, t:
            out = fetch_13f_holders("SpaceX",
                                    known_names=["Space Exploration Technologies Corp."])
        assert len(out["holders"]) == 1
        h = out["holders"][0]
        assert h["shares"] == 171826745
        assert h["value_usd"] == 29358317651
        assert h["share_class"] == "CLASS A COM STK"
        assert "percent" not in h and "stake_percent" not in h
        assert h["filer_name"] == "Gigafund Management Company, LLC"
        assert h["source_url"].endswith(f"{GIGA_ACC}-index.htm")

    def test_the_period_is_iso_not_edgars_mdy(self):
        g, t = _serve_13f([_fts_hit(GIGA_ACC, "1713833", "Gigafund", "2026-08-12")],
                          _giga_docs())
        with g, t:
            out = fetch_13f_holders("SpaceX", known_names=["Space Exploration Technologies Corp."])
        assert out["holders"][0]["period"] == "2026-06-30"   # filed as 06-30-2026
        assert out["period"] == "2026-06-30"

    def test_the_cusip_is_adopted_for_stamping(self):
        g, t = _serve_13f([_fts_hit(GIGA_ACC, "1713833", "Gigafund", "2026-08-12")],
                          _giga_docs())
        with g, t:
            out = fetch_13f_holders("SpaceX", known_names=["Space Exploration Technologies Corp."])
        assert out["cusip_seen"] == "84615Q103"

    def test_a_near_miss_issuer_name_is_rejected(self):
        # Gigafund's table also holds ANGEL STUDIOS INC; a query about it must
        # not pick up the SpaceX row, and vice versa.
        g, t = _serve_13f([_fts_hit(GIGA_ACC, "1713833", "Gigafund", "2026-08-12")],
                          _giga_docs())
        with g, t:
            out = fetch_13f_holders("Angel Studios", known_names=["Angel Studios Inc"])
        assert len(out["holders"]) == 1
        assert out["holders"][0]["shares"] == 19459882          # the Angel row
        assert out["cusip_seen"] == "034948109"

    def test_option_rows_are_not_holdings(self):
        """20 of BNP's 21 real SpaceX rows are options — shares UNDERLYING a
        contract, owned by nobody. Counting them multiplies a position."""
        docs = {f"{BNP_DIR}/index": ["infotable.xml"],
                f"{BNP_DIR}/infotable.xml": (FIX / "13f_bnp_infotable.xml").read_text()}
        g, t = _serve_13f([_fts_hit(BNP_ACC, "1166588", "BNP PARIBAS FINANCIAL MARKETS",
                                    "2026-08-11")], docs)
        with g, t:
            out = fetch_13f_holders("SpaceX",
                                    known_names=["Space Exploration Technologies Corp"])
        assert len(out["holders"]) == 1
        assert out["holders"][0]["shares"] == 707796            # the Equity row only
        assert out["holders"][0]["share_class"] == "Equity"

    def test_namespaced_tables_parse_like_plain_ones(self):
        # BNP's agent writes ns1:-prefixed XML; Gigafund's writes bare tags.
        # The {*} wildcard must serve both or half the filings silently vanish.
        docs = {f"{BNP_DIR}/index": ["infotable.xml"],
                f"{BNP_DIR}/infotable.xml": (FIX / "13f_bnp_infotable.xml").read_text()}
        g, t = _serve_13f([_fts_hit(BNP_ACC, "1166588", "BNP", "2026-08-11")], docs)
        with g, t:
            out = fetch_13f_holders("SpaceX",
                                    known_names=["Space Exploration Technologies Corp"])
        assert out["holders"], "the ns1: table parsed to nothing"

    def test_a_known_cusip_matches_exactly_and_skips_name_checks(self):
        g, t = _serve_13f([_fts_hit(GIGA_ACC, "1713833", "Gigafund", "2026-08-12")],
                          _giga_docs())
        with g, t:
            out = fetch_13f_holders("whatever name", cusip="84615Q103")
        assert len(out["holders"]) == 1
        assert out["holders"][0]["shares"] == 171826745

    def test_amendments_newest_per_filer_wins(self):
        old_acc = "0001140361-26-000001"
        docs = _giga_docs()
        docs[f"1713833/{old_acc.replace('-', '')}/index"] = []   # would 404 anyway
        # The OLD filing first: EFTS orders by relevance, not date, so
        # "keep the first hit" and "keep the newest" are different rules and
        # only the second one is right.
        hits = [_fts_hit(old_acc, "1713833", "Gigafund", "2026-05-01"),
                _fts_hit(GIGA_ACC, "1713833", "Gigafund", "2026-08-12")]
        g, t = _serve_13f(hits, docs)
        with g, t:
            out = fetch_13f_holders("SpaceX", known_names=["Space Exploration Technologies Corp."])
        assert out["filings_fetched"] == 1                      # one per filer
        assert out["holders"][0]["source_url"].endswith(f"{GIGA_ACC}-index.htm")

    def test_limit_caps_the_fts_paging(self):
        hits = [_fts_hit(f"000114036{i}-26-03250{i % 10}", str(1000 + i), f"Filer {i}",
                         "2026-08-01") for i in range(30)]
        docs = {}
        for i in range(30):
            d = f"{1000 + i}/000114036{i}2603250{i % 10}"
            docs[f"{d}/index"] = []
        g, t = _serve_13f(hits, docs, total=3000)
        with g as get_mock, t:
            out = fetch_13f_holders("X", known_names=["X"], limit=10)
        fts_calls = [c for c in get_mock.call_args_list
                     if c.args[0] == sec_edgar.SEARCH_URL]
        assert len(fts_calls) == 1                              # 10 // page(10)
        assert out["filings_total"] == 3000
        assert out["filings_fetched"] == 10


class TestBackoff:
    def test_a_429_with_retry_after_is_retried_once(self):
        import httpx
        calls = []

        def fake_get(url, params=None):
            calls.append(url)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"},
                                      request=httpx.Request("GET", url))
            return httpx.Response(200, json={"ok": True},
                                  request=httpx.Request("GET", url))

        with patch.object(sec_edgar, "_get_client") as client:
            client.return_value.get.side_effect = fake_get
            out = sec_edgar._get("https://efts.sec.gov/x")
        assert out == {"ok": True} and len(calls) == 2

    def test_a_second_429_raises(self):
        import httpx

        def always_429(url, params=None):
            return httpx.Response(429, headers={"Retry-After": "0"},
                                  request=httpx.Request("GET", url))

        with patch.object(sec_edgar, "_get_client") as client:
            client.return_value.get.side_effect = always_429
            with pytest.raises(httpx.HTTPStatusError):
                sec_edgar._get("https://efts.sec.gov/x")


class TestCusipFromSchedules:
    def test_both_spellings_reach_the_parsed_filing(self):
        for fixture in ("13g_vanguard.xml", "13ga_vanguard_held.xml"):
            raw = (FIX / fixture).read_text()
            parsed = sec_edgar._parse_13dg_xml(raw)
            assert parsed and parsed.get("issuer_cusip"), fixture


class TestTheDateWindow:
    """Relevance ordering serves 2022 filings for a widely-held issuer — the
    Televisa smoke run wrote "current" positions with as-of dates spanning four
    years. The window keeps the search inside the current reporting period."""

    def _fts_params(self, **kw):
        g, t = _serve_13f([_fts_hit(GIGA_ACC, "1713833", "Gigafund", "2026-08-12")],
                          _giga_docs())
        with g as get_mock, t:
            fetch_13f_holders("SpaceX", known_names=["Space Exploration Technologies Corp."],
                              **kw)
        fts = next(c for c in get_mock.call_args_list
                   if c.args[0] == sec_edgar.SEARCH_URL)
        return fts.args[1] if len(fts.args) > 1 else fts.kwargs.get("params", fts.args[1])

    def test_the_default_window_is_one_period_plus_the_deadline(self):
        from datetime import datetime, timedelta, timezone
        params = self._fts_params()
        assert params["dateRange"] == "custom"
        start = datetime.fromisoformat(params["startdt"])
        expect = datetime.now(timezone.utc).date() - timedelta(days=135)
        assert abs((start.date() - expect).days) <= 1
        assert params["enddt"] == datetime.now(timezone.utc).date().isoformat()

    def test_zero_disables_the_window(self):
        params = self._fts_params(window_days=0)
        assert "dateRange" not in params and "startdt" not in params
