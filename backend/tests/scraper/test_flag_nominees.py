"""Unit tests for flag_nominee_entities — flagging holders-of-record by name
(DB mocked). End-to-end write is covered against a real ArcadeDB in
tests/integration/test_nominee_it.py."""

from unittest.mock import patch

from app.scraper import maintenance


def test_flags_only_precise_nominee_matches():
    # 5 CONTAINSTEXT candidate queries (one per token); return overlapping rows —
    # a real nominee, a custodian, and a FULL_TEXT false positive ("Cedents Co"
    # matched the 'cede' token but isn't a nominee).
    candidates = [
        [{"id": "n1", "name": "Talbot Nominees Limited"}],   # nominee
        [{"id": "n1", "name": "Talbot Nominees Limited"}],   # nominees (dup id)
        [{"id": "c1", "name": "SF0 Custodian"}],             # custodian
        [],                                                  # custody
        [{"id": "x1", "name": "Cedents Trading Co"}],        # cede token, NOT a nominee
    ]
    with patch.object(maintenance, "run_sql", side_effect=candidates), \
         patch.object(maintenance, "run_sqlscript") as script:
        result = maintenance.flag_nominee_entities()

    assert result["candidates"] == 3          # n1, c1, x1 (deduped by id)
    assert result["flagged"] == 2             # n1 + c1 ; x1 filtered out
    # one UPDATE batch, setting is_nominee on the two matched ids
    assert script.call_count == 1
    stmt, params = script.call_args_list[0].args
    assert "SET is_nominee = true" in stmt
    assert set(params.values()) == {"n1", "c1"}


def test_no_matches_writes_nothing():
    with patch.object(maintenance, "run_sql", side_effect=[[], [], [], [], []]), \
         patch.object(maintenance, "run_sqlscript") as script:
        result = maintenance.flag_nominee_entities()
    assert result == {"candidates": 0, "flagged": 0}
    script.assert_not_called()
