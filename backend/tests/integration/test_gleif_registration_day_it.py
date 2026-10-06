"""
The registration-day rule against a real ArcadeDB: a GLEIF start that is only
the child's LEI registration day becomes "at least since" — after the bulk
import (in batches) and on every delta write — and nothing else is touched.
"""
import pytest

from app.scraper.gleif_rr import REGISTRATION_DAY, mark_registration_day

pytestmark = pytest.mark.integration


def _company(it_db, eid, registered=None, founded=None):
    it_db.run_command("CREATE (:Entity {id: $id, name: $id, type: 'company', lei_id: $id, "
                      "lei_registration_date: $r, founded_date: $f})",
                      {"id": eid, "r": registered, "f": founded})


def _edge(it_db, parent, child, **props):
    sets = ", ".join(f"{k}: ${k}" for k in props)
    it_db.run_command(f"MATCH (a:Entity {{id: $p}}), (b:Entity {{id: $c}}) "
                      f"CREATE (a)-[:OWNS {{{sets}}}]->(b)", {"p": parent, "c": child, **props})


def _basis(it_db, child):
    return it_db.run_command("MATCH (:Entity)-[r:OWNS]->(:Entity {id: $c}) RETURN r.since_basis AS b",
                             {"c": child})[0]["b"]


def test_only_a_gleif_start_on_the_registration_day_is_labelled(it_db):
    _company(it_db, "parent")
    _company(it_db, "bank", registered="2012-06-06", founded="1925-01-01")      # Barclays Bank
    _company(it_db, "bank2", registered="2013-02-02", founded="1990-01-01")
    _company(it_db, "new", registered="2020-03-01", founded="2020-03-01")       # founded = registered
    _company(it_db, "other", registered="2012-06-06")                           # another day stated
    _company(it_db, "sec", registered="2015-01-01")
    _company(it_db, "listed", registered="2014-08-14")
    _edge(it_db, "parent", "bank", filing_type="RR", since="2012-06-06")
    _edge(it_db, "parent", "bank2", filing_type="RR", since="2013-02-02")
    _edge(it_db, "parent", "new", filing_type="RR", since="2020-03-01")
    _edge(it_db, "parent", "other", filing_type="RR", since="2010-01-01")
    _edge(it_db, "parent", "sec", filing_type="13D", since="2015-01-01")       # not GLEIF's
    _edge(it_db, "parent", "listed", filing_type="RR", since="2014-08-14", since_basis="first_listed")
    assert mark_registration_day(batch=1) == 2                                  # the batch loop
    assert _basis(it_db, "bank") == _basis(it_db, "bank2") == REGISTRATION_DAY
    assert [_basis(it_db, c) for c in ("new", "other", "sec")] == [None, None, None]
    assert _basis(it_db, "listed") == "first_listed"
    assert mark_registration_day() == 0                                         # idempotent


def test_the_delta_labels_it_and_a_restatement_keeps_it(it_db):
    from app.scraper.gleif_incremental import _owns_edge_upsert
    _company(it_db, "lei:PARENT", registered="2012-01-01")
    _company(it_db, "lei:BANK", registered="2012-06-06", founded="1925-01-01")
    for _ in range(2):          # created, then restated by a later delta
        _owns_edge_upsert("lei:PARENT", "lei:BANK", "BANK", "direct", "gleif", 92,
                          since="2012-06-06", recorded="2026-04-13")
    rows = it_db.run_command("MATCH (:Entity {id: 'lei:PARENT'})-[r:OWNS]->(:Entity {id: 'lei:BANK'}) "
                             "RETURN r.since AS s, r.since_basis AS b")
    assert rows == [{"s": "2012-06-06", "b": REGISTRATION_DAY}]
    claim = it_db.run_command("MATCH (c:Claim {from_id: 'lei:PARENT', to_id: 'lei:BANK'}) "
                              "RETURN c.since AS s, c.since_basis AS b")
    assert claim[0]["s"] == "2012-06-06" and claim[0]["b"] is None         # GLEIF's date, as stated
