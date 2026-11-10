"""A 13D/G holding ends when the issuer's own ownership table no longer names
its holder (findings item 99), against a real ArcadeDB. Sirius XM's 2026 proxy
lists Berkshire Hathaway (37.1 %), John C. Malone and Vanguard as its 5 %
holders — not Apollo (16.16 %, 13D/A 2003) or CNA (5.6 %, 2001), whose
statements stayed live and were summed."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

SID = "sec-edgar"
SIRIUS = "lei:54930055RWDMVXAGZK98"
APOLLO, CNA, BRK, BUFFETT, WELL, LATE, BLKF, BLK, NICO = (
    "sec:0001068331", "sec:0000021175", "lei:5493000C01ZX7D35SD85", "sec:0000315090",
    "sec:0000902219", "sec:0000000001", "sec:0001364742", "lei:529900VBK42Y5HHRMD23", "sec:0000070866")
FILING = {"form": "DEF 14A", "date": "2026-04-10",
          "url": "https://www.sec.gov/Archives/edgar/data/908937/000110465926041777/tm261591-1_def14a.htm"}
ROWS = [{"name": "Berkshire Hathaway Inc.", "percents": [37.1]},
        {"name": "The Vanguard Group", "percents": [5.4]},
        {"name": "John C. Malone", "percents": [6.6]},
        {"name": "BlackRock, Inc.", "percents": [5.1]},
        {"name": "Gregory B. Maffei", "percents": [2.29]}]
#: what each statement's cover pages name (the filing is not read in the test)
PERSONS = {APOLLO: ["Apollo Investment Fund IV, L.P."], CNA: ["CNA Financial Corp"],
           BUFFETT: ["Warren E. Buffett", "Berkshire Hathaway Inc."], WELL: ["Wellington Management"],
           NICO: ["National Indemnity Company"]}


def _url(owner):
    return f"https://www.sec.gov/Archives/edgar/data/1/0000000000{owner[-6:]}/x-{owner[-6:]}-index.htm"


def _seed(it_db):
    it_db.run_command(f"CREATE (:Entity {{id: '{SIRIUS}', name: 'SIRIUS XM HOLDINGS INC.', type: 'company', "
                      "sec_cik: '0000908937'})")
    for eid, name in ((APOLLO, "Apollo Investment Fund Iv Lp"), (CNA, "Cna Financial Corp"),
                      (BRK, "BERKSHIRE HATHAWAY INC"), (WELL, "WELLINGTON MANAGEMENT GROUP LLP"),
                      (LATE, "Late Holder LLC"), (BLKF, "BlackRock Finance, Inc."), (BLK, "BlackRock, Inc."),
                      (NICO, "NATIONAL INDEMNITY CO")):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company'}})")
    it_db.run_command(f"CREATE (:Person {{id: '{BUFFETT}', full_name: 'Warren Buffett'}})")
    it_db.run_command(f"CREATE (:Source {{id: '{SID}', name: 'SEC EDGAR'}})")
    for owner, filed, pct, form in ((APOLLO, "2003-03-12", 16.16, "13D/A"), (CNA, "2001-02-13", 5.6, "13G"),
                                    (BRK, "2025-09-30", 37.1, "13G/A"), (BUFFETT, "2025-02-14", 34.963, "13G/A"),
                                    (WELL, "2013-02-14", 2.7, "13G/A"), (LATE, "2026-03-01", 6.0, "13G"),
                                    (BLKF, "2024-01-31", 5.2, "13G/A"), (NICO, "2024-02-14", 6.0, "13G")):
        _statement(owner, filed, pct, form)
    # BlackRock Finance is BlackRock, Inc.'s subsidiary (its Exhibit 21)
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner_id=BLK, owned_id=BLKF, source_id=SID, ownership_type="controlling",
                     file_date="2026-02-25", stake_percent=None, filing_type="EX-21",
                     filing_dates_the_stake=False, source_url="https://www.sec.gov/blk-ex21.htm")
    # National Indemnity is Berkshire's: a name with no word of its parent's
    _upsert_owns_sec(owner_id=BRK, owned_id=NICO, source_id=SID, ownership_type="controlling",
                     file_date="2026-02-25", stake_percent=None, filing_type="EX-21",
                     filing_dates_the_stake=False, source_url="https://www.sec.gov/brk-ex21.htm")


def _statement(owner, filed, pct, form):
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner_id=owner, owned_id=SIRIUS, source_id=SID, ownership_type="minority",
                     file_date=filed, stake_percent=pct, filing_type=form,
                     since_date=filed, since_basis="first_reported", source_url=_url(owner),
                     owner_label="Person" if owner == BUFFETT else "Entity")


def _persons(url):
    return next((v for k, v in PERSONS.items() if url == _url(k)), [])


def _owners(it_db):
    return {r["a"]: r for r in it_db.run_command(
        "MATCH (a)-[r:OWNS]->(b:Entity {id: $b}) RETURN a.id AS a, r.until AS until, "
        "r.until_reason AS why, r.until_source_url AS url, r.stale AS stale", {"b": SIRIUS})}


def _close(rows=ROWS):
    from app.scraper.sec_writer import close_absent_from_table
    with patch("app.scraper.sec_writer.statement_persons", side_effect=_persons) as read:
        return close_absent_from_table(SIRIUS, FILING, rows), read


def test_the_holders_the_table_no_longer_names_end_on_its_date(it_db):
    _seed(it_db)
    n, read = _close()
    owners = _owners(it_db)
    assert n == 2
    for gone in (APOLLO, CNA):
        assert (owners[gone]["until"], owners[gone]["why"], owners[gone]["url"]) == \
            ("2026-04-10", "not_in_issuer_table", FILING["url"])
        assert not owners[gone]["stale"]
    assert owners[BRK]["until"] is None          # named
    assert owners[BUFFETT]["until"] is None      # his statement names Berkshire, which the table lists
    assert owners[BLKF]["until"] is None         # its parent is named (and shares its word)
    assert owners[NICO]["until"] is None         # its parent Berkshire is named
    assert owners[WELL]["until"] is None         # under 5 %: the table cannot contradict it
    assert owners[LATE]["until"] is None         # filed after the table's record date
    # a statement is read only when its holder's own names are not in the table: Apollo, CNA and
    # Buffett (the Sirius table lists Berkshire, not him — his cover page names Berkshire)
    assert sorted(c.args[0] for c in read.call_args_list) == sorted([_url(APOLLO), _url(CNA), _url(BUFFETT)])
    assert _close()[0] == 0                       # idempotent


def test_a_table_without_a_5_percent_holder_is_no_evidence(it_db):
    _seed(it_db)
    n, read = _close([{"name": "Gregory B. Maffei", "percents": [2.29]}])
    assert n == 0 and read.call_count == 0
    assert all(o["until"] is None for o in _owners(it_db).values())


def test_a_statement_that_cannot_be_read_is_kept(it_db):
    _seed(it_db)
    from app.scraper.sec_writer import close_absent_from_table
    with patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        assert close_absent_from_table(SIRIUS, FILING, ROWS) == 0


def test_the_groups_reporting_person_keeps_the_holding(it_db):
    # a holder listed only by the group's controlling person: Berkshire's
    # statement names Warren E. Buffett, and Occidental's table lists him alone
    _seed(it_db)
    rows = [{"name": "Warren E. Buffett", "percents": [32.43]}]
    PERSONS[BRK] = ["Berkshire Hathaway Inc.", "Warren E. Buffett"]
    try:
        _close(rows)
    finally:
        PERSONS.pop(BRK)
    owners = _owners(it_db)
    assert owners[BRK]["until"] is None and owners[BUFFETT]["until"] is None
    assert owners[APOLLO]["until"] == "2026-04-10"


def test_re_reading_the_filing_updates_the_closed_spell(it_db):
    _seed(it_db)
    _close()
    _statement(APOLLO, "2003-03-12", 16.16, "13D/A")
    _close()
    spells = [(r["until"], r["why"]) for r in it_db.run_command(
        "MATCH (a:Entity {id: $o})-[r:OWNS]->(b:Entity {id: $s}) RETURN r.until AS until, r.until_reason AS why",
        {"o": APOLLO, "s": SIRIUS})]
    assert spells == [("2026-04-10", "not_in_issuer_table")]


def test_the_whole_graph_pass(it_db):
    _seed(it_db)
    from app.scraper.sec_writer import close_absent_from_tables
    subs = {"cik": "908937", "filings": {"recent": {"form": ["DEF 14A"], "filingDate": ["2026-04-10"],
            "accessionNumber": ["0001104659-26-041777"], "primaryDocument": ["tm261591-1_def14a.htm"]}}}
    with patch("app.scraper.sec_edgar._submissions", return_value=subs), \
         patch("app.scraper.proxy_statement.latest_table_filing", return_value=FILING), \
         patch("app.scraper.sec_edgar._get_text", return_value="<html></html>"), \
         patch("app.scraper.proxy_statement.read_holder_rows", return_value=ROWS), \
         patch("app.scraper.sec_writer.statement_persons", side_effect=_persons):
        res = close_absent_from_tables()
    assert (res["companies"], res["tables"], res["read"], res["failed"], res["closed"]) == (1, 1, 1, 0, 2)
    assert res["lines"] == ["SIRIUS XM HOLDINGS INC.: 2 closed — not in its DEF 14A of 2026-04-10 (5 holders listed)"]


def test_a_table_of_the_companys_own_directors_is_no_evidence(it_db):
    # Berkshire's 2026 proxy lists Buffett (38.4 %) and its board — not the funds
    # that hold its Class B stock — so BlackRock's 13G/A is not contradicted
    _seed(it_db)
    it_db.run_command(f"MATCH (p:Person {{id: '{BUFFETT}'}}), (e:Entity {{id: '{SIRIUS}'}}) "
                      "CREATE (p)-[:HAS_ROLE {role: 'Chairman', source_id: 'sec-edgar'}]->(e)")
    n, read = _close([{"name": "Warren E. Buffett", "percents": [38.4]},
                      {"name": "Susan A. Buffett", "percents": [0.2]}])
    assert n == 0 and read.call_count == 0
    assert all(o["until"] is None for o in _owners(it_db).values())


def test_an_insiders_statement_is_never_judged(it_db):
    # Dick's 2026 proxy read as its four funds; Edward W. Stack (chairman,
    # 19.69 %) sits in the officers' table the reader missed
    _seed(it_db)
    it_db.run_command("CREATE (:Person {id: 'p-stack', full_name: 'Edward W. Stack'})")
    it_db.run_command(f"MATCH (p:Person {{id: 'p-stack'}}), (e:Entity {{id: '{SIRIUS}'}}) "
                      "CREATE (p)-[:HAS_ROLE {role: 'Chairman', source_id: 'sec-edgar'}]->(e)")
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner_id="p-stack", owned_id=SIRIUS, source_id=SID, ownership_type="minority",
                     file_date="2025-02-17", stake_percent=19.69, filing_type="13G/A", owner_label="Person",
                     since_date="2025-02-17", since_basis="first_reported", source_url=_url("p-stack"))
    PERSONS["p-stack"] = ["Edward W. Stack"]
    try:
        _close()
    finally:
        PERSONS.pop("p-stack")
    assert _owners(it_db)["p-stack"]["until"] is None
    assert _owners(it_db)[APOLLO]["until"] == "2026-04-10"
