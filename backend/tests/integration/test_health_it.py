"""GET /scraper/health against a real database: seeded runs, real ImportState
checkpoints, the real endpoint over HTTP — the joined shape, not the mapper."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture()
def seeded(it_db):
    rows = [
        ("h1", "wikidata", "ok", "2026-09-03T10:00:00+00:00", 12, ""),
        ("h2", "wikidata", "failed", "2026-09-03T11:00:00+00:00", 0, "boom https://internal/x"),
        ("h3", "sec-13f", "ok", "2026-09-03T09:00:00+00:00", 89, ""),
        # a running row started long ago -> the stale third state
        ("h4", "open_corporates", "running", "2026-09-01T00:00:00+00:00", 0, ""),
    ]
    for rid, src, status, started, total, err in rows:
        it_db.run_command(
            "CREATE (:ScrapeRun {id: $id, source: $src, target: 't', status: $st, "
            "started_at: $at, finished_at: '', total: $n, error: $e})",
            {"id": rid, "src": src, "st": status, "at": started, "n": total, "e": err})
    it_db.run_sql("INSERT INTO ImportState SET key = 'gleif-update', "
                  "last_publish_date = '2026-09-01 08:00:00', last_run_at = 'x'")
    it_db.run_sql("INSERT INTO ImportState SET key = 'gleif-full-load', "
                  "last_run_at = 'x', scope = 'subset'")
    it_db.run_sql("INSERT INTO ImportState SET key = 'ch-psc-refresh', "
                  "last_run_at = 'x', snapshot_date = '2026-08-30', record_count = 123")
    with TestClient(app) as c:
        yield c


def test_the_joined_shape_and_the_derived_states(seeded):
    body = seeded.get("/v1/scraper/health").json()
    by = {s["name"]: s for s in body["sources"]}

    wd = by["wikidata"]
    assert wd["last_status"] == "failed" and wd["failure_streak"] == 1
    # ArcadeDB re-serialises datetime-ish params in its own ISO flavour —
    # compare the instant, not the spelling.
    assert (wd["last_ok_at"] or "").startswith("2026-09-03T10:00")
    assert wd["label"] == "Wikidata" and wd["enabled"] is True

    assert by["open_corporates"]["last_status"] == "stale"
    assert by["sec-13f"]["label"] == "SEC 13F holders"
    assert by["sec-13f"]["enabled"] is None

    # catalogue sources with no runs still appear
    assert by["bods_gleif"]["last_run_at"] is None

    ds = {d["name"]: d for d in body["datasets"]}
    assert ds["bods_gleif"]["scope"] == "subset"
    assert ds["bods_gleif"]["last_publish_date"] == "2026-09-01 08:00:00"
    assert ds["bods_gleif"]["behind_days"] >= 1
    assert ds["bods_uk_psc"]["record_count"] == 123
    assert body["import_lock"]["held"] is False


def test_redaction_over_the_real_read(seeded, make_token):
    anon = seeded.get("/v1/scraper/health").json()
    wd = next(s for s in anon["sources"] if s["name"] == "wikidata")
    assert "last_error" not in wd

    priv = seeded.get("/v1/scraper/health",
                      headers={"Authorization": f"Bearer {make_token(role='admin')}"}).json()
    wd = next(s for s in priv["sources"] if s["name"] == "wikidata")
    assert wd["last_error"].startswith("boom")
