"""
Unit tests for resolve_entity_id — the sequential, index-backed replacement for
the OR-based entity resolution that full-scans the Entity type on ArcadeDB.
"""
from app.entity_resolution import resolve_entity_id


def test_short_circuits_on_first_matching_field(fake_db):
    fake_db.queue([{"id": "e1"}])   # wikidata_id lookup hits immediately
    got = resolve_entity_id(fake_db, wikidata_id="Q1", name="Acme", name_normalized="acme")
    assert got == "e1"
    # only one lookup ran — it did NOT go on to name / name_normalized
    assert len(fake_db.calls) == 1
    assert "e.wikidata_id = $v" in fake_db.calls[0][0]


def test_falls_through_to_next_field_on_miss(fake_db):
    fake_db.queue([])               # wikidata_id: no match
    fake_db.queue([{"id": "e2"}])   # name: match
    got = resolve_entity_id(fake_db, wikidata_id="Q1", name="Acme")
    assert got == "e2"
    assert len(fake_db.calls) == 2
    assert "e.name = $v" in fake_db.calls[1][0]


def test_skips_falsy_identifiers(fake_db):
    fake_db.queue([{"id": "e3"}])   # first non-empty field (name_normalized)
    got = resolve_entity_id(fake_db, wikidata_id=None, sec_cik="", name_normalized="acme")
    assert got == "e3"
    assert len(fake_db.calls) == 1
    assert "e.name_normalized = $v" in fake_db.calls[0][0]


def test_returns_none_when_nothing_matches(fake_db):
    fake_db.queue([])
    fake_db.queue([])
    assert resolve_entity_id(fake_db, wikidata_id="Q1", name="Acme") is None


def test_honours_priority_order(fake_db):
    # external ids are checked before name — sec_cik wins over name here
    fake_db.queue([{"id": "cik-hit"}])
    got = resolve_entity_id(fake_db, name="Acme", sec_cik="0000320193")
    assert got == "cik-hit"
    assert "e.sec_cik = $v" in fake_db.calls[0][0]


def test_custom_label(fake_db):
    fake_db.queue([{"id": "c1"}])
    resolve_entity_id(fake_db, label="Company", name_normalized="acme")
    assert "MATCH (e:Company)" in fake_db.calls[0][0]
