"""Tests for backfill_entity_sources — stamping source_id on Wikidata/SEC
entities the scrapers created before they set it (DB mocked)."""

from unittest.mock import patch

from app.scraper import maintenance


def _run(query_side_effect):
    return (
        patch.object(maintenance, "run_query", side_effect=query_side_effect),
        patch.object(maintenance, "run_command"),
    )


def test_stamps_wikidata_then_sec_and_reports_remaining():
    # call order: source lookup Wikidata, source lookup SEC, count wikidata
    # candidates, count sec candidates, count still-missing.
    query_side_effect = [
        [{"id": "WD-SRC"}],   # _source_id("Wikidata")
        [{"id": "SEC-SRC"}],  # _source_id("SEC EDGAR")
        [{"c": 5}],           # wikidata candidates
        [{"c": 3}],           # sec candidates
        [{"c": 2}],           # still missing
    ]
    q, c = _run(query_side_effect)
    with q, c as cmd:
        result = maintenance.backfill_entity_sources()

    assert result["updated"] == {"wikidata": 5, "sec_edgar": 3}
    assert result["still_missing"] == 2
    assert result["wikidata_source_found"] is True
    assert result["sec_edgar_source_found"] is True

    # two SET commands, each with the resolved source id
    assert cmd.call_count == 2
    wd_stmt, wd_params = cmd.call_args_list[0].args
    assert "e.wikidata_id IS NOT NULL" in wd_stmt and wd_params == {"sid": "WD-SRC"}
    sec_stmt, sec_params = cmd.call_args_list[1].args
    assert "e.sec_cik IS NOT NULL" in sec_stmt and sec_params == {"sid": "SEC-SRC"}


def test_skips_pass_when_source_node_missing():
    # No Wikidata Source node → skip that pass; SEC still runs.
    query_side_effect = [
        [],                   # _source_id("Wikidata") missing
        [{"id": "SEC-SRC"}],  # _source_id("SEC EDGAR")
        [{"c": 4}],           # sec candidates
        [{"c": 0}],           # still missing
    ]
    q, c = _run(query_side_effect)
    with q, c as cmd:
        result = maintenance.backfill_entity_sources()

    assert result["updated"] == {"wikidata": 0, "sec_edgar": 4}
    assert result["wikidata_source_found"] is False
    assert result["sec_edgar_source_found"] is True
    # only the SEC SET ran
    assert cmd.call_count == 1
    assert "e.sec_cik IS NOT NULL" in cmd.call_args_list[0].args[0]


def test_idempotent_when_nothing_left_to_stamp():
    query_side_effect = [
        [{"id": "WD-SRC"}], [{"id": "SEC-SRC"}],
        [{"c": 0}],  # wikidata candidates
        [{"c": 0}],  # sec candidates
        [{"c": 0}],  # still missing
    ]
    q, c = _run(query_side_effect)
    with q, c as cmd:
        result = maintenance.backfill_entity_sources()
    assert result["updated"] == {"wikidata": 0, "sec_edgar": 0}
    assert result["still_missing"] == 0
    # SET still issued (harmless no-op match), but counts are zero
    assert cmd.call_count == 2
