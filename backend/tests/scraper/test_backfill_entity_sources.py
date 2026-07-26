"""Tests for backfill_entity_sources — stamping source_id on Wikidata/SEC
entities the scrapers created before they set it (DB mocked).

Reads go through run_sql (SELECT); the actual writes go through run_sqlscript
(the only path proven to commit on this ArcadeDB), updating candidates by id."""

from unittest.mock import patch

from app.scraper import maintenance


def _run(sql_side_effect):
    return (
        patch.object(maintenance, "run_sql", side_effect=sql_side_effect),
        patch.object(maintenance, "run_sqlscript"),
    )


def test_stamps_wikidata_then_sec_and_reports_remaining():
    # run_sql order: source lookup Wikidata, source lookup SEC, wikidata
    # candidate ids, sec candidate ids, remaining count.
    sql_side_effect = [
        [{"id": "WD-SRC"}],              # _source_id("Wikidata")
        [{"id": "SEC-SRC"}],             # _source_id("SEC EDGAR")
        [{"id": "e1"}, {"id": "e2"}],    # wikidata candidate ids
        [{"id": "e3"}],                  # sec candidate ids
        [{"c": 4}],                      # remaining still-null
    ]
    q, s = _run(sql_side_effect)
    with q, s as script:
        result = maintenance.backfill_entity_sources()

    assert result["updated"] == {"wikidata": 2, "sec_edgar": 1}
    assert result["still_missing"] == 4
    assert result["wikidata_source_found"] is True
    assert result["sec_edgar_source_found"] is True

    # one sqlscript per pass; each an UPDATE-by-id carrying the resolved source id
    assert script.call_count == 2
    wd_stmt, wd_params = script.call_args_list[0].args
    assert "UPDATE Entity SET source_id = :sid WHERE id = :id0" in wd_stmt
    assert wd_params["sid"] == "WD-SRC"
    assert wd_params["id0"] == "e1" and wd_params["id1"] == "e2"
    sec_stmt, sec_params = script.call_args_list[1].args
    assert sec_params["sid"] == "SEC-SRC" and sec_params["id0"] == "e3"


def test_skips_pass_when_source_node_missing():
    # No Wikidata Source node → skip that pass; SEC still runs.
    sql_side_effect = [
        [],                   # _source_id("Wikidata") missing
        [{"id": "SEC-SRC"}],  # _source_id("SEC EDGAR")
        [{"id": "e3"}],       # sec candidate ids
        [{"c": 0}],           # remaining
    ]
    q, s = _run(sql_side_effect)
    with q, s as script:
        result = maintenance.backfill_entity_sources()

    assert result["updated"] == {"wikidata": 0, "sec_edgar": 1}
    assert result["wikidata_source_found"] is False
    assert result["sec_edgar_source_found"] is True
    # only the SEC pass wrote
    assert script.call_count == 1
    assert script.call_args_list[0].args[1]["sid"] == "SEC-SRC"


def test_no_write_when_nothing_to_stamp():
    sql_side_effect = [
        [{"id": "WD-SRC"}], [{"id": "SEC-SRC"}],
        [],          # wikidata candidates: none
        [],          # sec candidates: none
        [{"c": 0}],  # remaining
    ]
    q, s = _run(sql_side_effect)
    with q, s as script:
        result = maintenance.backfill_entity_sources()
    assert result["updated"] == {"wikidata": 0, "sec_edgar": 0}
    assert result["still_missing"] == 0
    script.assert_not_called()  # no candidate ids → no write issued


def test_batches_large_candidate_sets():
    # 250 wikidata candidates with chunk=200 → 2 sqlscript writes.
    ids = [{"id": f"e{i}"} for i in range(250)]
    sql_side_effect = [
        [{"id": "WD-SRC"}], [{"id": "SEC-SRC"}],
        ids,          # wikidata candidates
        [],           # sec candidates
        [{"c": 0}],   # remaining
    ]
    q, s = _run(sql_side_effect)
    with q, s as script:
        result = maintenance.backfill_entity_sources()
    assert result["updated"]["wikidata"] == 250
    assert script.call_count == 2  # 200 + 50
