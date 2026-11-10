"""The securities bridge writes against a real ArcadeDB (findings item 71): a
join folds the filer and the GLEIF node into one, an attach has GLEIF write its
record first and folds the same way, an umbrella and a pair kept apart are left
alone. Only GLEIF is mocked (the record API); the mapping file is a real-shaped zip."""
import zipfile
from unittest.mock import patch

import pytest

from app.db.arcadedb import run_sql
from app.scraper import gleif_isin as gi

pytestmark = pytest.mark.integration

LEI, CIK, CUSIP, ISIN = "54930055RWDMVXAGZK98", "0000908937", "829933100", "US8299331004"


@pytest.fixture
def mapping(tmp_path):
    path = tmp_path / "isin-lei-20261106T081508.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lei-isin-20261106T081508.csv",
                   f"LEI,ISIN\n{LEI},{ISIN}\n549300X7CW3B8850WA94,US4642872000\n549300ZZZZZZZZZZZZ01,US4642874329\n")
    return gi.local_file(str(path))


def _gleif_record(lei, name):
    from app.scraper.register_pointer import golden_copy_record
    return golden_copy_record({
        "lei": lei,
        "entity": {"legalName": {"name": name}, "jurisdiction": "US-DE", "status": "ACTIVE",
                   "legalAddress": {"addressLines": ["1221 Avenue of the Americas"], "city": "New York",
                                    "country": "US", "postalCode": "10020", "region": "US-NY"},
                   "registeredAt": {"id": "RA000602"}, "registeredAs": "7718237",
                   "creationDate": "2024-09-09T00:00:00Z"},
        "registration": {"status": "ISSUED", "corroborationLevel": "FULLY_CORROBORATED",
                         "initialRegistrationDate": "2013-10-25T00:00:00Z"}})


@pytest.fixture(autouse=True)
def _gleif():
    with patch("app.scraper.register_pointer._gleif_enabled", return_value=True), \
         patch("app.scraper.register_pointer.fetch_gleif_record",
               side_effect=lambda lei: _gleif_record(lei, "SIRIUS XM INC.") if lei == LEI else None), \
         patch("app.scraper.maintenance._sec_lei_check_enabled", return_value=False):
        yield


def _filer(it_db, cusips=f"['{CUSIP}']", cik=CIK, nid=None, name="SIRIUS XM HOLDINGS INC."):
    it_db.run_command(
        f"CREATE (:Entity {{id: '{nid or 'sec:' + cik}', name: '{name}', name_normalized: 'sirius xm holdings', "
        f"search_text: '{name}', type: 'company', sec_cik: '{cik}', cusips: {cusips}, "
        "source_id: 'sec-edgar', name_credibility: 98})")


def _gleif_node(it_db):
    it_db.run_command(
        f"CREATE (:Entity {{id: 'lei:{LEI}', name: 'SIRIUS XM INC.', name_normalized: 'sirius xm', "
        f"search_text: 'SIRIUS XM INC.', type: 'company', lei_id: '{LEI}', country: 'US', "
        "source_id: 'gleif', name_credibility: 92})")


def _nodes():
    return [dict(r) for r in run_sql(
        "SELECT id, name, sec_cik, lei_id, lei_source, lei_source_url, country FROM Entity ORDER BY id")]


def test_a_join_folds_the_filer_and_the_gleif_node(it_db, mapping):
    _filer(it_db)
    _gleif_node(it_db)
    p = gi.plan(mapping)
    assert [(j["id"], j["lei_node"]) for j in p.joins] == [(f"sec:{CIK}", f"lei:{LEI}")]
    done = gi.apply(p)
    [n] = _nodes()
    assert (n["id"], n["sec_cik"], n["lei_id"]) == (f"lei:{LEI}", CIK, LEI)
    assert n["name"] == "SIRIUS XM HOLDINGS INC."                       # EDGAR's name survives (98 > 92)
    assert n["lei_source"] == f"isin:{ISIN}@2026-11-06" and n["lei_source_url"] == gi.MAPPING_PAGE
    assert n["country"] == "US"                                           # GLEIF's facts carried over
    assert done["joined"][0]["merged"]
    forwarded = {r["old_id"]: r["new_id"] for r in run_sql("SELECT old_id, new_id FROM MergedId")}
    assert forwarded.get(f"sec:{CIK}") == f"lei:{LEI}"


def test_an_attach_has_gleif_write_its_record_then_folds(it_db, mapping):
    _filer(it_db)
    p = gi.plan(mapping)
    assert [a["lei"] for a in p.attaches] == [LEI]
    done = gi.apply(p)
    [n] = _nodes()
    assert (n["id"], n["sec_cik"], n["lei_id"]) == (f"lei:{LEI}", CIK, LEI)
    assert n["lei_source"] == f"isin:{ISIN}@2026-11-06" and n["country"] == "US"
    assert [a["id"] for a in done["attached"]] == [f"sec:{CIK}"]


def test_an_lei_gleif_does_not_know_writes_nothing(it_db, tmp_path):
    other = "549300AAAAAAAAAAAA01"
    path = tmp_path / "isin-lei-20261106T081508.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lei-isin.csv", f"LEI,ISIN\n{other},{ISIN}\n")
    _filer(it_db)
    done = gi.apply(gi.plan(gi.local_file(str(path))))
    assert done["skipped"][0]["why"] == "GLEIF does not know the LEI"
    [n] = _nodes()
    assert n["id"] == f"sec:{CIK}" and n.get("lei_id") is None


def test_an_umbrella_is_left_alone(it_db, mapping):
    _filer(it_db, cusips="['464287200', '464287432']", cik="0000930667", name="iShares, Inc.")
    p = gi.plan(mapping)
    assert [u["id"] for u in p.umbrellas] == ["sec:0000930667"]
    gi.apply(p)
    [n] = _nodes()
    assert n["id"] == "sec:0000930667" and n.get("lei_id") is None


def test_a_pair_kept_apart_is_left_alone(it_db, mapping):
    _filer(it_db)
    _gleif_node(it_db)
    it_db.run_command(f"MATCH (a:Entity {{id: 'sec:{CIK}'}}), (b:Entity {{id: 'lei:{LEI}'}}) "
                      "CREATE (a)-[:NOT_DUPLICATE]->(b)")
    p = gi.plan(mapping)
    assert p.joins == [] and len(p.kept_apart) == 1
    gi.apply(p)
    assert [n["id"] for n in _nodes()] == [f"lei:{LEI}", f"sec:{CIK}"]


def test_a_node_that_took_an_lei_meanwhile_is_not_overwritten(it_db, mapping):
    _filer(it_db)
    _gleif_node(it_db)
    p = gi.plan(mapping)
    it_db.run_command(f"MATCH (e:Entity {{id: 'sec:{CIK}'}}) SET e.lei_id = '549300OTHEROTHER0001'")
    done = gi.apply(p)
    assert done["skipped"][0]["why"] == "the node took an LEI meanwhile"
    assert run_sql(f"SELECT lei_id FROM Entity WHERE id = 'sec:{CIK}'")[0]["lei_id"] == "549300OTHEROTHER0001"


def test_a_pair_kept_apart_after_the_plan_is_left_alone(it_db, mapping):
    _filer(it_db)
    _gleif_node(it_db)
    p = gi.plan(mapping)
    it_db.run_command(f"MATCH (a:Entity {{id: 'sec:{CIK}'}}), (b:Entity {{id: 'lei:{LEI}'}}) "
                      "CREATE (a)-[:NOT_DUPLICATE]->(b)")
    done = gi.apply(p)                     # the marks are read again, not taken from the plan
    assert done["skipped"][0]["why"] == f"kept apart from lei:{LEI}"
    assert [n["id"] for n in _nodes()] == [f"lei:{LEI}", f"sec:{CIK}"]
    assert run_sql(f"SELECT lei_id FROM Entity WHERE id = 'sec:{CIK}'")[0].get("lei_id") is None


def test_the_merge_keeps_a_marked_pair_that_shares_the_lei(it_db):
    # the last line of defence: two nodes already sharing the LEI and kept apart stay two
    _filer(it_db)
    _gleif_node(it_db)
    it_db.run_command(f"MATCH (e:Entity {{id: 'sec:{CIK}'}}) SET e.lei_id = '{LEI}'")
    assert gi._merge_lei_group(LEI, {frozenset((f"sec:{CIK}", f"lei:{LEI}"))}) == []
    assert len(_nodes()) == 2


def test_a_second_run_changes_nothing(it_db, mapping):
    _filer(it_db)
    _gleif_node(it_db)
    gi.apply(gi.plan(mapping))
    before = _nodes()
    p = gi.plan(mapping)
    assert p.joins == [] and p.attaches == [] and len(p.confirmed) == 1
    gi.apply(p)
    assert _nodes() == before


def _ex21_edge(it_db, owner, owned):
    it_db.run_command(
        f"MATCH (a:Entity {{id: '{owner}'}}), (b:Entity {{id: '{owned}'}}) "
        "CREATE (a)-[:OWNS {source_id: 'sec-edgar', filing_type: 'EX-21', until: null}]->(b)")


def test_a_filer_and_its_subsidiary_are_never_joined(it_db, mapping):
    # findings item 94: Sirius XM's 2026 Exhibit 21 lists 'Sirius XM Inc.' — the
    # GLEIF node its old CUSIP maps to — as its subsidiary. Joined, the merge
    # turned the filer's edge into a self-loop.
    _filer(it_db)
    _gleif_node(it_db)
    _ex21_edge(it_db, f"sec:{CIK}", f"lei:{LEI}")
    p = gi.plan(mapping)
    assert p.joins == [] and [c["id"] for c in p.contradictions] == [f"sec:{CIK}"]
    assert gi.apply(p) == {"joined": [], "attached": [], "skipped": []}
    assert [n["id"] for n in _nodes()] == [f"lei:{LEI}", f"sec:{CIK}"]


def test_an_edge_written_after_the_plan_stops_the_join(it_db, mapping):
    _filer(it_db)
    _gleif_node(it_db)
    p = gi.plan(mapping)
    assert len(p.joins) == 1
    _ex21_edge(it_db, f"lei:{LEI}", f"sec:{CIK}")         # either way round
    done = gi.apply(p)
    assert done["joined"] == [] and done["skipped"][0]["why"] == f"lei:{LEI} and the node own one another"
    assert {n["id"]: n["lei_id"] for n in _nodes()} == {f"lei:{LEI}": LEI, f"sec:{CIK}": None}


def test_an_ended_holding_proves_two_entities_too(it_db):
    from app.scraper.maintenance import one_owns_the_other
    _filer(it_db)
    _gleif_node(it_db)
    assert not one_owns_the_other(f"sec:{CIK}", f"lei:{LEI}")
    it_db.run_command(
        f"MATCH (a:Entity {{id: 'sec:{CIK}'}}), (b:Entity {{id: 'lei:{LEI}'}}) "
        "CREATE (a)-[:OWNS {source_id: 'sec-edgar', until: '2020-01-01'}]->(b)")
    assert one_owns_the_other(f"lei:{LEI}", f"sec:{CIK}")
