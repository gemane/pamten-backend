"""The directors a proxy names whom the insider window misses, written
against a real ArcadeDB (findings item 115). Berkshire Hathaway's window held
2 of its 13 directors: the board gets no stock grants, so most of it files no
Form 3/4 for years. The roster records (`sec_roster.roster_insiders`) are the
shape of the window's, and the seat writer takes them as they are. Only EDGAR
is mocked."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

PROXY = "https://www.sec.gov/Archives/edgar/data/1067983/000119312526106253/d882687ddef14a.htm"
BUFFETT = {"name": "Warren E Buffett", "role": "Director", "title": "", "former": False, "person_cik": "0000315090",
           "since": None, "until": None, "document_type": "4",
           "source_url": "https://www.sec.gov/Archives/edgar/data/1067983/w-index.htm", "source_date": "2026-07-15"}
CHENAULT = {"name": "Kenneth I Chenault", "role": "Director", "title": "", "former": False,
            "person_cik": "0001180524", "since": "2020-05-02", "until": None, "document_type": "3",
            "period_of_report": "2020-05-02", "shares_owned": None, "share_class": None, "holdings": {}, "blocks": [],
            "source_url": "https://www.sec.gov/Archives/edgar/data/1067983/000120919120028427/0001209191-20-028427-index.htm",
            "source_date": "2020-05-12", "roster": PROXY}
HOWARD = {"name": "Howard Buffett", "role": "Director", "title": "", "former": False, "person_cik": "0001253718",
          "since": None, "until": None, "document_type": None, "period_of_report": None, "shares_owned": None,
          "share_class": None, "holdings": {}, "blocks": [], "source_url": PROXY, "source_date": "2026-03-13",
          "roster": PROXY}


def _scrape(window, roster):
    """`scrape_company` for real up to its two insider readers: the window's
    list is given, the roster's is added to it as the scrape does."""
    from app.scraper import runner, sec_edgar, sec_roster
    with patch.object(sec_edgar, "search_company", return_value={"cik": "0001067983", "name": "BERKSHIRE HATHAWAY INC"}), \
         patch.object(sec_edgar, "fetch_former_names", return_value=[]), \
         patch.object(sec_edgar, "fetch_company_lei", return_value=None), \
         patch.object(sec_edgar, "fetch_ownership_filings", return_value=[]), \
         patch.object(sec_edgar, "fetch_executives", return_value=[dict(e) for e in window]), \
         patch.object(sec_roster, "roster_insiders", return_value=[dict(e) for e in roster]) as asked, \
         patch.object(sec_edgar, "fetch_filer_holdings", return_value=[]), \
         patch.object(sec_edgar, "fetch_shares_outstanding_detail", return_value=None), \
         patch("app.scraper.sec_f6.fetch_ads_cusips", return_value={}), \
         patch.object(sec_edgar, "fetch_filer_country", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch.object(sec_edgar, "fetch_insider_holding", return_value=None), \
         patch.object(sec_edgar, "_submissions", return_value={"cik": "1067983", "filings": {"recent": {}}}), \
         patch.object(runner, "_record_sec_industry", return_value=None), \
         patch.object(runner, "get_source_enabled", return_value=True), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        runner.run_scrape_sec_edgar("BERKSHIRE HATHAWAY INC")
    return asked


def _seats(db):
    return {r["pid"]: r for r in db.run_command(
        "MATCH (p:Person)-[r:HAS_ROLE]->(e:Entity {sec_cik: '0001067983'}) "
        "RETURN p.id AS pid, p.full_name AS name, r.role AS role, r.since AS since, r.until AS until, "
        "r.source_url AS url, r.source_id AS src")}


def test_the_proxy_named_directors_get_their_seats_beside_the_windows(it_db):
    asked = _scrape([BUFFETT], [CHENAULT, HOWARD])
    # the roster reader is asked with the issuer and what the window listed
    assert asked.call_args.args[0] == "0001067983"
    assert [e["name"] for e in asked.call_args.args[1]][:1] == ["Warren E Buffett"]
    seats = _seats(it_db)
    assert set(seats) == {"sec:0000315090", "sec:0001180524", "sec:0001253718"}
    chenault, howard = seats["sec:0001180524"], seats["sec:0001253718"]
    assert (chenault["role"], str(chenault["since"])[:10], chenault["until"], chenault["src"]) == \
        ("Director", "2020-05-02", None, "sec-edgar")
    assert chenault["url"] == CHENAULT["source_url"]                 # their own Form 3
    assert (howard["role"], howard["since"], howard["until"], howard["url"]) == ("Director", None, None, PROXY)
    # a seat, not a holding: a Form 3's count is as old as the seat
    assert it_db.run_command("MATCH (p:Person)-[o:OWNS]->(e:Entity {sec_cik: '0001067983'}) RETURN count(o) AS n")[0]["n"] == 0
    claims = it_db.run_command("MATCH (c:Claim) WHERE c.kind = 'role' AND c.from_id = 'sec:0001180524' "
                               "RETURN c.source_id AS s, c.to_id AS t")
    assert [c["s"] for c in claims] == ["sec-edgar"]


def test_a_second_scrape_writes_no_second_seat(it_db):
    _scrape([BUFFETT], [CHENAULT, HOWARD])
    first = _seats(it_db)
    _scrape([BUFFETT], [CHENAULT, HOWARD])
    assert _seats(it_db) == first
    assert it_db.run_command("MATCH (p:Person) RETURN count(p) AS n")[0]["n"] == 3
