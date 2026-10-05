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
