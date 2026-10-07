"""Exhibit 21 write path against a real ArcadeDB: record → runner → database →
read the fields back. The mapper proving a field means nothing about storage
(the PSC register_id lesson), so this drives run_sec_ex21 end to end with only
the network mocked."""
import pytest
from unittest.mock import patch

from app.config import settings
from app.scraper import runner

pytestmark = pytest.mark.integration

APPLE_DATA = {
    "subsidiaries": [
        {"name": "Apple Operations International Limited", "jurisdiction": "Ireland"},
        {"name": "Braeburn Capital, Inc.", "jurisdiction": "Nevada, U.S.",
         "stake_percent": 100.0},
        {"name": "Diagnosys (Pinpoint) Inc.", "jurisdiction": "Florida, USA"},
        {"name": "Apple Ruritania GmbH", "jurisdiction": "Ruritania"},  # unmappable
    ],
    "form": "10-K", "filing_date": "2025-10-31",
    "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/a10-kexhibit21109272025.htm",
}


@pytest.fixture(autouse=True)
def _flags():
    with patch.object(settings, "SCRAPER_ENABLED", True), \
         patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True):
        yield


def _apple(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'apple', name: 'Apple Inc.', name_normalized: 'apple', "
        "search_text: 'Apple Inc.', type: 'company', sec_cik: '0000320193'})")


def test_writes_subsidiaries_with_provenance_and_reads_them_back(it_db):
    _apple(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=APPLE_DATA):
        result = runner.run_sec_ex21("Apple")
    assert result["status"] == "ok"
    assert result["total"] == 4
    assert result["unmapped_jurisdictions"] == 1

    rows = it_db.run_command(
        "MATCH (a:Entity {id:'apple'})-[r:OWNS]->(b:Entity) "
        "RETURN b.name AS name, b.country AS country, r.filing_type AS ft, "
        "r.ownership_type AS ot, r.since AS since, r.source_date AS asof, "
        "r.source_url AS url, r.stake_percent AS stake")
    got = {dict(r)["name"]: dict(r) for r in rows}
    assert set(got) == {"Apple Operations International Limited",
                        "Braeburn Capital, Inc.", "Apple Ruritania GmbH",
                        "Diagnosys (Pinpoint) Inc."}
    ie = got["Apple Operations International Limited"]
    assert ie["country"] == "IE"
    assert ie["ft"] == "EX-21"
    assert ie["ot"] == "controlling"
    # A subsidiary LIST says what is held as of the filing, not since when:
    # the date is the as-of/source date and no start date is invented.
    assert ie["since"] is None
    assert ie["asof"] == "2025-10-31"
    assert "a10-kexhibit21" in ie["url"]
    assert ie["stake"] is None, "the exhibit states no stake; none is invented"
    assert got["Braeburn Capital, Inc."]["country"] == "US"
    # The finer grain the filing stated must survive — the reported bug.
    juris = it_db.run_command(
        "MATCH (:Entity {id:'apple'})-[:OWNS]->(b:Entity {name:'Diagnosys (Pinpoint) Inc.'}) "
        "RETURN b.country AS country, b.jurisdiction_code AS jc")
    assert dict(juris[0])["country"] == "US"
    assert dict(juris[0])["jc"] == "US-FL", "Florida, not just United States"
    assert got["Braeburn Capital, Inc."]["stake"] == 100.0, \
        "a STATED percentage (Astronics-style column) is stored"
    assert got["Apple Ruritania GmbH"]["country"] is None, \
        "unmappable jurisdiction stays uncoded, never guessed"


def test_resolves_an_existing_node_instead_of_duplicating(it_db):
    _apple(it_db)
    # the Irish subsidiary already exists from a GLEIF import, with an LEI
    it_db.run_command(
        "CREATE (:Entity {id: 'lei:APPLEOPS00000000001X', "
        "name: 'Apple Operations International Limited', "
        "name_normalized: 'apple operations international', "
        "search_text: 'Apple Operations International Limited', type: 'company', "
        "lei_id: 'APPLEOPS00000000001X'})")
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=APPLE_DATA):
        runner.run_sec_ex21("Apple")
    # count on `name`, not search_text: the FULL_TEXT index makes equality on
    # search_text behave as a token match ('Apple' matched three rows here)
    n = it_db.run_sql("SELECT count(*) AS n FROM Entity "
                      "WHERE name = 'Apple Operations International Limited'")[0]["n"]
    assert n == 1, "resolved by name — no duplicate beside the GLEIF node"
    edge = it_db.run_command(
        "MATCH (a:Entity {id:'apple'})-[:OWNS]->(b:Entity {id:'lei:APPLEOPS00000000001X'}) "
        "RETURN count(*) AS n")
    assert dict(edge[0])["n"] == 1, "the edge points at the existing node"


def test_rerun_is_gated_and_idempotent(it_db):
    _apple(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=APPLE_DATA):
        first = runner.run_sec_ex21("Apple")
        second = runner.run_sec_ex21("Apple")
        forced = runner.run_sec_ex21("Apple", force=True)
    assert first["status"] == "ok"
    assert second["status"] == "fresh", "same filing → gated"
    assert forced["status"] == "ok"
    n = it_db.run_command("MATCH (:Entity {id:'apple'})-[r:OWNS]->() RETURN count(r) AS n")
    assert dict(n[0])["n"] == 4, "force re-run updates, never doubles edges"


def test_no_cik_asks_for_the_sec_scrape_first(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'nocik', name: 'Private GmbH', name_normalized: 'private', "
        "search_text: 'Private GmbH', type: 'company'})")
    result = runner.run_sec_ex21("Private GmbH")
    assert result["status"] == "needs_sec_scrape"
    n = it_db.run_command("MATCH ()-[r:OWNS]->() RETURN count(r) AS n")
    assert dict(n[0])["n"] == 0, "nothing written before the record is judged"


# ── Co-holders a percentage cell names ────────────────────────────────────────

CHUBB_DATA = {
    "subsidiaries": [
        {"name": "Chubb Tempest Reinsurance Ltd.", "jurisdiction": "Bermuda", "stake_percent": 100.0},
        {"name": "Chubb Bermuda Insurance Ltd.", "jurisdiction": "Bermuda", "stake_percent": 100.0},
        # "66.66% 33.33% (Chubb Bermuda Insurance Ltd.)"
        {"name": "Oasis Investments Ltd.", "jurisdiction": "Bermuda", "stake_percent": 66.66,
         "co_owners": [{"name": "Chubb Bermuda Insurance Ltd.", "stake_percent": 33.33}]},
        # "87.99% 12.01% (Chubb Limited)" — the co-holder is the filer itself
        {"name": "Chubb INA Holdings LLC", "jurisdiction": "USA (Delaware)", "stake_percent": 87.99,
         "co_owners": [{"name": "Chubb Limited", "stake_percent": 12.01}]},
        # a co-holder the list does not carry: no edge, nothing looked up
        {"name": "Chubb Seguros Chile S.A.", "jurisdiction": "Chile", "stake_percent": 99.0,
         "co_owners": [{"name": "Some Stranger Holdings", "stake_percent": 1.0}]},
        # the nominal second shareholder Mexican law requires
        {"name": "AFIA Finance Corporation", "jurisdiction": "USA (Delaware)", "stake_percent": 100.0},
        {"name": "Chubb Seguros México S.A.", "jurisdiction": "Mexico", "stake_percent": 99.9999996,
         "co_owners": [{"name": "AFIA Finance Corporation", "stake_percent": 0.0000003}]},
    ],
    "form": "10-K", "filing_date": "2026-02-26",
    "url": "https://www.sec.gov/Archives/edgar/data/896159/000089615926000012/cb-20251231xex211.htm",
}


def test_co_holders_get_their_own_edges_and_the_filer_its_own_share(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'chubb', name: 'CHUBB LIMITED', name_normalized: 'chubb', "
        "search_text: 'CHUBB LIMITED', type: 'company', sec_cik: '0000896159'})")
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=CHUBB_DATA):
        result = runner.run_sec_ex21("Chubb")
    assert result["status"] == "ok"
    assert result["total"] == 7 and result["co_owner_edges"] == 2

    def read():
        rows = it_db.run_command(
            "MATCH (a:Entity)-[r:OWNS]->(b:Entity) RETURN a.name AS owner, b.name AS owned, "
            "r.stake_percent AS stake, r.filing_type AS ft, r.ownership_type AS ot")
        return {(r["owner"], r["owned"]): (r.get("stake"), r.get("ft")) for r in rows}, \
               {(r["owner"], r["owned"]): r.get("ot") for r in rows}
    edges, types = read()
    # the listed holding at its stated share…
    assert edges[("CHUBB LIMITED", "Oasis Investments Ltd.")] == (66.66, "EX-21")
    # …and the co-holder's edge, from the listed subsidiary, at its share
    assert edges[("Chubb Bermuda Insurance Ltd.", "Oasis Investments Ltd.")] == (33.33, "EX-21")
    # a co-holder that is the filer becomes the filer's own stake
    assert edges[("CHUBB LIMITED", "Chubb INA Holdings LLC")][0] == 12.01
    # an unlisted co-holder draws nothing and creates nobody
    assert not any(o == "Some Stranger Holdings" for o, _ in edges)
    assert it_db.run_command("MATCH (e:Entity) WHERE e.name CONTAINS 'Stranger' RETURN count(e) AS n")[0]["n"] == 0
    assert len(edges) == 9
    # a co-holder is typed by its share, not "controlling" like a listed subsidiary
    assert types[("AFIA Finance Corporation", "Chubb Seguros México S.A.")] == "minority"
    assert types[("Chubb Bermuda Insurance Ltd.", "Oasis Investments Ltd.")] == "controlling"   # 33.33%
    assert types[("CHUBB LIMITED", "Chubb Seguros México S.A.")] == "controlling"
    # a re-read corrects the type on the existing edge, not only the stake
    it_db.run_command("MATCH ()-[r:OWNS]->(:Entity {name: 'Chubb Seguros México S.A.'}) "
                      "SET r.ownership_type = 'controlling'")
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=CHUBB_DATA):
        runner.run_sec_ex21("Chubb", force=True)
    assert read()[1][("AFIA Finance Corporation", "Chubb Seguros México S.A.")] == "minority"


# ── The tree the filer draws ──────────────────────────────────────────────────

def _tree_data(**over):
    return {**{
        "subsidiaries": [
            {"name": "Aquarion Company", "jurisdiction": "Delaware", "parent_basis": "indent"},
            {"name": "Aquarion Water Company", "jurisdiction": "Connecticut",
             "parent": "Aquarion Company", "parent_basis": "indent"},
            {"name": "Abenaki Water Co., Inc.", "jurisdiction": "New Hampshire",
             "parent": "Aquarion Water Company", "parent_basis": "indent", "stake_percent": 100.0},
            {"name": "HWP Company", "jurisdiction": "Massachusetts", "parent_basis": "indent"},
            # a parent the list does not carry: stays under the filer, counted
            {"name": "Orphan Holdings LLC", "jurisdiction": "Delaware",
             "parent": "Yahoo! Inc.", "parent_basis": "heading"},
        ],
        "form": "10-K", "filing_date": "2026-02-17",
        "url": "https://www.sec.gov/Archives/edgar/data/72741/000007274126000010/a2025-ex21.htm",
    }, **over}


def _eversource(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'es', name: 'Eversource Energy', name_normalized: 'eversource energy', "
        "search_text: 'Eversource Energy', type: 'company', sec_cik: '0000072741'})")


def _owns(it_db):
    rows = it_db.run_command(
        "MATCH (a:Entity)-[r:OWNS]->(b:Entity) WHERE r.until IS NULL RETURN a.id AS o, b.name AS s, "
        "r.direct_or_indirect AS doi, r.structure_basis AS sb, r.source_id AS src, r.stake_percent AS stake")
    return {(r["o"], r["s"]): (r.get("doi"), r.get("sb"), r.get("src"), r.get("stake")) for r in rows}


def test_a_drawn_tree_puts_each_subsidiary_under_its_parent_not_the_filer(it_db):
    _eversource(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=_tree_data()):
        result = runner.run_sec_ex21("Eversource")
    assert result["status"] == "ok"
    assert (result["total"], result["nested"], result["unresolved_parents"], result["detached"]) == (5, 2, 1, 0)
    edges = _owns(it_db)
    aq = it_db.run_command("MATCH (e:Entity {name: 'Aquarion Company'}) RETURN e.id AS id")[0]["id"]
    aw = it_db.run_command("MATCH (e:Entity {name: 'Aquarion Water Company'}) RETURN e.id AS id")[0]["id"]
    assert edges[("es", "Aquarion Company")][:2] == ("direct", "ex21_indent")
    assert edges[(aq, "Aquarion Water Company")][:2] == ("direct", "ex21_indent")
    assert edges[(aw, "Abenaki Water Co., Inc.")] [:2] == ("direct", "ex21_indent")
    assert edges[(aw, "Abenaki Water Co., Inc.")][3] == 100.0      # the row's stake is the parent's
    assert ("es", "Aquarion Water Company") not in edges           # NOT also under the filer
    assert ("es", "Abenaki Water Co., Inc.") not in edges
    assert edges[("es", "Orphan Holdings LLC")][:2] == (None, None)   # unresolved parent: flat, unmarked
    assert edges[("es", "HWP Company")][:2] == ("direct", "ex21_indent")


def test_a_re_read_that_finds_the_tree_withdraws_the_flat_filer_edges(it_db):
    _eversource(it_db)
    flat = _tree_data(subsidiaries=[{k: v for k, v in s.items() if k not in ("parent", "parent_basis")}
                                    for s in _tree_data()["subsidiaries"]])
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=flat):
        runner.run_sec_ex21("Eversource")
    assert ("es", "Abenaki Water Co., Inc.") in _owns(it_db)
    # GLEIF also states the filer's ultimate-parent link to one of them — on
    # the pair's one shared edge, as the delta writes it
    from app.scraper.gleif_incremental import _owns_edge_upsert
    aw = it_db.run_command("MATCH (e:Entity {name: 'Aquarion Water Company'}) RETURN e.id AS id")[0]["id"]
    assert _owns_edge_upsert("es", aw, "LEI0000000000000AW00", "indirect", "gleif", 92, None) == "adopted"
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=_tree_data()):
        result = runner.run_sec_ex21("Eversource", force=True)
    assert result["detached"] == 2
    edges = _owns(it_db)
    assert ("es", "Abenaki Water Co., Inc.") not in edges           # SEC's alone: deleted
    # GLEIF's link stays, and no longer cites SEC for anything
    assert edges[("es", "Aquarion Water Company")] == ("indirect", None, "gleif", None)
    claims = {r["source_id"] for r in it_db.run_sql(
        "SELECT source_id FROM Claim WHERE from_id = 'es' AND to_id IN "
        "(SELECT id FROM Entity WHERE name IN ['Aquarion Water Company', 'Abenaki Water Co., Inc.'])")}
    assert claims == {"gleif"}


def test_the_history_walks_the_tree(it_db):
    from app.scraper.mapper import normalize_entity_name as n
    _eversource(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=_tree_data()):
        runner.run_sec_ex21("Eversource")
    history = [{"as_of": "2025-12-31", "filing_date": "2026-02-17", "form": "10-K",
                "url": "https://www.sec.gov/2026.htm",
                "names": {n("Aquarion Company"), n("Aquarion Water Company"), n("Abenaki Water Co., Inc.")}},
               {"as_of": "2019-12-31", "filing_date": "2020-02-17", "form": "10-K",
                "url": "https://www.sec.gov/2020.htm",
                "names": {n("Aquarion Company"), n("Aquarion Water Company"), n("Abenaki Water Co., Inc.")}}]
    with patch("app.routers.search.resolve_best_entity", return_value={"id": "es", "sec_cik": "0000072741"}), \
         patch("app.scraper.sec_ex21.fetch_subsidiary_history", return_value=history):
        res = runner.run_sec_ex21_history("Eversource")
    assert res["status"] == "ok" and res["total"] == 3
    since = {r["s"]: r["since"] for r in it_db.run_command(
        "MATCH (a:Entity)-[r:OWNS]->(b:Entity) WHERE r.until IS NULL RETURN b.name AS s, r.since AS since")}
    # the nested edges (holder → subsidiary) are dated, not only the filer's
    assert since["Aquarion Water Company"] == "2019-12-31"
    assert since["Abenaki Water Co., Inc."] == "2019-12-31"


def _abinbev(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'abi', name: 'Anheuser-Busch InBev SA/NV', name_normalized: 'anheuser busch inbev', "
        "search_text: 'Anheuser-Busch InBev', type: 'company', sec_cik: '0001668717'})")


def test_abinbevs_list_from_its_20f_note_lands_with_stakes(it_db):
    # the list read from note 34 of the 20-F (no Exhibit 8.1 file): stakes,
    # countries from the country rows, the note as the source
    from pathlib import Path
    from app.scraper.sec_ex21 import parse_exhibit
    note = (Path(__file__).parents[1] / "scraper" / "fixtures" / "abinbev_20f_note34.htm").read_text()
    data = {"subsidiaries": parse_exhibit(note, "Anheuser-Busch InBev SA/NV"), "form": "20-F",
            "filing_date": "2026-03-03",
            "url": "https://www.sec.gov/Archives/edgar/data/1668717/000119312526088105/d65314d20f.htm#note-34"}
    _abinbev(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=data):
        result = runner.run_sec_ex21("Anheuser-Busch InBev")
    assert result["status"] == "ok" and result["total"] == 65
    row = it_db.run_command(
        "MATCH (:Entity {id:'abi'})-[r:OWNS]->(b:Entity {name: 'Cerveceria y Malteria Quilmes Saica Y G'}) "
        "RETURN b.country AS country, r.stake_percent AS stake, r.filing_type AS ft, r.source_url AS url")
    assert row == [{"country": "AR", "stake": 61.63, "ft": "EX-8.1", "url": data["url"]}]


@pytest.mark.parametrize("filings,status", [([], "no_annual_filing"),
                                            ([("10-K", "0000000001-26-000001", "2026-02-25", "")], "no_exhibit")])
def test_no_list_says_why(it_db, filings, status):
    # SoftBank, Vanguard, FMR file no 10-K/20-F at all — "no exhibit" said the
    # filing lacked one
    _apple(it_db)
    with patch("app.scraper.sec_ex21.fetch_subsidiaries", return_value=None), \
         patch("app.scraper.sec_ex21.annual_filings", return_value=filings):
        assert runner.run_sec_ex21("Apple")["status"] == status
