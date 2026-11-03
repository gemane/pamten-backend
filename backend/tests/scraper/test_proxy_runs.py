"""The proxy-statement endpoints honour the scraper switches and leave a run row
(findings item 42). They called the reader and the writer directly: no
SCRAPER_ENABLED / SCRAPER_SEC_EDGAR_ENABLED check, no ScrapeRun row, so a failed
read showed nowhere. No database and no network here."""
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from app.config import settings
from app.scraper import runner, sec_edgar

ROWS: list[dict] = []
FETCHED: list = []


@contextmanager
def _fake_run(source, target, reason=None):
    run = {"source": source, "target": target}
    ROWS.append(run)
    yield run


@pytest.fixture(autouse=True)
def _env():
    ROWS.clear()
    FETCHED.clear()
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.run_log.record_run", _fake_run), \
         patch("app.routers.search.resolve_best_entity",
               return_value={"id": "e-goog", "sec_cik": "0001652044"}):
        yield


def _read(result, lose=0):
    def fetch(company, cik=None):
        FETCHED.append(cik)
        for _ in range(lose):        # a request the reader lost (#528 counts it)
            sec_edgar._request_lost("https://www.sec.gov/x", None, httpx.ReadTimeout("x"), True)
        return result
    with patch("app.scraper.proxy_statement.fetch_proxy_ownership", side_effect=fetch):
        return runner.run_proxy_statement("Alphabet Inc.")


OWNERS = [{"name": "Larry Page", "voting_power_pct": 27.4},
          {"name": "Sundar Pichai", "voting_power_pct": None, "below_one_percent": True}]


class TestTheReadRun:
    def test_the_graphs_cik_is_passed_to_the_reader(self):
        _read({"owners": OWNERS})
        assert FETCHED == ["0001652044"]

    def test_an_ok_read_counts_the_owners_with_a_figure_and_names_the_company(self):
        out = _read({"owners": OWNERS, "filing_date": "2026-04-24"})
        assert out["owners"] == OWNERS
        assert ROWS == [{"source": "sec-proxy", "target": "Alphabet Inc.", "entity_id": "e-goog", "total": 1}]

    def test_no_proxy_is_skipped_not_failed(self):
        _read({"error": "No DEF 14A found on EDGAR for 'Acme'", "owners": []})
        assert ROWS[0]["status"] == "skipped" and ROWS[0]["note"].startswith("No DEF 14A")

    def test_an_unreadable_filing_is_failed(self):
        _read({"error": "Failed to fetch filing: 503", "owners": []})
        assert ROWS[0]["status"] == "failed" and ROWS[0]["note"] == "Failed to fetch filing: 503"

    def test_the_readers_note_reaches_the_row(self):
        _read({"owners": [], "note": "The filing states no voting power per owner"})
        assert "status" not in ROWS[0] and ROWS[0]["note"].startswith("The filing states")

    def test_lost_requests_are_noted(self):
        _read({"owners": OWNERS}, lose=2)
        assert ROWS[0]["note"] == "2 request(s) lost to the network" and "status" not in ROWS[0]


class TestTheWriteRun:
    def _write(self, result):
        with patch("app.scraper.proxy_write.write_proxy_ownership", return_value=result) as w:
            out = runner.run_proxy_write("Alphabet Inc.", "e-goog")
        w.assert_called_once_with("Alphabet Inc.", "e-goog")
        return out

    def test_the_updated_edges_are_the_total(self):
        self._write({"entity_id": "e-goog", "updated": [{"proxy_name": "Larry Page"}], "not_found_in_db": []})
        assert ROWS == [{"source": "sec-proxy-write", "target": "Alphabet Inc.",
                         "entity_id": "e-goog", "total": 1}]

    def test_a_company_not_in_the_graph_is_skipped(self):
        self._write({"owners": [], "db_error": "Company not found in DB: 'Alphabet Inc.'"})
        assert ROWS[0]["status"] == "skipped" and ROWS[0]["entity_id"] == "e-goog"


@pytest.mark.parametrize("flag", ["SCRAPER_ENABLED", "SCRAPER_SEC_EDGAR_ENABLED"])
@pytest.mark.parametrize("call", [lambda: runner.run_proxy_statement("Acme"),
                                  lambda: runner.run_proxy_write("Acme")])
def test_a_switched_off_scraper_refuses_and_writes_no_row(flag, call):
    with patch.object(settings, flag, False):
        with pytest.raises(PermissionError):
            call()
    assert ROWS == []


@pytest.mark.parametrize("endpoint,name", [("proxy_statement_run", "run_proxy_statement"),
                                           ("proxy_statement_write", "run_proxy_write")])
def test_the_endpoints_answer_403_when_switched_off(endpoint, name):
    from app.scraper import router
    with patch.object(runner, name, side_effect=PermissionError("Scraper is disabled.")):
        with pytest.raises(HTTPException) as exc:
            getattr(router, endpoint)(company="Acme", _={"id": "u"}) if endpoint.endswith("run") \
                else getattr(router, endpoint)(company="Acme", entity_id=None, _={"id": "u"})
    assert exc.value.status_code == 403


def test_the_run_labels_are_registered():
    from app.scraper.health import _RUNLOG_LABELS
    from app.weekly_report import SEC_ENRICHMENTS
    assert {"sec-proxy", "sec-proxy-write"} <= set(_RUNLOG_LABELS) and "sec-proxy-write" in SEC_ENRICHMENTS
