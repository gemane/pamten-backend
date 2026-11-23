"""Source call counts against a real ArcadeDB (app/source_calls.py).

The store is one `UPDATE … UPSERT` with `COALESCE(count, 0) + :n` per (UTC day,
source, kind) — arithmetic inside an upsert, which a mocked session accepts
whatever the real engine does with it."""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def calls(it_db):
    from app import source_calls
    source_calls.drain()
    yield source_calls
    source_calls.drain()


def _rows():
    from app.db.arcadedb import run_sql
    return {(r["source"], r["kind"]): r["count"]
            for r in run_sql("SELECT source, kind, count FROM SourceCall")}


def test_two_flushes_add_up_in_one_row(calls):
    calls.count("sec-edgar", "ok", 5)
    calls.count("bucket:edgar", "hit", 2)
    assert calls.flush() == 2
    calls.count("sec-edgar", "ok", 3)
    calls.count("sec-edgar", "429")
    calls.flush()
    assert _rows() == {("sec-edgar", "ok"): 8, ("sec-edgar", "429"): 1, ("bucket:edgar", "hit"): 2}


def test_request_types_are_rows_the_totals_still_sum(calls):
    # findings item 135: the type splits a (source, kind) into several rows;
    # every reader that sums by source and kind must see the same number
    from app import weekly_report
    from app.weekly import iso_week

    calls.count("sec-edgar", "ok", 5, what="filer-record")
    calls.count("sec-edgar", "ok", 2, what="full-text")
    calls.count("sec-edgar", "429", 1, what="filer-record")
    calls.count("gleif", "ok", 4)
    calls.flush()
    calls.count("sec-edgar", "ok", 3, what="filer-record")
    calls.flush()
    today = datetime.now(timezone.utc).date()
    a, b = today.isoformat(), (today + timedelta(days=1)).isoformat()
    assert calls.totals(a, b) == {"gleif": {"ok": 4}, "sec-edgar": {"ok": 10, "429": 1}}
    assert calls.totals_by_type(a, b) == {"sec-edgar": {"filer-record": 9, "full-text": 2}}
    assert calls.totals_by_type(b, "9999-12-31") == {}
    assert weekly_report._source_calls(iso_week())["sources"]["sec-edgar"] == {"total": 11, "ok": 10, "429": 1}
    from app.db.arcadedb import run_sql
    assert len(run_sql("SELECT key FROM SourceCall WHERE source = 'sec-edgar'")) == 3


def test_totals_and_the_weekly_report_read_the_days(calls):
    from app import weekly_report
    from app.weekly import iso_week

    calls.count("gleif", "ok", 4)
    calls.count("bucket:geocode", "miss", 7)
    calls.flush()
    today = datetime.now(timezone.utc).date()
    assert calls.totals(today.isoformat(), (today + timedelta(days=1)).isoformat()) == {
        "bucket:geocode": {"miss": 7}, "gleif": {"ok": 4}}
    assert calls.totals((today + timedelta(days=1)).isoformat(), "9999-12-31") == {}
    assert weekly_report._source_calls(iso_week()) == {
        "sources": {"gleif": {"total": 4, "ok": 4}}, "bucket": {"geocode": {"miss": 7}}}


def test_old_rows_are_pruned(calls):
    from app import analytics
    from app.db.arcadedb import run_sql

    calls.count("wikidata", "ok")
    calls.flush()
    run_sql("UPDATE SourceCall SET last_seen = :t",
            {"t": (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()})
    assert analytics.prune(days=365)["SourceCall"] == 1
    assert _rows() == {}
