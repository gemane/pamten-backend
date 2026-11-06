"""The SEC's seat-holders pass runs once more when a later source wrote seats
(findings item 67). The registers run before the knowledge bases (item 60), so
on the SEC's own run Wikidata's officers are not on the graph yet: in the
2026-11-06 rebuild 22 persons kept their CIK and lost their item id, five lost
their Form 4 holding and their CIK. Both dispatchers, `run_scrape_all` and the
on-demand `_run_instant_sources`, repeat the pass once, and only for seat
holders without a holding the SEC run did not look up (its `seats_asked`).
Registry, graph and the pass are faked (no DB/network)."""
import contextlib

import pytest

import app.scraper.runner  # noqa: F401 - registers the built-ins before the swap
from app.config import settings
from app.scraper import ondemand
from app.scraper.scraper_registry import ScraperSpec, register


@pytest.fixture
def world(monkeypatch, fake_sources):
    """A fake `sec_edgar` spec that writes the filer and looks up the seats the
    graph has at that moment, and a fake `wd` spec that adds a seat; `seats` is
    what the graph answers for the filer's seat holders without a holding."""
    fake_sources("wd")
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
    state = {"calls": [], "seats": set(), "wd_adds": {"wd:Q92764"}, "sec_status": "ok"}
    monkeypatch.setattr("app.scraper.runner._stored_entity", lambda q, c=None: None)
    monkeypatch.setattr("app.scraper.runner.seat_holders_without_holdings",
                        lambda eid: frozenset(state["seats"]) if eid else frozenset())

    def sec(q, d, c=None):
        state["calls"].append("sec_edgar")
        return {"status": state["sec_status"], "total": 38, "cik": "0001652044",
                "entity_id": "sec:0001652044", "company": "Alphabet Inc.", "seats_asked": sorted(state["seats"])}

    def wd(q, d, c=None):
        state["calls"].append("wd")
        state["seats"] |= state["wd_adds"]
        return {"status": "ok", "total": 3}

    def seat_pass(entity_id, cik, company_name):
        state["calls"].append("seats")
        state["pass_args"] = (entity_id, cik, company_name)
        return {"status": "ok", "company": company_name, "entity_id": entity_id, "total": 1, "scraped": [],
                "seats_asked": sorted(state["seats"])}
    monkeypatch.setattr("app.scraper.runner.run_sec_seat_holders", seat_pass)
    register(ScraperSpec("sec_edgar", sec, lambda: True))
    register(ScraperSpec("wd", wd, lambda: True, depth_aware=True))
    return state


class TestRunScrapeAll:
    def _run(self):
        from app.scraper.runner import run_scrape_all
        return run_scrape_all("Alphabet", depth=2)

    def test_seats_a_later_source_wrote_get_the_pass_once_more(self, world):
        out = self._run()
        assert world["calls"] == ["sec_edgar", "wd", "seats"]
        assert world["pass_args"] == ("sec:0001652044", "0001652044", "Alphabet Inc.")
        assert out["results"]["sec_edgar"]["seats_rerun"]["total"] == 1
        assert out["results"]["sec_edgar"]["status"] == "ok"       # the run's own result stays

    def test_seats_the_sec_run_looked_up_ask_nothing(self, world):
        world["seats"] = {"wd:Q92764"}                           # a re-scrape: the SEC run saw the seat
        out = self._run()
        assert world["calls"] == ["sec_edgar", "wd"]
        assert "seats_rerun" not in out["results"]["sec_edgar"]

    def test_a_source_that_adds_no_seat_asks_nothing(self, world):
        world["wd_adds"] = set()
        self._run()
        assert world["calls"] == ["sec_edgar", "wd"]

    def test_a_partial_sec_run_still_gets_the_pass(self, world):
        world["sec_status"] = "partial"
        out = self._run()
        assert world["calls"] == ["sec_edgar", "wd", "seats"]
        assert out["results"]["sec_edgar"]["status"] == "partial"

    def test_a_failed_pass_is_reported_not_raised(self, world, monkeypatch):
        def boom(entity_id, cik, company_name):
            raise RuntimeError("EDGAR down")
        monkeypatch.setattr("app.scraper.runner.run_sec_seat_holders", boom)
        out = self._run()
        assert out["results"]["sec_edgar"]["seats_rerun"] == {"status": "error", "detail": "EDGAR down"}
        assert out["results"]["sec_edgar"]["status"] == "ok"


def test_the_on_demand_dispatcher_runs_the_pass_and_logs_why(world, monkeypatch):
    reasons = []

    @contextlib.contextmanager
    def fake_record_run(source, target, reason=None):
        row = {}
        reasons.append((source, reason, row))
        yield row

    monkeypatch.setattr("app.scraper.run_log.record_run", fake_record_run)
    out = ondemand._run_instant_sources("Alphabet", ondemand.ScrapeDecision(True, "absent", 1))
    assert world["calls"] == ["sec_edgar", "wd", "seats"]
    assert [(s, r) for s, r, _ in reasons] == [("sec_edgar", "absent"), ("wd", "absent"), ("sec_edgar", "new_seats")]
    assert reasons[-1][2] == {"total": 1, "entity_id": "sec:0001652044"}   # the row records the pass
    assert out["source_totals"]["sec_edgar"] == 39                          # 38 + the pass's 1
    assert out["names_run"] == ["sec_edgar", "wd"], "one source, listed once"


def test_the_on_demand_dispatcher_asks_nothing_for_seats_the_run_saw(world, monkeypatch):
    world["seats"] = {"wd:Q92764"}
    monkeypatch.setattr("app.scraper.run_log.record_run", lambda *a, **k: contextlib.nullcontext({}))
    ondemand._run_instant_sources("Alphabet", ondemand.ScrapeDecision(True, "absent", 1))
    assert world["calls"] == ["sec_edgar", "wd"]
