"""Tests for backfill_entity_sources — stamping source_id on Wikidata/SEC
entities the scrapers created before they set it (DB mocked)."""

from unittest.mock import patch

from app.scraper import maintenance


def _run(sql_side_effect):
    # Reads (source lookup, counts) and writes (UPDATE) both go through run_sql;
    # SELECTs return rows from the side-effect list, UPDATEs return [].
    return patch.object(maintenance, "run_sql", side_effect=sql_side_effect)


def test_stamps_wikidata_then_sec_and_reports_remaining():
    # run_sql call order: source lookup Wikidata, source lookup SEC, count
    # wikidata candidates, UPDATE wikidata, count sec candidates, UPDATE sec,
    # count still-missing.
    sql_side_effect = [
        [{"id": "WD-SRC"}],   # _source_id("Wikidata")
        [{"id": "SEC-SRC"}],  # _source_id("SEC EDGAR")
        [{"c": 5}],           # wikidata candidates
        [],                   # UPDATE wikidata
        [{"c": 3}],           # sec candidates
        [],                   # UPDATE sec
        [{"c": 2}],           # still missing
    ]
    with _run(sql_side_effect) as sql:
        result = maintenance.backfill_entity_sources()

    assert result["updated"] == {"wikidata": 5, "sec_edgar": 3}
    assert result["still_missing"] == 2
    assert result["wikidata_source_found"] is True
    assert result["sec_edgar_source_found"] is True

    # the two writes are SQL UPDATEs, each with the resolved source id
    updates = [c for c in sql.call_args_list if c.args[0].startswith("UPDATE Entity")]
    assert len(updates) == 2
    assert "wikidata_id IS NOT NULL" in updates[0].args[0] and updates[0].args[1] == {"sid": "WD-SRC"}
    assert "sec_cik IS NOT NULL" in updates[1].args[0] and updates[1].args[1] == {"sid": "SEC-SRC"}


def test_skips_pass_when_source_node_missing():
    # No Wikidata Source node → skip that pass; SEC still runs.
    sql_side_effect = [
        [],                   # _source_id("Wikidata") missing
        [{"id": "SEC-SRC"}],  # _source_id("SEC EDGAR")
        [{"c": 4}],           # sec candidates
        [],                   # UPDATE sec
        [{"c": 0}],           # still missing
    ]
    with _run(sql_side_effect) as sql:
        result = maintenance.backfill_entity_sources()

    assert result["updated"] == {"wikidata": 0, "sec_edgar": 4}
    assert result["wikidata_source_found"] is False
    assert result["sec_edgar_source_found"] is True
    # only the SEC UPDATE ran
    updates = [c for c in sql.call_args_list if c.args[0].startswith("UPDATE Entity")]
    assert len(updates) == 1
    assert "sec_cik IS NOT NULL" in updates[0].args[0]


def test_idempotent_when_nothing_left_to_stamp():
    sql_side_effect = [
        [{"id": "WD-SRC"}], [{"id": "SEC-SRC"}],
        [{"c": 0}], [],  # wikidata candidates, UPDATE
        [{"c": 0}], [],  # sec candidates, UPDATE
        [{"c": 0}],      # still missing
    ]
    with _run(sql_side_effect) as sql:
        result = maintenance.backfill_entity_sources()
    assert result["updated"] == {"wikidata": 0, "sec_edgar": 0}
    assert result["still_missing"] == 0
    # UPDATE still issued (harmless no-op match), but counts are zero
    updates = [c for c in sql.call_args_list if c.args[0].startswith("UPDATE Entity")]
    assert len(updates) == 2
