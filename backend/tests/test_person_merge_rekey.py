"""The callers of `merge_person_records` continue under the id it returns
(findings item 39): the auto-dedup merges a group's next twin into the
re-keyed survivor and reports that id; the merge endpoint answers with it."""
from unittest.mock import patch

from app.models.person import PersonMergeRequest
from app.routers import persons

GROUP = {"confidence": "high", "likely_distinct": False, "suggested_keep_id": "wd:Q912811",
         "members": [{"id": "wd:Q912811", "full_name": "Brian Moynihan"},
                     {"id": "sec:0001195071", "full_name": "Brian T Moynihan"},
                     {"id": "u-3", "full_name": "Moynihan Brian T"}]}


def test_the_auto_dedup_follows_the_survivor_to_its_new_id():
    calls = []

    def merge(keep, dup):
        calls.append((keep, dup))
        return "sec:0001195071" if dup == "sec:0001195071" else keep

    with patch.object(persons, "scan_duplicate_groups", return_value=[GROUP]), \
         patch.object(persons, "merge_person_records", side_effect=merge):
        res = persons.deduplicate_high_confidence(apply=True)
    # the second twin is merged into the RE-KEYED survivor, not the id it had
    assert calls == [("wd:Q912811", "sec:0001195071"), ("sec:0001195071", "u-3")]
    assert res["merged"] == [{"keep_id": "sec:0001195071", "keep_name": "Brian Moynihan",
                              "merged": ["Brian T Moynihan", "Moynihan Brian T"]}]


def test_a_dry_run_reports_the_suggested_survivor_unchanged():
    with patch.object(persons, "scan_duplicate_groups", return_value=[GROUP]), \
         patch.object(persons, "merge_person_records") as merge:
        res = persons.deduplicate_high_confidence(apply=False)
    merge.assert_not_called()
    assert res["merged"][0]["keep_id"] == "wd:Q912811"


def test_the_merge_endpoint_answers_with_the_id_the_person_lives_under():
    with patch.object(persons, "merge_person_records", return_value="sec:0001195071") as merge:
        out = persons.merge_persons(PersonMergeRequest(keep_id="wd:Q912811", dup_id="sec:0001195071"),
                                    _={"role": "contributor"})
    merge.assert_called_once_with("wd:Q912811", "sec:0001195071")
    assert out == {"message": "Persons merged", "keep_id": "sec:0001195071", "removed_id": "sec:0001195071"}
