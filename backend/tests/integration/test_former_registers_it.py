"""Former register identities: the backfill, the delta's preservation, and the
dedup merge they exist to enable — against a real database.

The scenario is Tesla's: GLEIF moved the company to a new register (Delaware →
Texas 2024) and the current golden copy no longer knows the old pair, while a
PSC filer still states it. History recovered from a snapshot bridges the two.
"""
import csv
import io
import zipfile

import pytest

pytestmark = pytest.mark.integration

_COLS = ["LEI",
         "Entity.RegistrationAuthority.RegistrationAuthorityID",
         "Entity.RegistrationAuthority.RegistrationAuthorityEntityID"]


def _snapshot(tmp_path, rows):
    """A minimal golden-copy-shaped zip: one CSV, the three columns we read."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS)
    w.writeheader()
    for lei, ra, num in rows:
        w.writerow(dict(zip(_COLS, (lei, ra, num))))
    path = tmp_path / "20231229-0000-gleif-goldencopy-lei2.csv.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("20231229-0000-gleif-goldencopy-lei2.csv", buf.getvalue())
    return str(path)


def _tesla(it_db):
    it_db.run_command(
        "CREATE (:Entity {id: 'lei:TESLA000LEI000000001', name: 'TESLA, INC.', "
        "name_normalized: 'tesla', search_text: 'TESLA, INC.', type: 'company', "
        "lei_id: 'TESLA000LEI000000001', register_id: 'RA000637:0805587591', "
        "name_credibility: 92})")  # GLEIF stamps 92; the survivor pick depends on it


def test_the_backfill_recovers_a_moved_register(it_db, tmp_path):
    from app.scraper.register_history import backfill_former_registers
    _tesla(it_db)
    snap = _snapshot(tmp_path, [
        ("TESLA000LEI000000001", "RA000602", "3903573"),   # the 2023 Delaware pair
        ("SOMEOTHERLEI00000002", "RA000585", "07524813"),  # not in this database
    ])
    counts = backfill_former_registers([snap])
    assert counts["backfilled"] == 1 and counts["matched"] == 1
    row = it_db.run_sql("SELECT former_register_ids FROM Entity "
                        "WHERE id = 'lei:TESLA000LEI000000001'")[0]
    assert row["former_register_ids"] == ["RA000602:3903573"]


def test_the_backfill_is_idempotent_and_skips_the_current_pair(it_db, tmp_path):
    from app.scraper.register_history import backfill_former_registers
    _tesla(it_db)
    snap = _snapshot(tmp_path, [
        ("TESLA000LEI000000001", "RA000602", "3903573"),
        ("TESLA000LEI000000001", "RA000637", "0805587591"),  # today's pair — no history
    ])
    backfill_former_registers([snap])
    counts = backfill_former_registers([snap])
    assert counts["backfilled"] == 0, "a second run finds nothing new"
    row = it_db.run_sql("SELECT former_register_ids FROM Entity "
                        "WHERE id = 'lei:TESLA000LEI000000001'")[0]
    assert row["former_register_ids"] == ["RA000602:3903573"]


def test_a_placeholder_ra_never_becomes_history(it_db, tmp_path):
    from app.scraper.register_history import backfill_former_registers
    _tesla(it_db)
    snap = _snapshot(tmp_path, [("TESLA000LEI000000001", "RA999999", "123")])
    counts = backfill_former_registers([snap])
    assert counts["backfilled"] == 0


def test_the_recovered_pair_merges_the_psc_twin(it_db, tmp_path):
    """The point of the whole feature: the node that still states the OLD
    register hard-merges with the node that moved on."""
    from app.scraper.register_history import backfill_former_registers
    from app.scraper.maintenance import deduplicate_entities
    _tesla(it_db)
    it_db.run_command(
        "CREATE (:Entity {id: 'chpsc:09533203:xyz', name: 'Tesla, Inc.', "
        "name_normalized: 'tesla', search_text: 'Tesla, Inc.', type: 'company', "
        "register_id: 'RA000602:3903573'})")
    backfill_former_registers([_snapshot(tmp_path, [
        ("TESLA000LEI000000001", "RA000602", "3903573")])])
    result = deduplicate_entities(limit=None)
    assert result["entities_merged"] == 1
    survivors = it_db.run_sql(
        "SELECT id FROM Entity WHERE name_normalized = 'tesla'")
    assert [dict(s)["id"] for s in survivors] == ["lei:TESLA000LEI000000001"]


def test_the_delta_preserves_the_outgoing_pair(it_db, monkeypatch, tmp_path):
    """A registration move applied by the daily delta keeps the old pair."""
    import json, gzip
    from app.scraper import gleif_incremental as gi
    _tesla(it_db)
    rec = {"LEI": {"$": "TESLA000LEI000000001"},
           "Entity": {
               "LegalName": {"$": "TESLA, INC."},
               "LegalAddress": {"FirstAddressLine": {"$": "x"}, "Country": {"$": "US"}},
               "HeadquartersAddress": {"FirstAddressLine": {"$": "x"}, "Country": {"$": "US"}},
               "RegistrationAuthority": {
                   "RegistrationAuthorityID": {"$": "RA000602"},
                   "RegistrationAuthorityEntityID": {"$": "3903573"}},
               "EntityStatus": {"$": "ACTIVE"}},
           "Registration": {"RegistrationStatus": {"$": "ISSUED"}}}
    path = tmp_path / "delta.json"
    path.write_text(json.dumps({"records": [rec]}))
    counts = gi.import_lei_cdf_delta(str(path), "src1", 98)
    assert counts["updated"] == 1
    assert counts.get("register_moves") == 1
    row = it_db.run_sql("SELECT register_id, former_register_ids FROM Entity "
                        "WHERE id = 'lei:TESLA000LEI000000001'")[0]
    assert row["register_id"] == "RA000602:3903573"
    assert row["former_register_ids"] == ["RA000637:0805587591"]


def test_the_backfill_renormalizes_mixed_vintage_keys_and_the_twins_merge(it_db, tmp_path):
    """A full import minted RA000637:0805587591 before zero-normalization; the
    freshly-minted PSC twin says :805587591. The backfill renormalizes stored
    keys, and the dedup then sees one key on two nodes."""
    from app.scraper.register_history import backfill_former_registers
    from app.scraper.maintenance import deduplicate_entities
    _tesla(it_db)   # stores the padded RA000637:0805587591
    it_db.run_command(
        "CREATE (:Entity {id: 'chpsc:09533203:xyz', name: 'Tesla, Inc.', "
        "name_normalized: 'tesla', search_text: 'Tesla, Inc.', type: 'company', "
        "register_id: 'RA000637:805587591'})")
    counts = backfill_former_registers([_snapshot(tmp_path, [])])
    assert counts["renormalized"] == 1
    result = deduplicate_entities(limit=None)
    assert result["entities_merged"] == 1
    survivors = it_db.run_sql("SELECT id FROM Entity WHERE name_normalized = 'tesla'")
    assert [dict(s)["id"] for s in survivors] == ["lei:TESLA000LEI000000001"]


def test_the_corporate_psc_write_path_lands_the_register_fields(it_db):
    """The mapper computed register_id for months while the writer dropped it —
    this drives the WRITE path, which the mapper unit tests cannot see."""
    from app.scraper.bulk_import import _BatchWriter
    from app.scraper.companies_house_psc import _process
    rec = {"company_number": "09533203", "data": {
        "kind": "corporate-entity-person-with-significant-control",
        "name": "Tesla, Inc.",
        "identification": {"registration_number": "805587591",
                           "country_registered": "United States",
                           "legal_authority": "Texas",
                           "place_registered": "N/A"},
        "links": {"self": "/company/09533203/persons-with-significant-control/corporate-entity/x"},
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}}
    batch = _BatchWriter()
    assert _process(rec, batch, "src1", 80) == "entity"
    batch.flush()
    row = it_db.run_sql("SELECT register_id, registration_number, registration_authority "
                        "FROM Entity WHERE id = 'chpsc:09533203:x'")[0]
    assert row["register_id"] == "RA000637:805587591"
    assert row["registration_number"] == "805587591"
