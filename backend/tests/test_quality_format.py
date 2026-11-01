"""The quality report's terminal table (`app.quality.format_report`)."""
from app.quality import format_report

REPORT = {"generated_at": "2026-11-01T12:00:00+00:00", "owns_by_source": {},
          "corroboration": {"corroborated": 0, "relationships_with_claims": 0, "corroborated_pct": 0},
          "identity": {"with_official_id": 0, "entities": 0, "with_official_id_pct": 0, "wikidata_only": 0,
                       "wikidata_only_pct": 0, "no_id_at_all": 0},
          "contradictions": {"stake_with_unknown_type": 0, "self_owning_edges": 0,
                             "provenance_mismatches": 0, "stale_without_reason": 0}}


def test_a_dimmed_edge_without_its_evidence_is_a_regression_on_the_report():
    # findings item 32: 81 GLEIF edges were grey with no reason and no date
    assert "stale-without-reason=0" in format_report(REPORT) and "REGRESSION" not in format_report(REPORT)
    bad = {**REPORT, "contradictions": {**REPORT["contradictions"], "stale_without_reason": 81}}
    assert "stale-without-reason=81" in format_report(bad) and "REGRESSION" in format_report(bad)
