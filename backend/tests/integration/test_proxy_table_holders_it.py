"""The 5 % holders an issuer's own ownership table states and no Schedule
13D/G covers (findings item 86), against a real ArcadeDB. Skydance's 10-K/A of
2026-04-24 names Harbor Lights Entertainment with 100 % of the Class A stock,
and EDGAR holds no statement of it: the graph showed nobody owning Skydance.
The table rows are written where the holder is a node the graph has (or an
EDGAR registrant of exactly that name) and no 13D/G speaks for the pair."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

SID = "sec-edgar"
ISSUER = "sec:0002041610"
HARBOR, ELLISON, BLK, BRK, CARD = ("harbor", "sec:ellison", "lei:blk", "lei:brk", "sec:card")
FILING = {"form": "10-K/A", "date": "2026-04-24",
          "url": "https://www.sec.gov/Archives/edgar/data/2041610/000114036126016758/ef20071103_10ka.htm"}
LATER = {"form": "DEF 14A", "date": "2027-04-20", "url": "https://www.sec.gov/later-def14a.htm"}


@pytest.fixture(autouse=True)
def filer_index():
    """EDGAR's index of an issuer's 13D/G filers (`_statement_filers`, findings
    item 157): nobody has filed, unless a test says who."""
    with patch("app.scraper.sec_writer._statement_filers", return_value=[]) as index:
        yield index


def _row(name, pct, cls=None, group=False, names=None):
    return {"name": name, "names": names or [name], "stake_pct": pct, "share_class": cls,
            "voting_power_pct": None, "group": group}


def _seed(it_db):
    it_db.run_command(f"CREATE (:Source {{id: '{SID}', name: 'SEC EDGAR'}})")
    it_db.run_command(f"CREATE (:Entity {{id: '{ISSUER}', name: 'Skydance Corp', type: 'company', "
                      "name_normalized: 'skydance', sec_cik: '0002041610'})")
    for eid, name, key in ((HARBOR, "Harbor Lights Entertainment, Inc.", "harbor lights entertainment"),
                           (BLK, "BlackRock, Inc.", "blackrock"), (BRK, "BERKSHIRE HATHAWAY INC", "berkshire hathaway")):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company', name_normalized: '{key}'}})")
    it_db.run_command(f"CREATE (:Person {{id: '{ELLISON}', full_name: 'David Ellison'}})")


def _write(table, filing=FILING, registrant=None, exited=None):
    from app.scraper import sec_lookup
    from app.scraper.sec_writer import write_table_holders
    with patch.object(sec_lookup, "_browse_index", return_value=[registrant] if registrant else []), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        return write_table_holders(ISSUER, filing, table, SID, {"filings": {"recent": {"form": ["10-K", "10-K/A"]}}},
                                   exited=exited)


def _edges(it_db):
    return {r["a"]: r for r in it_db.run_command(
        f"MATCH (a)-[r:OWNS]->(b:Entity {{id: '{ISSUER}'}}) RETURN a.id AS a, r.stake_percent AS pct, "
        "r.share_class AS cls, r.filing_type AS ft, r.read_from AS rf, r.since AS since, r.since_basis AS basis, "
        "r.event_date AS ev, r.source_url AS url, r.until AS until, r.until_reason AS why")}


def test_the_controlling_holder_is_written_once_from_the_table(it_db):
    _seed(it_db)
    out = _write({"as_of": "2026-02-20", "rows": [
        _row("Harbor Lights Entertainment, Inc.", 100.0, "Class A"),
        _row("David Ellison", 100.0, "Class A"),            # deemed, as Harbor Lights' controller
        _row("Gerald Cardinale", 13.32, "Class B"),         # no node, no registrant: never minted
    ]})
    edges = _edges(it_db)
    assert set(edges) == {HARBOR}
    e = edges[HARBOR]
    assert (e["pct"], e["cls"], e["ft"], e["rf"]) == (100.0, "Class A", "10-K/A", "table")
    assert (str(e["since"])[:10], e["basis"], str(e["ev"])[:10]) == ("2026-02-20", "first_reported", "2026-02-20")
    assert e["url"] == FILING["url"]
    assert out == {"written": 1, "covered": 0, "unresolved": 1, "lost": 0, "shared": 1, "exited": 0, "fund": False}
    assert it_db.run_command("MATCH (p:Person) RETURN count(p) AS n")[0]["n"] == 1
    claim = it_db.run_command(f"MATCH (c:Claim) WHERE c.to_id = '{ISSUER}' RETURN c.filing_type AS filing_type, "
                              "c.stake_percent AS stake_percent, c.read_from AS read_from")
    assert [(c["filing_type"], c["stake_percent"], c["read_from"]) for c in claim] == [("10-K/A", 100.0, "table")]


def test_a_pair_a_statement_speaks_for_is_not_written(it_db):
    from app.scraper.sec_writer import _upsert_owns_sec
    _seed(it_db)
    # Berkshire's 13G/A ended AFTER the table's date: the table is older news
    _upsert_owns_sec(BRK, ISSUER, SID, ownership_type="minority", file_date="2026-02-17", stake_percent=8.2,
                     filing_type="13G/A", until="2026-05-31", source_url="https://www.sec.gov/brk-index.htm")
    # BlackRock's ended long before it: the table is the newer word
    _upsert_owns_sec(BLK, ISSUER, SID, ownership_type="minority", file_date="2019-02-01", stake_percent=6.0,
                     filing_type="13G/A", until="2020-02-01", source_url="https://www.sec.gov/blk-index.htm")
    out = _write({"as_of": "2026-02-20", "rows": [_row("Berkshire Hathaway Inc.", 8.0), _row("BlackRock, Inc.", 6.9)]})
    edges = _edges(it_db)
    assert edges[BRK]["ft"] == "13G/A" and str(edges[BRK]["until"])[:10] == "2026-05-31"
    open_blk = [r for r in it_db.run_command(
        f"MATCH (a {{id: '{BLK}'}})-[r:OWNS]->(b {{id: '{ISSUER}'}}) WHERE r.until IS NULL RETURN r.filing_type AS ft, "
        "r.stake_percent AS pct")]
    assert open_blk == [{"ft": "10-K/A", "pct": 6.9}]
    assert (out["written"], out["covered"]) == (1, 1)


def test_a_holder_a_statement_names_otherwise_is_covered(it_db):
    """"Warren E. Buffett" in the table is Berkshire's 13G — its aliases share the name."""
    from app.scraper.sec_writer import _upsert_owns_sec
    _seed(it_db)
    it_db.run_command("CREATE (:Person {id: 'sec:buffett', full_name: 'Warren E. Buffett'})")
    it_db.run_command(f"MATCH (e:Entity {{id: '{BRK}'}}) SET e.aliases = $a", {"a": ["Warren E. Buffett"]})
    _upsert_owns_sec(BRK, ISSUER, SID, ownership_type="minority", file_date="2026-02-17", stake_percent=9.9,
                     filing_type="13G/A", source_url="https://www.sec.gov/brk-index.htm")
    out = _write({"as_of": None, "rows": [_row("Warren E. Buffett/Berkshire Hathaway Inc.", 9.96,
                                               names=["Warren E. Buffett", "Berkshire Hathaway Inc."])]})
    assert out["covered"] == 1 and out["written"] == 0
    assert set(_edges(it_db)) == {BRK}


def test_a_group_row_is_written_only_as_a_company(it_db):
    _seed(it_db)
    out = _write({"as_of": None, "rows": [
        _row("Entities affiliated with David Ellison", 9.0, group=True, names=["David Ellison"]),
        _row("Entities affiliated with BlackRock, Inc.", 7.0, group=True, names=["BlackRock, Inc."])]})
    assert set(_edges(it_db)) == {BLK}
    assert out["unresolved"] == 1


def test_an_edgar_registrant_of_exactly_that_name_is_written_by_its_cik(it_db):
    _seed(it_db)
    out = _write({"as_of": None, "rows": [_row("4280661 Canada Inc.", 9.6, "Class A")]},
                 registrant=("0001317151", "4280661 Canada Inc."))
    node = it_db.run_command("MATCH (e:Entity {sec_cik: '0001317151'}) RETURN e.id AS id")
    assert len(node) == 1 and node[0]["id"] in _edges(it_db)
    assert out["written"] == 1


def test_with_the_bulk_name_list_a_holder_is_found_without_a_search(it_db, tmp_path):
    """Findings item 135: the live index lists nothing for "Amgen Inc." with its
    dot, so the 5 % row was dropped at every run; the list holds AMGEN INC, and
    the filer's own record confirms the name it bears today."""
    from app.scraper import sec_client, sec_lookup, sec_names
    from app.scraper.sec_writer import write_table_holders
    _seed(it_db)
    raw = b"AMGEN INC:0000318154:\n" + b"".join(b"ZZ FILLER %04d INC:%010d:\n" % (i, i) for i in range(1100))
    index = sec_names.index_path(str(tmp_path), download=lambda: raw)
    record = {"name": "AMGEN INC", "filings": {"recent": {"form": ["SC 13G"], "filingDate": ["2026-02-10"]}}}
    with patch.object(sec_names, "index_path", return_value=index), \
         patch.object(sec_lookup, "_browse_index") as live, \
         patch.object(sec_client, "_submissions", return_value=record), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        out = write_table_holders(ISSUER, FILING, {"as_of": None, "rows": [_row("Amgen Inc.", 9.6, "Class A")]}, SID,
                                  {"filings": {"recent": {"form": ["10-K", "10-K/A"]}}})
    live.assert_not_called()
    node = it_db.run_command("MATCH (e:Entity {sec_cik: '0000318154'}) RETURN e.id AS id")
    assert len(node) == 1 and node[0]["id"] in _edges(it_db)
    assert out["written"] == 1


def test_over_a_form4_count_the_table_wins_and_the_form4_reader_yields(it_db):
    from app.scraper.sec_writer import _upsert_owns_sec, stated_by_13dg
    _seed(it_db)
    _upsert_owns_sec(ELLISON, ISSUER, SID, owner_label="Person", ownership_type="minority", file_date="2026-03-01",
                     stake_percent=0.02, filing_type="Form 4", source_url="https://www.sec.gov/f4-index.htm")
    assert not stated_by_13dg(ELLISON, ISSUER, SID)
    _write({"as_of": "2026-02-20", "rows": [_row("David Ellison", 48.63, "Class B")]})
    e = _edges(it_db)[ELLISON]
    assert (e["pct"], e["ft"], e["cls"]) == (48.63, "10-K/A", "Class B")
    assert stated_by_13dg(ELLISON, ISSUER, SID)


def test_a_newer_table_that_leaves_the_holder_out_ends_it(it_db):
    from app.scraper.sec_writer import close_absent_from_table
    _seed(it_db)
    _write({"as_of": "2026-02-20", "rows": [_row("Harbor Lights Entertainment, Inc.", 100.0, "Class A")]})
    rows = [{"name": "BlackRock, Inc.", "percents": [7.0]}]
    with patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        assert close_absent_from_table(ISSUER, LATER, rows) == 1
    e = _edges(it_db)[HARBOR]
    assert (str(e["until"])[:10], e["why"]) == ("2027-04-20", "not_in_issuer_table")
    # the table that wrote it does not end it
    _write({"as_of": "2026-02-20", "rows": [_row("BlackRock, Inc.", 7.0)]}, filing=LATER)
    with patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        assert close_absent_from_table(ISSUER, LATER, rows) == 0


def test_re_reading_the_same_table_keeps_one_edge(it_db):
    _seed(it_db)
    table = {"as_of": "2026-02-20", "rows": [_row("Harbor Lights Entertainment, Inc.", 100.0, "Class A")]}
    _write(table)
    _write(table)
    n = it_db.run_command(f"MATCH (a {{id: '{HARBOR}'}})-[r:OWNS]->(b {{id: '{ISSUER}'}}) RETURN count(r) AS n")
    assert n[0]["n"] == 1


def test_a_name_two_nodes_bear_is_not_guessed(it_db):
    _seed(it_db)
    it_db.run_command("CREATE (:Entity {id: 'blk2', name: 'BlackRock Inc', type: 'company', name_normalized: 'blackrock'})")
    out = _write({"as_of": None, "rows": [_row("BlackRock, Inc.", 7.0)]})
    assert out["unresolved"] == 1 and _edges(it_db) == {}


def test_a_holder_found_by_its_cik_whose_statement_names_it_otherwise_is_covered(it_db):
    """The registrant's node holds a 13G under another spelling: the pair is the statement's."""
    from app.scraper.sec_writer import _upsert_owns_sec
    _seed(it_db)
    it_db.run_command("CREATE (:Entity {id: 'sec:0001317151', name: 'NUMBERED CO', type: 'company', "
                      "name_normalized: 'numbered', sec_cik: '0001317151'})")
    _upsert_owns_sec("sec:0001317151", ISSUER, SID, ownership_type="minority", file_date="2026-02-01",
                     stake_percent=9.0, filing_type="13G", source_url="https://www.sec.gov/n-index.htm")
    out = _write({"as_of": None, "rows": [_row("4280661 Canada Inc.", 9.6)]},
                 registrant=("0001317151", "4280661 Canada Inc."))
    assert out["covered"] == 1 and _edges(it_db)["sec:0001317151"]["ft"] == "13G"


def test_the_table_that_wrote_a_holding_never_ends_it(it_db):
    """The absence reader spells a row its own way; the same filing is no newer word."""
    from app.scraper.sec_writer import close_absent_from_table
    _seed(it_db)
    _write({"as_of": None, "rows": [_row("Harbor Lights Entertainment, Inc.", 100.0)]}, filing=LATER)
    with patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        assert close_absent_from_table(ISSUER, LATER, [{"name": "BlackRock, Inc.", "percents": [7.0]}]) == 0
    assert _edges(it_db)[HARBOR]["until"] is None


def test_a_row_under_five_percent_is_not_written(it_db):
    _seed(it_db)
    out = _write({"as_of": None, "rows": [_row("BlackRock, Inc.", 4.9)]})
    assert out["written"] == 0 and _edges(it_db) == {}


def _pass(table):
    """The whole-graph pass over the seeded issuer, its table given."""
    from app.scraper.sec_writer import close_absent_from_tables
    subs = {"cik": "2041610", "filings": {"recent": {"form": ["10-K", "10-K/A"]}}}
    with patch("app.scraper.sec_client._submissions", return_value=subs), \
         patch("app.scraper.proxy_statement.latest_table_filing", return_value=FILING), \
         patch("app.scraper.sec_client._get_text", return_value="<html></html>"), \
         patch("app.scraper.sec_lookup._browse_index", return_value=[]), \
         patch("app.scraper.proxy_statement.read_holder_rows",
               return_value=[{"name": r["name"], "percents": [r["stake_pct"]]} for r in table["rows"]]), \
         patch("app.scraper.proxy_statement.read_holder_table", return_value=table), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        return close_absent_from_tables()


def test_the_whole_graph_pass_writes_a_holder_whose_node_came_later(it_db):
    """Findings item 113: Security Benefit Life holds 100 % of Franklin BSP's
    Series H; its node was written 21 minutes after Franklin BSP's own scrape,
    which found nobody of that name. The pass over the finished graph does."""
    from app.scraper.sec_writer import _upsert_owns_sec
    _seed(it_db)
    table = {"as_of": "2026-02-20", "rows": [_row("BlackRock, Inc.", 17.0),
                                             _row("Security Benefit Life Insurance Company", 100.0, "Series H")]}
    # the issuer's own scrape: BlackRock's 13G/A covers its row, the insurer has no node
    _upsert_owns_sec(BLK, ISSUER, SID, ownership_type="minority", file_date="2026-02-01", stake_percent=17.0,
                     filing_type="13G/A", source_url="https://www.sec.gov/blk-index.htm")
    assert _write(table)["unresolved"] == 1 and set(_edges(it_db)) == {BLK}
    # a later scrape writes the insurer under EDGAR's spelling of it
    it_db.run_command("CREATE (:Entity {id: 'sec:0000869772', name: 'Security Benefit Life Insurance Co /Ks/', "
                      "type: 'company', name_normalized: 'security benefit life insurance', sec_cik: '0000869772'})")
    res = _pass(table)
    edges = _edges(it_db)
    assert (edges["sec:0000869772"]["pct"], edges["sec:0000869772"]["cls"], edges["sec:0000869772"]["ft"]) == \
        (100.0, "Series H", "10-K/A")
    assert edges[BLK]["ft"] == "13G/A" and edges[BLK]["until"] is None
    assert (res["companies"], res["written"], res["closed"]) == (1, 1, 0)
    assert res["lines"] == ["Skydance Corp: 1 written — named in its 10-K/A of 2026-04-24, in no 13D/G"]
    # a second pass writes nothing new
    again = _pass(table)
    assert (again["written"], len(_edges(it_db))) == (1, 2)


def test_a_holder_that_left_after_the_table_is_not_written(it_db):
    """Findings item 116: MiniMed's proxy of 2026-08-21 names Medtronic with
    89.86 %; Medtronic's 13G/A of 2026-10-16 states 0 % after the split-off.
    Nobody scraped MiniMed, so the graph held no statement of Medtronic's and
    the whole-graph pass wrote the table's row."""
    _seed(it_db)
    out = _write({"as_of": "2026-02-20", "rows": [_row("Harbor Lights Entertainment, Inc.", 89.86),
                                                  _row("BlackRock, Inc.", 6.9)]},
                 exited=["HARBOR LIGHTS ENTERTAINMENT, INC."])
    assert set(_edges(it_db)) == {BLK}
    assert (out["written"], out["exited"]) == (1, 1)


def test_an_exit_of_another_company_of_the_name_does_not_stop_the_row(it_db):
    _seed(it_db)
    out = _write({"as_of": "2026-02-20", "rows": [_row("BlackRock, Inc.", 6.9)]},
                 exited=["BlackRock Portfolio Management LLC"])
    assert set(_edges(it_db)) == {BLK} and out["exited"] == 0


# ── the whole-graph pass reads the statements filed since the table ─────────

VANGUARD_EXIT = "0000102909-26-000394"      # The Vanguard Group, 0 % on 2026-03-26 (the real filing, on GCI Liberty)
GCI = "0002057463"


def test_the_pass_writes_who_reported_since_the_table_and_not_who_left(it_db):
    """Findings item 116, the user's choice of 2026-11-14 — a company nobody
    scraped: its table (filed 2026-03-12) lists The Vanguard Group and
    BlackRock; since then The Vanguard Group's 13G/A states 0 % and Cox
    Enterprises filed a 13D. Vanguard is not written from the table, BlackRock
    is, and Cox's statement is written as a scrape of the issuer would."""
    from pathlib import Path
    from app.scraper import sec_13dg_xml, sec_client, sec_filer, sec_lookup
    from app.scraper.sec_writer import read_table_and_close
    it_db.run_command(f"CREATE (:Source {{id: '{SID}', name: 'SEC EDGAR'}})")
    it_db.run_command("CREATE (:Entity {id: 'sec:0002057463', name: 'GCI Liberty Inc', type: 'company', "
                      f"name_normalized: 'gci liberty', sec_cik: '{GCI}'}})")
    for eid, name, key in ((BLK, "BlackRock, Inc.", "blackrock"), ("lei:vg", "VANGUARD GROUP INC", "vanguard group")):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company', name_normalized: '{key}'}})")
    fixtures = Path(__file__).parent.parent / "scraper" / "fixtures"
    subs = {"cik": GCI, "filings": {"recent": {
        "form": ["SCHEDULE 13D", "SCHEDULE 13G/A", "DEF 14A", "10-K"],
        "filingDate": ["2026-08-25", "2026-03-26", "2026-03-12", "2026-02-01"],
        "accessionNumber": ["0000000042-26-000001", VANGUARD_EXIT, "0001140361-26-000001", "0002057463-26-000001"],
        "primaryDocument": ["primary_doc.xml", "primary_doc.xml", "def14a.htm", "10k.htm"]}}}
    cox = {"schedule": "13D", "issuer_cik": GCI, "issuer_name": "GCI Liberty Inc", "issuer_cusip": None,
           "class_title": "Common Stock", "event_date": "2026-08-20", "final_report": None, "filer_cik": "0000000042",
           "comment_text": "", "persons": [{"name": "Cox Enterprises, Inc.", "cik": "0000000042", "percent": 28.7,
                                            "aggregate": 2870.0, "sole_voting": 2870.0, "shared_voting": 0.0,
                                            "sole_dispositive": 2870.0, "shared_dispositive": 0.0, "type_code": "CO"}]}
    exit_xml = sec_13dg_xml._parse_13dg_xml((fixtures / "13ga_vanguard_exit.xml").read_text())
    table = {"as_of": "2026-03-02", "rows": [_row("The Vanguard Group", 10.84), _row("BlackRock, Inc.", 6.9)]}
    with patch.object(sec_client, "_submissions", return_value=subs), \
         patch.object(sec_client, "_get_text", return_value="<html></html>"), \
         patch.object(sec_13dg_xml, "_fetch_13dg_xml",
                      side_effect=lambda cik, acc: exit_xml if acc == VANGUARD_EXIT else cox), \
         patch.object(sec_filer, "fetch_former_names", return_value=[]), \
         patch.object(sec_lookup, "_browse_index", return_value=[]), \
         patch("app.scraper.proxy_statement.read_holder_table", return_value=table), \
         patch("app.scraper.proxy_statement.read_holder_rows", return_value=[]), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        res = read_table_and_close("sec:0002057463", GCI, write=True, source_id=SID, statements_for="GCI Liberty Inc")
    edges = {r["n"]: (r["pct"], r["ft"]) for r in it_db.run_command(
        "MATCH (a)-[r:OWNS]->(b:Entity {id: 'sec:0002057463'}) WHERE r.until IS NULL "
        "RETURN a.name AS n, r.stake_percent AS pct, r.filing_type AS ft")}
    assert edges == {"Cox Enterprises, Inc.": (28.7, "13D"), "BlackRock, Inc.": (6.9, "DEF 14A")}
    assert res["statements"]["written"] == 1 and "The Vanguard Group" in res["statements"]["exited"]
    assert (res["written"]["written"], res["written"]["exited"]) == (1, 1)


def test_an_older_exit_found_through_the_index_does_not_stop_the_row(it_db, filer_index):
    from pathlib import Path
    from app.scraper import sec_13dg_xml
    table, subs = _newmark(it_db)
    table["rows"] = [_row("The Vanguard Group", 5.0)]
    it_db.run_command("CREATE (:Entity {id: 'lei:vg', name: 'VANGUARD GROUP INC', type: 'company', "
                      "name_normalized: 'vanguard group', sec_cik: '0000102909'})")
    fixtures = Path(__file__).parent.parent / "scraper" / "fixtures"
    exit_xml = {**sec_13dg_xml._parse_13dg_xml((fixtures / "13ga_vanguard_exit.xml").read_text()),
                "issuer_cik": NEWMARK, "issuer_name": "Newmark Group, Inc."}
    filer_index.return_value = [_index_entry(day="2021-02-10", name="VANGUARD GROUP INC", cik="0000102909",
                                             accession=VANGUARD_EXIT)]
    res, _ = _read_newmark(table, subs, persons=(), statement=exit_xml)
    assert res["older"]["exited"] == [] and _newmark_edges(it_db) == [("VANGUARD GROUP INC", "DEF 14A", None)]


def test_the_issuers_own_scrape_does_not_read_the_statements_again(it_db):
    from app.scraper import sec_client
    from app.scraper.sec_writer import read_table_and_close
    _seed(it_db)
    subs = {"cik": "0002041610", "filings": {"recent": {"form": ["DEF 14A"], "filingDate": ["2026-03-12"],
                                   "accessionNumber": ["0001140361-26-000001"], "primaryDocument": ["def14a.htm"]}}}
    with patch.object(sec_client, "_get_text", return_value="<html></html>"), \
         patch("app.scraper.sec_writer.write_statements_since") as since:
        res = read_table_and_close(ISSUER, "0002041610", subs, write=True, source_id=SID)
    since.assert_not_called()
    assert res["filing"]["date"] == "2026-03-12" and res["statements"] is None


def test_a_registered_funds_statements_are_not_read(it_db):
    """Its table is not written (it lists holders of record), so there is
    nothing the statements since could correct."""
    from app.scraper import sec_client
    from app.scraper.sec_writer import read_table_and_close
    _seed(it_db)
    subs = {"cik": "0002041610", "filings": {"recent": {"form": ["DEF 14A", "N-CSR"], "filingDate": ["2026-03-12", "2026-02-01"],
                                                        "accessionNumber": ["0001140361-26-000001", "0002041610-26-000002"],
                                                        "primaryDocument": ["def14a.htm", "ncsr.htm"]}}}
    with patch.object(sec_client, "_get_text", return_value="<html></html>"), \
         patch("app.scraper.sec_writer.write_statements_since") as since:
        res = read_table_and_close(ISSUER, "0002041610", subs, write=True, source_id=SID, statements_for="Skydance Corp")
    since.assert_not_called()
    assert res["written"]["fund"] is True


# ── A scrape that lost its 13D/G owners (findings item 156) ──────────────────

def _scrape(table, failed_sections, browse=None, exits=None):
    """The issuer's own SEC scrape written to the graph, its table given and
    its 13D/G owners read (`[]`: none filed) or lost to the network."""
    from app.scraper import runner_sec
    subs = {"cik": "2041610", "filings": {"recent": {"form": ["10-K", "10-K/A"]}}}
    data = {"name": "Skydance Corp", "cik": "0002041610", "ownership_filings": [], "executives": [],
            "holdings": [], "failed_sections": failed_sections, "ownership_exits": exits or []}
    with patch("app.scraper.sec_filer.fetch_filer_headquarters", return_value=None), \
         patch("app.scraper.sec_filer.fetch_filer_website", return_value=None), \
         patch("app.scraper.sec_filer.fetch_filer_sic", return_value=None), \
         patch("app.scraper.sec_insiders.fetch_departures", return_value=[]), \
         patch("app.scraper.sec_class_end.class_ends", return_value=[]), \
         patch("app.scraper.sec_client._submissions", return_value=subs), \
         patch("app.scraper.proxy_statement.latest_table_filing", return_value=FILING), \
         patch("app.scraper.sec_client._get_text", return_value="<html></html>"), \
         patch("app.scraper.sec_lookup._browse_index", side_effect=browse or (lambda query, form_type: [])), \
         patch("app.scraper.proxy_statement.read_holder_rows",
               return_value=[{"name": r["name"], "percents": [r["stake_pct"]]} for r in table["rows"]]), \
         patch("app.scraper.proxy_statement.read_holder_table", return_value=table), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        return runner_sec._write_sec_edgar("Skydance Corp", data, "US")


def test_a_scrape_that_lost_its_owners_writes_no_table_holder(it_db):
    # Morgan Stanley, rebuild 2026-11-24: the proxy names "BlackRock, Inc." (7 %),
    # whose 13G/A the scrape could not read — a read timeout on the owners' list.
    # Written from the table, it stood beside the 13G/A the retry read from
    # BlackRock Finance; SmartStop's three (rebuild 2026-11-25b) left their DEF 14A
    # claims behind an edge the statement had taken over.
    _seed(it_db)
    table = {"as_of": "2026-04-17", "rows": [_row("BlackRock, Inc.", 7.0, "Class B"),
                                             _row("Harbor Lights Entertainment, Inc.", 100.0, "Class A")]}
    out = _scrape(table, ["13D/G owners"])
    assert out["status"] == "partial" and out["failed_sections"] == ["13D/G owners"]
    assert _edges(it_db) == {}
    assert it_db.run_command("MATCH (c:Claim) WHERE c.kind = 'owns' RETURN count(c) AS n")[0]["n"] == 0
    # the retry reads the owners (none filed a statement here) and the table is applied
    assert _scrape(table, [])["status"] == "ok"
    edges = _edges(it_db)
    assert set(edges) == {BLK, HARBOR} and {e["ft"] for e in edges.values()} == {"10-K/A"}


def test_a_scrape_that_lost_another_section_applies_its_table(it_db):
    _seed(it_db)
    table = {"as_of": "2026-04-17", "rows": [_row("Harbor Lights Entertainment, Inc.", 100.0, "Class A")]}
    assert _scrape(table, ["insiders"])["status"] == "partial"
    assert set(_edges(it_db)) == {HARBOR}


def test_the_issuers_own_scrape_leaves_out_a_holder_whose_statement_reports_no_position(it_db):
    # Sirius XM, rebuild 2026-11-26b: the scrape read The Vanguard Group's 13G/A
    # exit of 2026-03 and closed its holding, then its DEF 14A of 2026-04-10 —
    # quoting an older 13G — wrote "VANGUARD GROUP INC 5.4 %" (findings item 159)
    _seed(it_db)
    table = {"as_of": "2026-04-02", "rows": [_row("BlackRock, Inc.", 5.4),
                                             _row("Harbor Lights Entertainment, Inc.", 100.0, "Class A")]}
    _scrape(table, [], exits=[("BLACKROCK INC", "2026-03-13")])
    assert set(_edges(it_db)) == {HARBOR}


def test_an_exit_years_before_the_table_does_not_stop_its_row(it_db):
    # Anson Funds on Genius Brands: its exit of 2021-02-10 is the newest statement
    # EDGAR's index lists, but the proxy of 2026 quotes its 13G of 2026-02-17
    _seed(it_db)
    table = {"as_of": "2026-04-02", "rows": [_row("BlackRock, Inc.", 9.9)]}
    _scrape(table, [], exits=[("BLACKROCK INC", "2021-02-10")])
    assert set(_edges(it_db)) == {BLK}


# ── A holder's name search lost to the network (findings item 155) ───────────

CANADA = ("0001317151", "4280661 Canada Inc.")


def _answers(table, lost_readings):
    """EDGAR's name index for the readings of ``table``: lost to a read
    timeout during the readings numbered in ``lost_readings``, the registrant
    otherwise. Returns (the table reader, the name index)."""
    import httpx
    from app.scraper import sec_client
    reading = {"n": 0}

    def read_table(html):
        reading["n"] += 1
        return table

    def browse(query, form_type):
        if reading["n"] in lost_readings:
            sec_client._request_lost("https://www.sec.gov/cgi-bin/browse-edgar", {"company": query},
                                     httpx.ReadTimeout("stalled"), True)
            return None
        return [CANADA]
    return read_table, browse


def _pass_with_a_stall(table, lost_readings):
    from app.scraper.sec_writer import close_absent_from_tables
    read_table, browse = _answers(table, lost_readings)
    subs = {"cik": "2041610", "filings": {"recent": {"form": ["10-K", "10-K/A"]}}}
    with patch("app.scraper.sec_client._submissions", return_value=subs), \
         patch("app.scraper.proxy_statement.latest_table_filing", return_value=FILING), \
         patch("app.scraper.sec_client._get_text", return_value="<html></html>"), \
         patch("app.scraper.sec_lookup._browse_index", side_effect=browse), \
         patch("app.scraper.proxy_statement.read_holder_rows",
               return_value=[{"name": r["name"], "percents": [r["stake_pct"]]} for r in table["rows"]]), \
         patch("app.scraper.proxy_statement.read_holder_table", side_effect=read_table), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        return close_absent_from_tables()


def _stalled_table(it_db):
    """Skydance with one holding the pass reads its table for (Harbor Lights,
    written from it) and a second holder EDGAR must be asked about."""
    _seed(it_db)
    table = {"as_of": "2026-02-20", "rows": [_row("Harbor Lights Entertainment, Inc.", 60.0, "Class A"),
                                             _row("4280661 Canada Inc.", 9.6, "Class A")]}
    with patch("app.scraper.sec_lookup._browse_index", return_value=[]), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        from app.scraper.sec_writer import write_table_holders
        write_table_holders(ISSUER, FILING, {"as_of": "2026-02-20", "rows": table["rows"][:1]}, SID,
                            {"filings": {"recent": {"form": ["10-K", "10-K/A"]}}})
    assert set(_edges(it_db)) == {HARBOR}
    return table


def test_a_lost_name_search_is_not_an_unknown_name(it_db):
    # Wu-Wallace Family Trust on Figma, SailingStone on Compass Minerals (rebuild
    # 2026-11-24): the search timed out, the holder was counted with the names
    # EDGAR does not know, and nothing said a holder had not been looked up
    from app.scraper import sec_client
    from app.scraper.sec_writer import write_table_holders
    _seed(it_db)
    table = {"as_of": None, "rows": [_row("4280661 Canada Inc.", 9.6, "Class A"),
                                     _row("Nobody Knows Holdings LLC", 8.0, "Class A")]}
    _, lost_once = _answers(table, {0})

    def browse(query, form_type):
        return lost_once(query, form_type) if "4280661" in query else []
    with patch("app.scraper.sec_lookup._browse_index", side_effect=browse), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]), \
         sec_client.collect_lost_requests() as scrape_lost:
        out = write_table_holders(ISSUER, FILING, table, SID, {"filings": {"recent": {"form": ["10-K"]}}})
    assert (out["lost"], out["unresolved"], out["written"]) == (1, 1, 0)
    assert scrape_lost and all("browse-edgar" in url for url in scrape_lost)   # the scrape's own count keeps them


def test_the_pass_reads_a_table_again_whose_holder_was_not_looked_up(it_db):
    table = _stalled_table(it_db)
    res = _pass_with_a_stall(table, lost_readings={1})
    node = it_db.run_command("MATCH (e:Entity {sec_cik: '0001317151'}) RETURN e.id AS id")
    assert len(node) == 1 and set(_edges(it_db)) == {HARBOR, node[0]["id"]}
    assert (res["written"], res["lost"]) == (2, 0)                # Harbor Lights once, the Canadian holder once
    assert res["lines"][-1] == ("Skydance Corp: 1 holder(s) of its 10-K/A of 2026-04-24 not looked up — the name "
                                "search was lost to the network; read again: 1 written, none lost")


def test_a_holder_still_not_looked_up_is_counted_and_said(it_db):
    table = _stalled_table(it_db)
    res = _pass_with_a_stall(table, lost_readings={1, 2})
    assert set(_edges(it_db)) == {HARBOR}
    assert (res["written"], res["lost"]) == (1, 1)
    assert res["lines"][-1].endswith("read again: 0 written, 1 STILL NOT LOOKED UP")


def test_a_table_read_without_a_loss_is_read_once(it_db):
    table = _stalled_table(it_db)
    read_table, _ = _answers(table, set())
    res = _pass_with_a_stall(table, lost_readings=set())
    assert (res["written"], res["lost"]) == (2, 0) and not any("not looked up" in line for line in res["lines"])


def test_the_issuers_own_scrape_is_partial_when_its_table_lost_a_holder(it_db):
    _seed(it_db)
    table = {"as_of": "2026-04-17", "rows": [_row("4280661 Canada Inc.", 9.6, "Class A")]}
    _, browse = _answers(table, {0})
    out = _scrape(table, [], browse=browse)
    assert out["status"] == "partial" and out["failed_sections"] == ["ownership table"]
    assert _edges(it_db) == {}


# ── A table holder's older statement (findings item 157) ─────────────────────

NEWMARK = "0001690680"
CANTOR_13D = "0001024896-25-000010"


def _newmark(it_db):
    it_db.run_command(f"CREATE (:Source {{id: '{SID}', name: 'SEC EDGAR'}})")
    it_db.run_command("CREATE (:Entity {id: 'sec:0001690680', name: 'Newmark Group Inc', type: 'company', "
                      f"name_normalized: 'newmark group', sec_cik: '{NEWMARK}'}})")
    # the three holders stand in the graph already (another company's scrape wrote them)
    from app.scraper.mapper import normalize_entity_name
    for eid, name, cik in ((BLK, "BlackRock, Inc.", None), ("sec:0001024896", "Cantor Fitzgerald, L.P.", "0001024896"),
                           ("sec:0001097856", "CF Group Management, Inc.", "0001097856")):
        it_db.run_command("CREATE (:Entity {id: $id, name: $n, type: 'company', name_normalized: $k, sec_cik: $c})",
                          {"id": eid, "n": name, "k": normalize_entity_name(name), "c": cik})
    table = {"as_of": "2026-03-20", "rows": [_row("Cantor Fitzgerald, L.P.", 99.1, "Class B"),
                                             _row("CF Group Management, Inc.", 100.0, "Class B"),
                                             _row("BlackRock, Inc.", 6.6, "Class A")]}
    subs = {"cik": NEWMARK, "filings": {"recent": {
        "form": ["DEF 14A", "10-K"], "filingDate": ["2026-04-10", "2026-02-20"],
        "accessionNumber": ["0001690680-26-000020", "0001690680-26-000005"],
        "primaryDocument": ["def14a.htm", "10k.htm"]}}}
    return table, subs


def _cantor_statement():
    """Cantor's 13D/A as its XML reads: three reporting persons, the filer first."""
    person = lambda name, cik, code: {"name": name, "cik": cik, "percent": 19.9, "aggregate": 4.0e7, "sole_voting": 0.0,
                                      "shared_voting": 4.0e7, "sole_dispositive": 0.0, "shared_dispositive": 4.0e7,
                                      "type_code": code}
    return {"schedule": "13D", "issuer_cik": NEWMARK, "issuer_name": "Newmark Group, Inc.", "issuer_cusip": "65158N102",
            "class_title": "Class A Common Stock, par value $0.01 per share", "event_date": "2025-10-01",
            "final_report": None, "filer_cik": "0001024896", "comment_text": "", "raw_text": "",
            "persons": [person("Cantor Fitzgerald, L.P.", "0001024896", "PN"),
                        person("CF Group Management, Inc.", "0001097856", "CO"),
                        person("Brandon G. Lutnick", None, "IN")]}


def _index_entry(day="2025-10-06", name="CANTOR FITZGERALD, L. P.", cik="0001024896", accession=CANTOR_13D):
    from app.scraper import sec_client
    return {"cik": cik, "name": name, "file_date": day, "form_type": "SCHEDULE 13D/A", "accession": accession,
            "index_url": sec_client._filing_index_url(NEWMARK, accession)}


def _read_newmark(table, subs, persons=("CF Group Management, Inc.", "Brandon G. Lutnick"), statements_for="Newmark Group Inc",
                  statement=None):
    from app.scraper import sec_13dg_xml, sec_client, sec_filer, sec_lookup
    from app.scraper.sec_writer import read_table_and_close
    opened = []

    def xml(cik, accession):
        opened.append(accession)
        return statement or _cantor_statement()
    with patch.object(sec_client, "_submissions", return_value=subs), \
         patch.object(sec_client, "_get_text", return_value="<html></html>"), \
         patch.object(sec_13dg_xml, "_fetch_13dg_xml", side_effect=xml), \
         patch.object(sec_filer, "fetch_former_names", return_value=[]), \
         patch.object(sec_filer, "fetch_filer_name", return_value="CANTOR FITZGERALD, L. P."), \
         patch.object(sec_filer, "fetch_filer_sic", return_value=None), \
         patch.object(sec_lookup, "_browse_index", return_value=[]), \
         patch("app.scraper.proxy_statement.read_holder_table", return_value=table), \
         patch("app.scraper.proxy_statement.read_holder_rows", return_value=[]), \
         patch("app.scraper.sec_writer.statement_persons", return_value=list(persons)):
        res = read_table_and_close("sec:0001690680", NEWMARK, write=True, source_id=SID, statements_for=statements_for)
    return res, opened


def _newmark_edges(it_db):
    return sorted((r["n"], r["ft"], r["cls"]) for r in it_db.run_command(
        "MATCH (a)-[r:OWNS]->(b:Entity {id: 'sec:0001690680'}) WHERE r.until IS NULL "
        "RETURN coalesce(a.name, a.full_name) AS n, r.filing_type AS ft, r.share_class AS cls"))


def test_a_table_holder_is_written_from_its_own_older_statement(it_db, filer_index):
    """Newmark, never scraped itself: its proxy names Cantor Fitzgerald, L.P.
    (99.1 % of Class B) and CF Group Management (100 %). Cantor's 13D/A of
    2025-10-06 — older than the proxy, so 'the statements since the table' never
    read it — names CF Group Management as its co-filer. The two table rows were
    written side by side, 199 % of the class."""
    table, subs = _newmark(it_db)
    filer_index.return_value = [_index_entry(),
                                _index_entry(name="VANGUARD GROUP INC", cik="0000102909", accession="0000102909-25-000077")]
    res, opened = _read_newmark(table, subs)
    assert opened == [CANTOR_13D]                              # the holder the table names — not Vanguard's
    assert res["older"] == {"written": 1, "asked": True, "lost": False, "exited": []}
    edges = _newmark_edges(it_db)
    assert ("BlackRock, Inc.", "DEF 14A", "Class A") in edges  # no statement of its own: from the table, as before
    assert not [e for e in edges if e[1] == "DEF 14A" and e[0] != "BlackRock, Inc."], edges
    assert [e[1] for e in edges if e[0] != "BlackRock, Inc."] == ["13D/A"]
    assert (res["written"]["written"], res["written"]["covered"]) == (1, 2)


def test_without_the_statement_the_rows_are_written_from_the_table(it_db, filer_index):
    # the index knows no statement of these holders: the table is all there is (item 86)
    table, subs = _newmark(it_db)
    res, opened = _read_newmark(table, subs, persons=())
    assert opened == [] and res["older"] == {"written": 0, "asked": True, "lost": False, "exited": []}
    assert _newmark_edges(it_db) == [("BlackRock, Inc.", "DEF 14A", "Class A"), ("CF Group Management, Inc.", "DEF 14A", "Class B"),
                                     ("Cantor Fitzgerald, L.P.", "DEF 14A", "Class B")]


def test_a_statement_filed_after_the_table_is_not_read_here(it_db, filer_index):
    # that one is `write_statements_since`'s (findings item 116)
    table, subs = _newmark(it_db)
    filer_index.return_value = [_index_entry(day="2026-06-01")]
    _, opened = _read_newmark(table, subs, persons=())
    assert opened == []


def test_a_filer_of_another_name_is_not_read(it_db, filer_index):
    table, subs = _newmark(it_db)
    filer_index.return_value = [_index_entry(name="CANTOR FITZGERALD SECURITIES", cik="0000017018")]
    _, opened = _read_newmark(table, subs, persons=())
    assert opened == []


def test_the_index_is_not_asked_when_statements_cover_every_row(it_db, filer_index):
    from app.scraper.sec_writer import _upsert_owns_sec
    table, subs = _newmark(it_db)
    table["rows"] = [_row("BlackRock, Inc.", 6.6, "Class A")]
    _upsert_owns_sec(BLK, "sec:0001690680", SID, ownership_type="minority", file_date="2026-02-01", stake_percent=6.6,
                     filing_type="13G/A", source_url="https://www.sec.gov/blk-index.htm")
    res, _ = _read_newmark(table, subs, persons=())
    filer_index.assert_not_called()
    assert res["older"] == {"written": 0, "asked": False, "lost": False, "exited": []}


def test_the_issuers_own_scrape_does_not_ask_the_index(it_db, filer_index):
    table, subs = _newmark(it_db)
    res, _ = _read_newmark(table, subs, persons=(), statements_for=None)
    filer_index.assert_not_called()
    assert res["older"] is None


def test_an_index_that_did_not_answer_holds_the_table_rows_back(it_db, filer_index):
    # "in no 13D/G" cannot be said when nobody could ask (items 155, 157): the rows
    # are counted lost, so the pass reads the table again at its end
    table, subs = _newmark(it_db)
    filer_index.return_value = None
    res, _ = _read_newmark(table, subs, persons=())
    assert res["older"] == {"written": 0, "asked": True, "lost": True, "exited": []}
    assert (res["written"]["written"], res["written"]["lost"]) == (0, 3)
    assert _newmark_edges(it_db) == []


def _newmark_pass(it_db):
    """The whole-graph pass over a graph in which somebody's open 13G brings
    Newmark in; its table names Cantor and CF Group Management."""
    from app.scraper import sec_13dg_xml, sec_client, sec_filer, sec_lookup
    from app.scraper.sec_writer import _upsert_owns_sec, close_absent_from_tables
    table, subs = _newmark(it_db)
    table["rows"] = table["rows"][:2]
    it_db.run_command("CREATE (:Entity {id: 'lei:vg', name: 'VANGUARD GROUP INC', type: 'company', "
                      "name_normalized: 'vanguard group', sec_cik: '0000102909'})")
    _upsert_owns_sec("lei:vg", "sec:0001690680", SID, ownership_type="minority", file_date="2026-02-10",
                     stake_percent=10.0, filing_type="13G/A", source_url="https://www.sec.gov/vg-index.htm")
    with patch.object(sec_client, "_submissions", return_value=subs), \
         patch.object(sec_client, "_get_text", return_value="<html></html>"), \
         patch.object(sec_13dg_xml, "_fetch_13dg_xml", return_value=_cantor_statement()), \
         patch.object(sec_filer, "fetch_former_names", return_value=[]), \
         patch.object(sec_filer, "fetch_filer_name", return_value="CANTOR FITZGERALD, L. P."), \
         patch.object(sec_filer, "fetch_filer_sic", return_value=None), \
         patch.object(sec_lookup, "_browse_index", return_value=[]), \
         patch("app.scraper.proxy_statement.read_holder_table", return_value=table), \
         patch("app.scraper.proxy_statement.read_holder_rows",
               return_value=[{"name": r["name"], "percents": [r["stake_pct"]]} for r in table["rows"]] +
                            [{"name": "The Vanguard Group", "percents": [10.0]}]), \
         patch("app.scraper.sec_writer.statement_persons", return_value=["CF Group Management, Inc."]):
        res = close_absent_from_tables()
    edges = _newmark_edges(it_db)
    assert sorted(e[1] for e in edges) == ["13D/A", "13G/A"] and not [e for e in edges if e[1] == "DEF 14A"]
    return res


def test_the_pass_counts_and_says_the_older_statements_it_wrote(it_db, filer_index):
    filer_index.return_value = [_index_entry()]
    res = _newmark_pass(it_db)
    assert (res["older"], res["lost"], res["written"], res["statements"]) == (1, 0, 0, 0)
    assert res["lines"] == ["Newmark Group Inc: 1 statement holders written — their newest 13D/G, filed before its "
                            "DEF 14A of 2026-04-10, which names them"]


def test_the_pass_reads_the_table_again_when_the_index_answers_later(it_db, filer_index):
    # the index is lost at the first reading and the rows are held back; read
    # again at the end of the pass, Cantor's statement is written
    filer_index.side_effect = [None, [_index_entry()]]
    res = _newmark_pass(it_db)
    assert filer_index.call_count == 2
    assert (res["older"], res["lost"], res["written"]) == (1, 0, 0)
    assert res["lines"][-1].endswith("read again: 0 written, none lost; 1 statement holders written")


def test_a_holder_whose_newest_statement_before_the_table_is_an_exit_is_not_written(it_db, filer_index):
    """Findings item 159: The Vanguard Group's 13G/A of 2026-03-26 reports 0 %
    (its holdings went to Vanguard Capital Management and Vanguard Portfolio
    Management); Lexeo's proxy of 2026-04-15 still lists it at 5 %, "based on …
    a Schedule 13G filed … on January 30, 2026". The exit is filed before the
    table, so 'the statements since' never read it, and the older-statements
    pass read it and dropped it: the row was written from the table — 24
    tables in the rebuild of 2026-11-26b."""
    from pathlib import Path
    from app.scraper import sec_13dg_xml
    table, subs = _newmark(it_db)
    table["as_of"] = "2026-03-31"
    table["rows"] = [_row("The Vanguard Group", 5.0), _row("BlackRock, Inc.", 6.6, "Class A")]
    it_db.run_command("CREATE (:Entity {id: 'lei:vg', name: 'VANGUARD GROUP INC', type: 'company', "
                      "name_normalized: 'vanguard group', sec_cik: '0000102909'})")
    fixtures = Path(__file__).parent.parent / "scraper" / "fixtures"
    exit_xml = {**sec_13dg_xml._parse_13dg_xml((fixtures / "13ga_vanguard_exit.xml").read_text()),
                "issuer_cik": NEWMARK, "issuer_name": "Newmark Group, Inc."}    # the real one is on GCI Liberty
    filer_index.return_value = [_index_entry(day="2026-03-26", name="VANGUARD GROUP INC", cik="0000102909",
                                             accession=VANGUARD_EXIT)]
    res, opened = _read_newmark(table, subs, persons=(), statement=exit_xml)
    assert opened == [VANGUARD_EXIT]
    assert res["older"]["written"] == 0 and "The Vanguard Group" in res["older"]["exited"]
    assert _newmark_edges(it_db) == [("BlackRock, Inc.", "DEF 14A", "Class A")]
    assert (res["written"]["written"], res["written"]["exited"]) == (1, 1)
