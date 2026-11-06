"""Wikidata node hygiene against a real database: an id-less related company
is skipped (no orphan minted), an id-bearing one is created and can hard-merge,
and an already-existing one gets the edge without a duplicate."""
import pytest
from unittest.mock import patch

from app.scraper import runner
from app.scraper import sources as src_mod

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _wikidata_draws(it_db):
    # Node hygiene is the subject here, but the assertions count the edges the
    # scrape draws. Wikidata is claims-only unless asked otherwise (the built-in
    # SCRAPER_WIKIDATA_DATA_MODE default), so ask: a row with a mode wins over
    # the default. Without this the file passed only on a warm mode cache.
    it_db.run_command("MERGE (s:Source {id: 'wikidata'}) SET s.registry_key = 'wikidata', s.name = 'Wikidata', "
                      "s.data_mode = 'full', s.enabled = true, s.kind = 'instant'")
    src_mod._MODE_CACHE["at"] = None
    yield
    src_mod._MODE_CACHE["at"] = None


def _company_data(subsidiaries):
    return {"qid": "Q_PARENT", "name": "Parent Corp", "description": None,
            "instances": ["Q4830453"], "country": None, "founded": None,
            "revenue": None, "subsidiaries": subsidiaries,
            "owners": [], "successors": [], "predecessors": [],
            "ceos": [], "officers": []}


def _scrape(it_db, data):
    src = runner._ensure_source(runner.WIKIDATA_SOURCE_NAME,
                                runner.WIKIDATA_SOURCE_URL, 80, "knowledge_base")
    counts: dict = {}
    with patch("app.scraper.runner.fetch_company_data", return_value=data):
        runner._scrape_node("Q_PARENT", depth=1, visited=set(), scraped=[],
                            source_id=src, counts=counts)
    return counts


def test_an_idless_subsidiary_mints_no_node_and_draws_no_edge(it_db):
    counts = _scrape(it_db, _company_data(
        [{"qid": "Q_SUB_NOID", "name": "Ghost Sub Ltd", "instances": []}]))
    assert counts.get("skipped_unidentified") == 1
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity "
                         "WHERE name = 'Ghost Sub Ltd'")[0]["n"] == 0
    # the parent exists, but it has no OWNS edge out
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0


def _gleif_record(lei, name):
    from app.scraper.register_pointer import golden_copy_record
    return golden_copy_record({
        "lei": lei,
        "entity": {"legalName": {"name": name}, "jurisdiction": "DE", "status": "ACTIVE",
                   "legalAddress": {"addressLines": ["Hauptstrasse 1"], "city": "Berlin",
                                    "country": "DE", "postalCode": "10115"},
                   "registeredAt": {"id": "RA000197"}, "registeredAs": "HRB 1",
                   "creationDate": "2001-05-01T22:00:00Z"},
        "registration": {"status": "ISSUED", "corroborationLevel": "FULLY_CORROBORATED",
                         "initialRegistrationDate": "2012-06-06T08:00:00Z"}})


def test_an_lei_bearing_subsidiary_is_written_by_gleif(it_db):
    """2026-10-29: Wikidata's LEI is a pointer — GLEIF writes the node under
    its own name, id and source, and Wikidata adds its item and the edge."""
    lei = "SUB0000000000000TEST"
    with patch("app.scraper.register_pointer._gleif_enabled", return_value=True), \
         patch("app.scraper.register_pointer.fetch_gleif_record",
               return_value=_gleif_record(lei, "Real Sub AG")) as fetch:
        _scrape(it_db, _company_data(
            [{"qid": "Q_SUB_LEI", "name": "Real Sub", "instances": [], "lei": lei, "leis": [lei]}]))
    fetch.assert_called_once_with(lei)
    [row] = [dict(r) for r in it_db.run_sql(
        "SELECT id, name, lei_id, source_id, country, incorporated_on, wikidata_id FROM Entity "
        "WHERE lei_id = :l", {"l": lei})]
    assert row["id"] == f"lei:{lei}" and row["name"] == "Real Sub AG"
    assert row["source_id"] == "gleif" and row["country"] == "DE"
    assert row["incorporated_on"] == "2001-05-02"
    assert row["wikidata_id"] == "Q_SUB_LEI"
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1


def test_an_lei_gleif_does_not_know_writes_nothing(it_db):
    with patch("app.scraper.register_pointer._gleif_enabled", return_value=True), \
         patch("app.scraper.register_pointer.fetch_gleif_record", return_value=None):
        counts = _scrape(it_db, _company_data(
            [{"qid": "Q_SUB_BAD", "name": "Phantom Sub", "instances": [],
              "lei": "BAD00000000000000000", "leis": ["BAD00000000000000000"]}]))
    assert counts.get("skipped_unidentified") == 1
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE name = 'Phantom Sub'")[0]["n"] == 0
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0


def test_an_existing_subsidiary_gets_the_edge_without_being_recreated(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'existing-sub', name: 'Known Sub SA', "
        "name_normalized: 'known sub sa', search_text: 'Known Sub SA', "
        "type: 'company', wikidata_id: 'Q_SUB_KNOWN'})")
    _scrape(it_db, _company_data(
        [{"qid": "Q_SUB_KNOWN", "name": "Known Sub SA", "instances": []}]))
    subs = it_db.run_sql("SELECT id FROM Entity WHERE wikidata_id = 'Q_SUB_KNOWN'")
    assert [dict(r)["id"] for r in subs] == ["existing-sub"], "no duplicate minted"
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1


def test_an_item_whose_ids_another_legal_entity_holds_writes_nothing(it_db):
    """BlackRock, 2026-10-28: the item states the pre-2024 parent's LEI and
    CIK; the graph holds the listed company under its own GLEIF LEI. The item
    found it by name — and must write nothing onto it: no CIK, no item id, no
    subsidiary edge."""
    it_db.run_command(
        "CREATE (:Entity {id: 'lei:529900VBK42Y5HHRMD23', name: 'BlackRock, Inc.', "
        "name_normalized: 'blackrock', search_text: 'BlackRock, Inc.', type: 'company', "
        "lei_id: '529900VBK42Y5HHRMD23', name_credibility: 92})")
    data = {**_company_data([{"qid": "Q_SUB_KNOWN", "name": "Known Sub SA", "instances": []}]),
            "name": "BlackRock", "lei": "549300LRIF3NWCU26A80", "leis": ["549300LRIF3NWCU26A80"],
            "sec_cik": "0001364742", "sec_ciks": ["0001364742"]}
    it_db.run_command("CREATE (:Entity {id: 'existing-sub', name: 'Known Sub SA', "
                      "name_normalized: 'known sub sa', type: 'company', wikidata_id: 'Q_SUB_KNOWN'})")
    # found by name alone, so the item's ids are asked of their register
    # (findings item 30); none writes a node here, and the name's node is refused
    with patch("app.scraper.register_pointer.write_from_pointer", return_value=None) as reg:
        counts = _scrape(it_db, data)
    assert counts.get("conflicts") == 1
    assert reg.call_args.args == (["549300LRIF3NWCU26A80"], ["0001364742"]) and reg.call_count == 1
    assert reg.call_args.kwargs["names"][0] == "BlackRock"        # the item names break an LEI tie (item 61)
    row = it_db.run_sql("SELECT FROM Entity WHERE id = 'lei:529900VBK42Y5HHRMD23'")[0]
    assert row.get("sec_cik") is None and row.get("wikidata_id") is None
    assert row["name"] == "BlackRock, Inc."
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0


def test_an_item_stating_several_leis_lands_on_the_parent_per_gleif(it_db):
    """Barclays, 2026-11-06 rebuild (findings item 61): both LEI nodes exist, and
    the lookup took the first LEI Wikidata lists — the Bank — before the pointer's
    order (#554) could be asked. The parent per GLEIF comes first in the lookup too."""
    bank, plc = "G5GSEF7VJP5I7OUK5573", "213800LBQA1Y9L22JB70"
    for lei, name in ((bank, "BARCLAYS BANK PLC"), (plc, "BARCLAYS PLC")):
        it_db.run_command(
            f"CREATE (:Entity {{id: 'lei:{lei}', name: '{name}', name_normalized: '{name.lower()}', "
            f"search_text: '{name}', type: 'company', lei_id: '{lei}', name_credibility: 92}})")
    data = {**_company_data([]), "name": "Barclays", "aliases": ["Barclays PLC", "Barclays Bank"],
            "lei": bank, "leis": [bank, plc]}
    with patch("app.scraper.legal_identity.gleif_parents",
               side_effect=lambda lei: [{"lei": plc, "names": ["BARCLAYS PLC"]}] if lei == bank else []), \
         patch("app.scraper.register_pointer.fetch_gleif_record", return_value=None):
        _scrape(it_db, data)
    by_id = {r["id"]: dict(r) for r in it_db.run_sql(
        f"SELECT id, wikidata_id, aliases FROM Entity WHERE id IN ['lei:{bank}', 'lei:{plc}']")}
    assert by_id[f"lei:{plc}"]["wikidata_id"] == "Q_PARENT"
    assert "Barclays PLC" in (by_id[f"lei:{plc}"].get("aliases") or [])
    assert by_id[f"lei:{bank}"].get("wikidata_id") is None


def test_a_related_company_the_registers_wrote_first_takes_the_item(it_db):
    """MUFG, 2026-11-06 rebuild (findings item 66): the owner's filer node
    existed from a 13D/A when the item named it; the gate drew the edge and
    left the node without the item. Now the writer enriches it: the QID lands,
    the stated LEI goes to GLEIF (`attach_stated_lei`, mocked here), the edge
    is drawn once."""
    it_db.run_command(
        "CREATE (:Entity {id: 'sec:0000067088', name: 'Mitsubishi Ufj Financial Group, Inc.', "
        "name_normalized: 'mitsubishi ufj financial group', search_text: 'Mitsubishi Ufj Financial Group, Inc.', "
        "type: 'company', sec_cik: '0000067088', name_credibility: 98})")
    data = {**_company_data([]),
            "owners": [{"qid": "Q1542175", "label": "Mitsubishi UFJ Financial Group", "instances": ["Q4830453"],
                        "sec_cik": "0000067088", "sec_ciks": ["0000067088"],
                        "lei": "353800V2V8PUY9TK3E06", "leis": ["353800V2V8PUY9TK3E06"]}]}
    with patch("app.scraper.register_pointer.attach_stated_lei", return_value=None) as attach:
        counts = _scrape(it_db, data)
    row = dict(it_db.run_sql("SELECT id, wikidata_id, name, sec_cik FROM Entity WHERE sec_cik = '0000067088'")[0])
    assert row["id"] == "sec:0000067088" and row["wikidata_id"] == "Q1542175"
    assert row["name"] == "Mitsubishi Ufj Financial Group, Inc."           # EDGAR's name stays (98 > 80)
    assert attach.call_args.args[1] == "sec:0000067088" and attach.call_args.args[2] == ["353800V2V8PUY9TK3E06"]
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE wikidata_id = 'Q1542175'")[0]["n"] == 1
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1
    assert not counts.get("skipped_unidentified") and not counts.get("conflicts")


def test_a_renamed_registrant_keeps_the_item(it_db):
    """Franklin, 2026-11-06 rebuild (findings item 66): EDGAR renamed CIK
    0000038777 FRANKLIN TEMPLETON INC; the item's alias is the old name and no
    other registrant bears it — a rename, not a move. The item lands."""
    it_db.run_command(
        "CREATE (:Entity {id: 'sec:0000038777', name: 'FRANKLIN TEMPLETON INC', name_normalized: 'franklin templeton', "
        "search_text: 'FRANKLIN TEMPLETON INC', type: 'company', sec_cik: '0000038777', name_credibility: 98, "
        "former_names: ['FRANKLIN RESOURCES INC|2026-08-14']})")
    data = {**_company_data([]), "name": "Franklin Templeton Investments", "aliases": ["Franklin Resources"],
            "sec_cik": "0000038777", "sec_ciks": ["0000038777"]}
    with patch("app.scraper.register_pointer._sec_enabled", return_value=True), \
         patch("app.scraper.sec_edgar.search_company",
               return_value={"cik": "0000038777", "name": "FRANKLIN TEMPLETON INC"}) as search:
        counts = _scrape(it_db, data)
    search.assert_called_once_with("Franklin Resources")
    row = dict(it_db.run_sql("SELECT wikidata_id, name, aliases FROM Entity WHERE id = 'sec:0000038777'")[0])
    assert row["wikidata_id"] == "Q_PARENT" and not counts.get("conflicts")
    assert row["name"] == "FRANKLIN TEMPLETON INC"                    # EDGAR's name stays (98 > 80)


def test_a_name_another_registrant_now_bears_still_refuses(it_db):
    """BlackRock (findings item 22): the item's CIK is the old parent's, and
    EDGAR lists "BlackRock, Inc." under the new parent's CIK — the item has
    moved on; nothing is written."""
    it_db.run_command(
        "CREATE (:Entity {id: 'sec:0001364742', name: 'BlackRock Finance, Inc.', name_normalized: 'blackrock finance', "
        "search_text: 'BlackRock Finance, Inc.', type: 'company', sec_cik: '0001364742', name_credibility: 98, "
        "former_names: ['BlackRock Inc.|2024-09-26']})")
    data = {**_company_data([]), "name": "BlackRock", "aliases": ["BlackRock, Inc."],
            "sec_cik": "0001364742", "sec_ciks": ["0001364742"]}
    with patch("app.scraper.register_pointer._sec_enabled", return_value=True), \
         patch("app.scraper.sec_edgar.search_company", return_value={"cik": "0002012383", "name": "BlackRock, Inc."}):
        counts = _scrape(it_db, data)
    assert counts.get("conflicts") == 1
    row = dict(it_db.run_sql("SELECT wikidata_id FROM Entity WHERE id = 'sec:0001364742'")[0])
    assert row.get("wikidata_id") is None
