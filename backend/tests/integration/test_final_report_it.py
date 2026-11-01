"""A 13D/G that is the filer's last report stays a holding, dimmed from its
date (findings item 35), against a real ArcadeDB. BlackRock, Inc.'s 13G/A on
Tesla of 2026-10-27 ticks Item 5 — 4.96 % as of 2026-09-30, "5 percent or
less" — and stood as a current holding."""
import pytest
from unittest.mock import patch

from app.config import settings

pytestmark = pytest.mark.integration

SID = "sec-edgar"
BLK, TSLA = "lei:529900VBK42Y5HHRMD23", "lei:54930043XZGB27CTOV49"
URL = "https://www.sec.gov/Archives/edgar/data/1318605/000201238326003852/0002012383-26-003852-index.htm"


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield


def _companies(it_db):
    for eid, name in ((BLK, "BlackRock, Inc."), (TSLA, "Tesla, Inc.")):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company'}})")
    it_db.run_command(f"CREATE (:Source {{id: '{SID}', name: 'SEC EDGAR'}})")


def _report(filed, pct, final=None, url=URL):
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner_id=BLK, owned_id=TSLA, source_id=SID, ownership_type="minority",
                     file_date=filed, stake_percent=pct, filing_type="13G/A", source_url=url,
                     final_report=final)


def _edge(it_db):
    rows = it_db.run_command(
        "MATCH (:Entity {id: $a})-[r:OWNS]->(:Entity {id: $b}) RETURN r.until AS until, r.stale AS stale, "
        "r.stale_reason AS why, r.stale_since AS since, r.stale_source_url AS url, r.stake_percent AS pct",
        {"a": BLK, "b": TSLA})
    assert len(rows) == 1
    return rows[0]


def test_the_last_report_is_an_open_holding_dimmed_from_its_day(it_db):
    _companies(it_db)
    _report("2026-10-27", 4.96, final="2026-09-30")
    e = _edge(it_db)
    assert e["until"] is None and e["pct"] == 4.96                 # held, as stated
    assert (e["stale"], e["why"], e["since"], e["url"]) == (True, "final_report", "2026-09-30", URL)
    from app.quality import quality_report
    assert quality_report()["contradictions"]["stale_without_reason"] == 0


def test_a_later_report_above_5_percent_clears_all_of_it(it_db):
    _companies(it_db)
    _report("2026-10-27", 4.96, final="2026-09-30")
    _report("2027-02-10", 6.1)
    e = _edge(it_db)
    assert (e["stale"], e["why"], e["since"], e["url"]) == (False, None, None, None)


def test_an_older_final_report_re_read_under_a_newer_statement_dims_nothing(it_db):
    _companies(it_db)
    _report("2027-02-10", 6.1)
    from app.scraper.sec_writer import _mark_final_report
    _mark_final_report(BLK, TSLA, SID, "Entity", "2026-09-30", "2026-10-27", URL)
    assert not _edge(it_db)["stale"]


def test_the_restatement_pass_leaves_it_to_its_reason(it_db):
    from app.scraper.maintenance import mark_not_restated
    _companies(it_db)
    _report("2024-02-12", 4.2, final="2023-12-31")
    mark_not_restated(today="2026-11-01")
    e = _edge(it_db)
    assert (e["why"], e["since"]) == ("final_report", "2023-12-31")


def test_a_holdings_run_dims_it(it_db):
    from app.scraper import runner
    _companies(it_db)
    holding = {"subject_name": "Tesla, Inc.", "subject_cik": None, "stake_percent": 4.96,
               "file_date": "2026-10-27", "filing_type": "13G/A", "event_date": "2026-09-30",
               "final_report": "2026-09-30", "source_url": URL}
    with patch("app.scraper.sec_edgar.fetch_filer_name", return_value="BlackRock, Inc."), \
         patch("app.scraper.sec_edgar.fetch_filer_holdings", return_value=[holding]), \
         patch("app.scraper.sec_edgar.fetch_affiliated_managers", return_value=[]), \
         patch("app.scraper.sec_edgar.fetch_filer_country", return_value="US"), \
         patch("app.scraper.sec_edgar.fetch_filer_headquarters", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_filer_website", return_value=None), \
         patch("app.scraper.runner._record_sec_industry"), \
         patch("app.scraper.runner._upsert_entity_by_name",
               side_effect=lambda **kw: BLK if kw.get("cik") == "0002012383" else TSLA):
        runner.run_sec_holdings("0002012383")
    assert _edge(it_db)["why"] == "final_report"
