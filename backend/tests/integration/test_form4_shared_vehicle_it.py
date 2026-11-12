"""A vehicle several insiders report is one owner (findings item 111), against
a real ArcadeDB. America Movil's Form 3s of 2026-03 (the real filings, in
fixtures) by Carlos and Patrick Slim Domit each list the Control Trust, Control
Empresarial and GFI blocks; Vanessa Hajj Slim the Control Empresarial one.
Summed into every member's edge the family held 169 % of Series B. The blocks
are now written once each, from a node named as the filers name the holder,
and each person keeps their own shares. Daniel Hajj Aboumrad reports the
blocks inside "By Spouse" — only a footnote breaks that down, and footnotes
are not read (the user, 2026-11-12) — so his position stays as filed."""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.scraper.sec_edgar import SharesOutstanding, _parse_form34_xml, split_shared_blocks, stake_for

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "scraper" / "fixtures"
DETAIL = SharesOutstanding(60_264_000_000.0, "2026-02-28", {})


def _executives(*who: str) -> list[dict]:
    out = []
    for w in who:
        ex = _parse_form34_xml((FIXTURES / f"amx_form3_{w}.xml").read_text())
        ex.update(source_url=f"https://www.sec.gov/f3-{w}-index.htm", source_date="2026-03-18")
        out.append(ex)
    return out


def _payload(executives: list[dict]) -> dict:
    vehicles = split_shared_blocks(executives, DETAIL)      # what `_scrape_company` does
    for ex in executives:
        if ex.get("holdings") and (pick := stake_for(ex["holdings"], DETAIL)):
            ex.update(pick)
    for v in vehicles:
        v.update(stake_for({v["title"]: v["shares"]}, DETAIL))
    return {"name": "AMERICA MOVIL SAB DE CV/", "cik": "0001129137", "ownership_filings": [],
            "executives": executives, "shared_vehicles": vehicles, "holdings": [], "former_names": [],
            "lei": None}


@pytest.fixture
def graph(it_db):
    it_db.run_command("CREATE (s:Source {id:'sec', name:'SEC EDGAR'})")
    it_db.run_command("CREATE (e:Entity {id:'amx', name:'AMERICA MOVIL SAB DE CV/', type:'company', "
                      "name_normalized:'america movil', search_text:'AMERICA MOVIL', sec_cik:'0001129137'})")
    # a namesake the holder's short name must never land on
    it_db.run_command("CREATE (e:Entity {id:'gfi-group', name:'GFI', type:'company', name_normalized:'gfi', "
                      "search_text:'GFI', sec_cik:'0001302215'})")
    return it_db


def _scrape(payload: dict, holding: dict | None = None):
    from app.scraper import runner, sec_edgar
    with patch.object(sec_edgar, "scrape_company", return_value=payload), \
         patch.object(sec_edgar, "fetch_filer_country", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_headquarters", return_value=None), \
         patch.object(sec_edgar, "fetch_filer_website", return_value=None), \
         patch.object(sec_edgar, "fetch_departures", return_value=[]), \
         patch.object(sec_edgar, "fetch_insider_holding", return_value=holding), \
         patch.object(sec_edgar, "_submissions", return_value={}), \
         patch.object(runner, "get_source_enabled", return_value=True), \
         patch.object(runner.settings, "SCRAPER_ENABLED", True), \
         patch.object(runner.settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        return runner.run_scrape_sec_edgar("America Movil")


def _owners(graph) -> dict[str, tuple]:
    rows = graph.run_command(
        "MATCH (o)-[r:OWNS]->(e:Entity {id:'amx'}) WHERE r.until IS NULL "
        "RETURN o.id AS id, coalesce(o.full_name, o.name) AS name, r.shares AS sh, r.stake_percent AS pct, "
        "r.filing_type AS ft")
    return {r["name"]: (r["id"], r["sh"], r["pct"], r["ft"]) for r in rows}


def test_each_shared_block_is_written_once_from_its_holder(graph):
    _scrape(_payload(_executives("carlos", "patrick", "daniel", "vanessa")))
    owners = _owners(graph)
    shares = {n: v[1] for n, v in owners.items()}
    assert shares["Control Trust"] == 17742515522
    assert shares["Control Empresarial Trust by Control Empresarial"] == 10933355873
    assert shares["GFI"] == 196009735
    assert owners["GFI"][0] != "gfi-group", "found among the issuer's vehicles only, never by name"
    people = {n: s for n, s in shares.items() if n not in ("Control Trust", "GFI",
                                                           "Control Empresarial Trust by Control Empresarial")}
    assert sorted(people.values()) == [2444476, 1242622344, 2325617791, 29824857979]   # Daniel's "By Spouse" stays
    assert all(v[3] == "Form 4" for v in owners.values())
    # family total: 169 % before; the blocks once plus each person's own
    assert 100 < sum(v[2] for v in owners.values()) < 104      # Daniel's spouse block, as filed
    assert sum(v[2] for n, v in owners.items() if n != "Aboumrad Daniel Antonio Hajj") < 57


def test_a_re_read_finds_the_same_vehicles(graph):
    _scrape(_payload(_executives("carlos", "patrick", "vanessa")))
    _scrape(_payload(_executives("carlos", "patrick", "vanessa")))
    n = graph.run_sql("SELECT count(*) AS n FROM Entity WHERE name = 'Control Trust'")[0]["n"]
    assert n == 1
    assert graph.run_sql("SELECT count(*) AS n FROM Entity WHERE name = 'GFI'")[0]["n"] == 2   # ours + the namesake


def test_an_insider_whose_filing_was_only_the_vehicle_keeps_no_edge_of_their_own(graph):
    # before: Vanessa's Form 4 edge carried the Control Empresarial block
    _scrape(_payload(_executives("vanessa")))           # alone: no other reporter, the block is hers as filed
    assert _owners(graph)["Slim Vanessa Hajj"][1] == 10935800349
    only = _executives("vanessa")[0]
    only["blocks"] = [b for b in only["blocks"] if b["indirect"]]
    only["holdings"] = {"Series B Shares": 10933355873.0}
    _scrape(_payload([only, *_executives("carlos")]))
    owners = _owners(graph)
    assert "Slim Vanessa Hajj" not in owners
    assert owners["Control Empresarial Trust by Control Empresarial"][1] == 10933355873


def test_the_seat_lookup_takes_a_known_vehicle_out_of_a_persons_filing(graph):
    _scrape(_payload(_executives("carlos", "patrick")))
    # a director the issuer scan did not list, whose own Form 3 lists the trust
    graph.run_command("CREATE (p:Person {id:'p-x', full_name:'Example Director', type:'person'})")
    graph.run_command("MATCH (p:Person {id:'p-x'}), (e:Entity {id:'amx'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'Director', source_id:'sec'}]->(e)")
    holding = {"shares_owned": 17742615522.0, "share_class": "Series B Shares", "stake_percent": 29.44,
               "shares_outstanding": DETAIL.total, "cik": "0009999999", "source_date": "2026-03-20",
               "source_url": "https://www.sec.gov/f3-x-index.htm",
               "holdings": {"Series B Shares": 17742615522.0},
               "blocks": [{"title": "Series B Shares", "indirect": False, "nature": "", "shares": 100000.0},
                          {"title": "Series B Shares", "indirect": True, "nature": "By Control Trust",
                           "shares": 17742515522.0}]}
    _scrape(_payload(_executives("carlos", "patrick")), holding=holding)
    assert _owners(graph)["Example Director"][1] == 100000


def test_a_company_reporting_only_the_shared_funds_keeps_no_edge_of_its_own(graph):
    # Innovex: Amberjack Capital Partners, L.P. and Innovex Co-Invest Fund, L.P.
    # both report "Amberjack Capital Fund II, L.P" 13,164,573 shares (18.8 %)
    def filer(name, cik, own):
        blocks = [{"title": "Common Stock", "indirect": True, "nature": "Amberjack Capital Fund II, L.P",
                   "shares": 13164573.0}]
        if own:
            blocks.append({"title": "Common Stock", "indirect": False, "nature": "", "shares": own})
        return {"name": name, "role": "10% Owner", "person_cik": cik, "blocks": blocks,
                "holdings": {"Common Stock": sum(b["shares"] for b in blocks)},
                "shares_owned": sum(b["shares"] for b in blocks), "share_class": "Common Stock",
                "source_url": f"https://www.sec.gov/f4-{cik}-index.htm", "source_date": "2026-05-01"}
    graph.run_command("CREATE (e:Entity {id:'sec:0001940001', name:'Amberjack Capital Partners, L.P.', "
                      "type:'company', sec_cik:'0001940001', name_normalized:'amberjack capital partners'})")
    graph.run_command("MATCH (a:Entity {id:'sec:0001940001'}), (b:Entity {id:'amx'}) CREATE (a)-[:OWNS "
                      "{source_id:'sec', filing_type:'Form 4', shares:13164573, stake_percent:18.8}]->(b)")
    detail = SharesOutstanding(70_108_000.0, "2026-04-30", {})
    executives = [filer("Amberjack Capital Partners, L.P.", "0001940001", 0),
                  filer("INNOVEX CO-INVEST FUND, L.P.", "0001940002", 5000.0)]
    vehicles = split_shared_blocks(executives, detail)
    for ex in executives:
        if ex["holdings"] and (pick := stake_for(ex["holdings"], detail)):
            ex.update(pick)
    for v in vehicles:
        v.update(stake_for({v["title"]: v["shares"]}, detail))
    _scrape({"name": "AMERICA MOVIL SAB DE CV/", "cik": "0001129137", "ownership_filings": [],
             "executives": executives, "shared_vehicles": vehicles, "holdings": [], "former_names": [],
             "lei": None})
    owners = _owners(graph)
    assert "Amberjack Capital Partners, L.P." not in owners
    assert owners["INNOVEX CO-INVEST FUND, L.P."][1] == 5000
    assert owners["Amberjack Capital Fund II, L.P"][1] == 13164573
