"""A parent's newer Schedule 13D/G ends its subsidiary's older one (findings
item 34), against a real ArcadeDB. The DaVita case of 2026-11-01: the old
BlackRock parent (CIK 0001364742, renamed BlackRock Finance, Inc.) reported
5.2 % on 2024-01-31; the new parent BlackRock, Inc. (CIK 0002012383), whose
Exhibit 21 lists the old one, reported 5.58 % on 2025-04-25 — and DaVita
listed both."""
import pytest
from unittest.mock import patch

from app.config import settings

pytestmark = pytest.mark.integration

SID = "sec-edgar"
OLD, NEW, DAVITA = "sec:0001364742", "lei:529900VBK42Y5HHRMD23", "lei:M2XHYMU3TZNEZURC6H66"


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield


def _companies(it_db, *extra):
    for eid, name in ((OLD, "BlackRock Finance, Inc."), (NEW, "BlackRock, Inc."), (DAVITA, "DAVITA INC."),
                      *extra):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company'}})")
    it_db.run_command(f"CREATE (:Source {{id: '{SID}', name: 'SEC EDGAR'}})")


def _statement(owner, owned, filed, pct, since=None, form="13G/A"):
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner_id=owner, owned_id=owned, source_id=SID, ownership_type="minority",
                     file_date=filed, stake_percent=pct, filing_type=form,
                     since_date=since or filed, since_basis="first_reported",
                     source_url=f"https://www.sec.gov/{owner}/{filed}")


def _listed(parent, child, ownership_type="controlling", until=None):
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner_id=parent, owned_id=child, source_id=SID, ownership_type=ownership_type,
                     file_date="2026-02-25", stake_percent=None, filing_type="EX-21",
                     filing_dates_the_stake=False, until=until,
                     source_url="https://www.sec.gov/blk-ex21_1.htm")


def _owners(it_db, owned=DAVITA):
    return {(r["a"], r["until"]): r for r in it_db.run_command(
        "MATCH (a:Entity)-[r:OWNS]->(b:Entity {id: $b}) RETURN a.id AS a, r.until AS until, "
        "r.until_reason AS why, r.stale AS stale, r.stale_reason AS sreason", {"b": owned})}


def _davita(it_db):
    _companies(it_db)
    _listed(NEW, OLD)
    _statement(OLD, DAVITA, "2024-01-31", 5.2, since="2023-12-31")
    it_db.run_command("MATCH (:Entity {id: $o})-[r:OWNS]->(:Entity {id: $d}) SET r.stale = true, "
                      "r.stale_reason = 'not_restated', r.stale_since = '2024-01-31'", {"o": OLD, "d": DAVITA})
    _statement(NEW, DAVITA, "2025-04-25", 5.5831, since="2025-03-31")


def test_the_parents_newer_statement_ends_the_subsidiarys(it_db):
    from app.scraper.sec_writer import close_reported_by_parent
    _davita(it_db)
    assert close_reported_by_parent(owned_id=DAVITA) == 1
    owners = _owners(it_db)
    old = owners[(OLD, "2025-03-31")]                # the parent's first report
    assert old["why"] == "reported_by_parent"
    assert not old["stale"] and old["sreason"] is None
    assert [k for k in owners if k[1] is None] == [(NEW, None)]
    assert close_reported_by_parent(owned_id=DAVITA) == 0          # idempotent


def test_re_reading_the_subsidiarys_filing_updates_the_closed_spell(it_db):
    from app.scraper.sec_writer import close_reported_by_parent
    _davita(it_db)
    close_reported_by_parent(owned_id=DAVITA)
    _statement(OLD, DAVITA, "2024-01-31", 5.2, since="2023-12-31")      # the next scrape
    close_reported_by_parent(owned_id=DAVITA)
    spells = [r["until"] for r in it_db.run_command(
        "MATCH (a:Entity {id: $o})-[r:OWNS]->(b:Entity {id: $d}) RETURN r.until AS until",
        {"o": OLD, "d": DAVITA})]
    assert spells == ["2025-03-31"]                  # the one spell, no twin drawn beside it


def test_the_list_read_after_both_statements_still_ends_it(it_db):
    # a rebuild reads DaVita's owners before BlackRock's Exhibit 21
    from app.scraper import runner
    _companies(it_db)
    it_db.run_command(f"MATCH (e:Entity {{id: '{NEW}'}}) SET e.sec_cik = '0002012383'")
    _statement(OLD, DAVITA, "2024-01-31", 5.2)
    _statement(NEW, DAVITA, "2025-04-25", 5.5831, since="2025-03-31")
    data = {"subsidiaries": [{"name": "BlackRock Finance, Inc.", "jurisdiction": "Delaware"}],
            "form": "10-K", "filing_date": "2026-02-25", "url": "https://www.sec.gov/blk-ex21_1.htm"}
    with patch("app.routers.search.resolve_best_entity", return_value={"id": NEW, "name": "BlackRock, Inc.",
                                                                       "sec_cik": "0002012383"}), \
         patch("app.scraper.runner._upsert_entity_by_name", return_value=OLD), \
         patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=data), \
         patch("app.scraper.sec_edgar.fetch_filer_identity", return_value=(None, None)):
        assert runner.run_sec_ex21("BlackRock", force=True)["status"] == "ok"
    assert _owners(it_db)[(OLD, "2025-03-31")]["why"] == "reported_by_parent"


def test_the_whole_graph_pass(it_db):
    from app.scraper.sec_writer import close_reported_by_parent
    _davita(it_db)
    assert close_reported_by_parent() == 1
    assert (OLD, "2025-03-31") in _owners(it_db)


def test_a_holdings_run_ends_it(it_db):
    # the parent's filer-side holdings run writes the newer statement
    from app.scraper import runner
    _companies(it_db)
    _listed(NEW, OLD)
    _statement(OLD, DAVITA, "2024-01-31", 5.2)
    holding = {"subject_name": "DAVITA INC.", "subject_cik": None, "stake_percent": 5.5831,
               "file_date": "2025-04-25", "filing_type": "13G/A", "since": "2025-03-31",
               "since_basis": "first_reported", "source_url": "https://www.sec.gov/new"}
    with patch("app.scraper.sec_edgar.fetch_filer_name", return_value="BlackRock, Inc."), \
         patch("app.scraper.sec_edgar.fetch_filer_holdings", return_value=[holding]), \
         patch("app.scraper.sec_edgar.fetch_affiliated_managers", return_value=[]), \
         patch("app.scraper.sec_edgar.fetch_filer_country", return_value="US"), \
         patch("app.scraper.sec_edgar.fetch_filer_headquarters", return_value=None), \
         patch("app.scraper.sec_edgar.fetch_filer_website", return_value=None), \
         patch("app.scraper.runner._record_sec_industry"), \
         patch("app.scraper.runner._upsert_entity_by_name",
               side_effect=lambda **kw: NEW if kw.get("cik") == "0002012383" else DAVITA):
        runner.run_sec_holdings("0002012383")
    assert _owners(it_db)[(OLD, "2025-03-31")]["why"] == "reported_by_parent"


class TestWhatStaysOpen:
    def test_a_minority_stake_in_the_filer_is_no_group(self, it_db):
        from app.scraper.sec_writer import close_reported_by_parent
        _companies(it_db)
        _listed(NEW, OLD, ownership_type="minority")
        _statement(OLD, DAVITA, "2024-01-31", 5.2)
        _statement(NEW, DAVITA, "2025-04-25", 5.58)
        assert close_reported_by_parent(owned_id=DAVITA) == 0

    def test_an_ended_link_is_no_group(self, it_db):
        from app.scraper.sec_writer import close_reported_by_parent
        _companies(it_db)
        _listed(NEW, OLD, until="2023-01-01")
        _statement(OLD, DAVITA, "2024-01-31", 5.2)
        _statement(NEW, DAVITA, "2025-04-25", 5.58)
        assert close_reported_by_parent(owned_id=DAVITA) == 0

    def test_the_subsidiarys_own_newer_filing_stays(self, it_db):
        # Vanguard Capital Management's 7.48 % of 2026-04 after the Vanguard
        # Group's 0 % of 2026-03: the disaggregated filer is the current word
        from app.scraper.sec_writer import close_reported_by_parent
        _companies(it_db)
        _listed(NEW, OLD)
        _statement(NEW, DAVITA, "2026-03-26", 6.0)
        _statement(OLD, DAVITA, "2026-04-29", 7.48)
        assert close_reported_by_parent(owned_id=DAVITA) == 0

    def test_a_joint_filing_of_one_day_stays(self, it_db):
        # Berkshire and National Indemnity on one Chevron schedule
        from app.scraper.sec_writer import close_reported_by_parent
        _companies(it_db)
        _listed(NEW, OLD)
        _statement(NEW, DAVITA, "2026-05-15", 4.2)
        _statement(OLD, DAVITA, "2026-05-15", 3.8)
        assert close_reported_by_parent(owned_id=DAVITA) == 0

    def test_a_13f_is_not_compared(self, it_db):
        from app.scraper.sec_writer import close_reported_by_parent
        _companies(it_db)
        _listed(NEW, OLD)
        _statement(OLD, DAVITA, "2024-01-31", 5.2)
        _statement(NEW, DAVITA, "2026-06-30", 8.6, form="13F")
        assert close_reported_by_parent(owned_id=DAVITA) == 0
