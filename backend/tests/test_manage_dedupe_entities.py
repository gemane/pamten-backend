"""`manage.py dedupe-entities` reports what it did — the summary line printed
"across ? shared-id groups" because it read a key the function never returns."""
from types import SimpleNamespace

import manage


def test_the_summary_names_the_groups(monkeypatch, capsys):
    from app.scraper import maintenance
    monkeypatch.setattr(maintenance, "deduplicate_entities", lambda limit=None: {
        "entities_merged": 13, "groups_processed": 13, "duplicate_groups_found": 13,
        "remaining": 0, "detail": []})
    manage.cmd_dedupe_entities(SimpleNamespace(limit=None, db_url=None))
    assert capsys.readouterr().out.strip() == \
        "Merged 13 entities across 13 shared-id groups; 0 groups remaining"


def test_both_bridges_are_reported(monkeypatch, capsys):
    # the 10-31 rebuild stopped here: a PSC join carries no cik_id or source_url
    from app.scraper import maintenance
    monkeypatch.setattr(maintenance, "deduplicate_entities", lambda limit=None: {
        "entities_merged": 2, "groups_processed": 2, "remaining": 0, "detail": [], "lei_bridged": [
            {"cik_id": "sec:0000217410", "lei_id": "549300MKFYEKVRWML317", "name": "UNILEVER PLC",
             "source_url": "https://www.sec.gov/x.htm"},
            {"psc_id": "chpsc:03230337:x", "lei_id": "5493000C01ZX7D35SD85", "lei_node_id": "lei:5493000C01ZX7D35SD85",
             "name": "Berkshire Hathaway Inc.", "evidence": "name+place+hq"}]})
    manage.cmd_dedupe_entities(SimpleNamespace(limit=None, db_url=None))
    assert capsys.readouterr().out.splitlines() == [
        "  UNILEVER PLC: CIK node sec:0000217410 takes LEI 549300MKFYEKVRWML317 (https://www.sec.gov/x.htm)",
        "  Berkshire Hathaway Inc.: PSC node chpsc:03230337:x takes LEI 5493000C01ZX7D35SD85 (name+place+hq)",
        "Merged 2 entities across 2 shared-id groups; 0 groups remaining"]
