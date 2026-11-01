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
    reg.assert_called_once_with(["549300LRIF3NWCU26A80"], ["0001364742"])
    row = it_db.run_sql("SELECT FROM Entity WHERE id = 'lei:529900VBK42Y5HHRMD23'")[0]
    assert row.get("sec_cik") is None and row.get("wikidata_id") is None
    assert row["name"] == "BlackRock, Inc."
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0
