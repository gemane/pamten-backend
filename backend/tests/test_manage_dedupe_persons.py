"""`manage.py dedupe-persons` — the net under the post-scrape person dedup
(findings item 55): a full scan by default, the hard-id seeds on `--scraped-only`,
nothing written on `--dry-run`, and a report a human can read."""
from types import SimpleNamespace

import manage


def _result(**over):
    base = {"applied": True, "merged_count": 2, "review_count": 10,
            "merged": [{"keep_id": "wd:Q3215817", "keep_name": "Lachlan Murdoch", "merged": ["Lachlan K. Murdoch"]},
                       {"keep_id": "wd:Q7346054", "keep_name": "Robert James Thomson", "merged": ["Robert J Thomson"]}],
            "needs_review": []}
    return {**base, **over}


def _wire(monkeypatch, result):
    from app.routers import persons
    calls = []

    def fake_dedup(apply=True, seed_ids=None):
        calls.append({"apply": apply, "seed_ids": seed_ids})
        return result

    monkeypatch.setattr(persons, "deduplicate_high_confidence", fake_dedup)
    monkeypatch.setattr(manage, "_hard_id_person_ids", lambda: ["sec:0001242818", "wd:Q3215817"])
    return calls


def test_the_default_is_the_full_scan_applied(monkeypatch, capsys):
    calls = _wire(monkeypatch, _result())
    manage.cmd_dedupe_persons(SimpleNamespace(scraped_only=False, dry_run=False, db_url=None))
    assert calls == [{"apply": True, "seed_ids": None}]
    assert capsys.readouterr().out.splitlines() == [
        "  Lachlan Murdoch (wd:Q3215817) takes Lachlan K. Murdoch",
        "  Robert James Thomson (wd:Q7346054) takes Robert J Thomson",
        "Merged 2 persons across 2 groups; 10 groups need review"]


def test_scraped_only_seeds_the_scoped_scan_from_the_hard_ids(monkeypatch):
    calls = _wire(monkeypatch, _result())
    manage.cmd_dedupe_persons(SimpleNamespace(scraped_only=True, dry_run=False, db_url=None))
    assert calls == [{"apply": True, "seed_ids": ["sec:0001242818", "wd:Q3215817"]}]


def test_a_dry_run_applies_nothing_and_says_so(monkeypatch, capsys):
    calls = _wire(monkeypatch, _result(applied=False, merged_count=1, review_count=0,
                                       merged=[{"keep_id": "wd:Q3215817", "keep_name": "Lachlan Murdoch",
                                                "merged": ["Lachlan K. Murdoch"]}]))
    manage.cmd_dedupe_persons(SimpleNamespace(scraped_only=False, dry_run=True, db_url=None))
    assert calls == [{"apply": False, "seed_ids": None}]
    assert capsys.readouterr().out.splitlines()[-1] == "Would merge 1 persons across 1 groups; 0 groups need review"


def test_the_command_is_registered_with_the_flags_it_reads():
    args = manage._build_parser().parse_args(["dedupe-persons", "--scraped-only", "--dry-run"])
    assert args.scraped_only is True and args.dry_run is True and args.func is manage.cmd_dedupe_persons
    plain = manage._build_parser().parse_args(["dedupe-persons"])
    assert plain.scraped_only is False and plain.dry_run is False
