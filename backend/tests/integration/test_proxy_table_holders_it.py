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


def _write(table, filing=FILING, registrant=None):
    from app.scraper import sec_edgar
    from app.scraper.sec_writer import write_table_holders
    with patch.object(sec_edgar, "_browse_index", return_value=[registrant] if registrant else []), \
         patch("app.scraper.sec_writer.statement_persons", return_value=[]):
        return write_table_holders(ISSUER, filing, table, SID, {"filings": {"recent": {"form": ["10-K", "10-K/A"]}}})


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
    assert out == {"written": 1, "covered": 0, "unresolved": 1, "shared": 1, "fund": False}
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
