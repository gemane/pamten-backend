"""Pure freshness-decision tests for on-demand scraping (no DB, no network)."""
from datetime import datetime, timedelta, timezone

from app.scraper.ondemand import decide_scrape

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _ent(**kw):
    e = {"id": "e1", "on_demand_scraped": True,
         "last_scraped_at": NOW.isoformat(), "scrape_depth": 1}
    e.update(kw)
    return e


def test_force_always_scrapes_even_when_fresh():
    d = decide_scrape(_ent(scrape_depth=3), requested_depth=1, force=True, now=NOW)
    assert d.should_scrape and d.reason == "forced" and d.need_depth == 1


def test_absent_scrapes():
    d = decide_scrape(None, requested_depth=2, force=False, now=NOW)
    assert d.should_scrape and d.reason == "absent" and d.need_depth == 2


def test_never_on_demand_scrapes():
    d = decide_scrape(_ent(on_demand_scraped=False), requested_depth=1, force=False, now=NOW)
    assert d.should_scrape and d.reason == "never_on_demand"


def test_stale_beyond_ttl_scrapes():
    old = (NOW - timedelta(days=40)).isoformat()
    d = decide_scrape(_ent(last_scraped_at=old), requested_depth=1, force=False, now=NOW)
    assert d.should_scrape and d.reason == "stale"


def test_missing_or_unparseable_timestamp_is_stale():
    assert decide_scrape(_ent(last_scraped_at=None), requested_depth=1, force=False, now=NOW).reason == "stale"
    assert decide_scrape(_ent(last_scraped_at="nonsense"), requested_depth=1, force=False, now=NOW).reason == "stale"


def test_deepen_when_requested_deeper_than_reached():
    d = decide_scrape(_ent(scrape_depth=1), requested_depth=2, force=False, now=NOW)
    assert d.should_scrape and d.reason == "deepen" and d.need_depth == 2


def test_fresh_and_deep_enough_does_not_scrape():
    d = decide_scrape(_ent(scrape_depth=2), requested_depth=1, force=False, now=NOW)
    assert not d.should_scrape and d.reason == "fresh"


def test_just_within_ttl_is_fresh():
    recent = (NOW - timedelta(days=29)).isoformat()
    d = decide_scrape(_ent(last_scraped_at=recent, scrape_depth=2), requested_depth=1, force=False, now=NOW)
    assert not d.should_scrape and d.reason == "fresh"


def test_custom_ttl_is_honoured():
    ts = (NOW - timedelta(days=8)).isoformat()
    assert decide_scrape(_ent(last_scraped_at=ts, scrape_depth=2), requested_depth=1,
                         force=False, now=NOW, ttl_days=7).reason == "stale"
    assert not decide_scrape(_ent(last_scraped_at=ts, scrape_depth=2), requested_depth=1,
                            force=False, now=NOW, ttl_days=30).should_scrape
