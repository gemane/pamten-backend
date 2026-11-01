"""Wikidata runs once more when a later source gave the company an id
(findings item 27). Toast, Inc. was new in rebuild 10-31b: Wikidata searched
"TOAST INC" by name and refused the two unrelated "Toast" items, then the SEC
wrote the node with CIK 0001650164 — the id Wikidata's Toast item (Q25025312)
states. Both dispatchers, `run_scrape_all` and the on-demand
`_run_instant_sources`, repeat a source that reads the stored ids once, and
only when the ids changed. Registry and resolver are faked (no DB/network)."""
import contextlib

import pytest

import app.scraper.runner  # noqa: F401 - registers the built-ins before the swap
from app.config import settings
from app.scraper import ondemand
from app.scraper.scraper_registry import ScraperSpec, register

TOAST = {"id": "sec:0001650164", "sec_cik": "0001650164", "lei_id": None, "wikidata_id": None}


@pytest.fixture
def world(monkeypatch, fake_sources):
    """Fake wikidata (reads stored ids) + sec specs; `stored` is the node the
    query resolves to, which the fake SEC run writes."""
    fake_sources("wd", "sec")
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", False)
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
    state = {"stored": None, "after_sec": TOAST, "calls": [], "wd": ["no_results", "ok"]}
    monkeypatch.setattr("app.scraper.runner._stored_entity", lambda q, c=None: state["stored"])

    def wd(q, d, c=None):
        state["calls"].append("wd")
        status = state["wd"].pop(0) if len(state["wd"]) > 1 else state["wd"][0]
        return {"status": status, "total": 1 if status == "ok" else 0}

    def sec(q, d, c=None):
        state["calls"].append("sec")
        state["stored"] = state["after_sec"]
        return {"status": "ok", "total": 45, "entity_id": "sec:0001650164"}

    state["register"] = lambda flag=True: (
        register(ScraperSpec("wd", wd, lambda: True, depth_aware=True, reads_stored_ids=flag)),
        register(ScraperSpec("sec", sec, lambda: True)))
    return state


class TestRunScrapeAll:
    def _run(self):
        from app.scraper.runner import run_scrape_all
        return run_scrape_all("TOAST INC", depth=2)

    def test_a_cik_the_sec_wrote_gets_wikidata_asked_again(self, world):
        world["register"]()
        out = self._run()
        assert world["calls"] == ["wd", "sec", "wd"]
        assert out["results"]["wd"] == {"status": "ok", "total": 1, "rerun": "new_ids"}

    def test_not_a_company_is_asked_again_too(self, world):
        world["register"]()
        world["wd"] = ["not_a_company", "ok"]
        assert self._run()["results"]["wd"]["status"] == "ok"
        assert world["calls"] == ["wd", "sec", "wd"]

    def test_unchanged_ids_ask_once(self, world):
        # a re-scrape of a node that already held the CIK: the same answer again
        world["register"]()
        world["stored"] = TOAST
        out = self._run()
        assert world["calls"] == ["wd", "sec"]
        assert out["results"]["wd"]["status"] == "no_results" and "rerun" not in out["results"]["wd"]

    def test_a_node_without_any_id_asks_once(self, world):
        world["register"]()
        world["after_sec"] = {"id": "u1", "sec_cik": None, "lei_id": None, "wikidata_id": None}
        self._run()
        assert world["calls"] == ["wd", "sec"]

    def test_a_found_item_is_not_asked_again(self, world):
        world["register"]()
        world["wd"] = ["ok"]
        self._run()
        assert world["calls"] == ["wd", "sec"]

    def test_a_source_that_does_not_read_ids_is_not_repeated(self, world):
        world["register"](flag=False)
        self._run()
        assert world["calls"] == ["wd", "sec"]

    def test_only_the_source_that_reads_ids_is_repeated(self, world, fake_sources):
        # a name-only source that found nothing beside it stays at one ask
        fake_sources("oc")
        world["register"]()
        register(ScraperSpec("oc", lambda q, d, c=None: world["calls"].append("oc") or
                             {"status": "no_results", "total": 0}, lambda: True))
        self._run()
        assert world["calls"] == ["wd", "sec", "oc", "wd"]


def test_the_on_demand_dispatcher_asks_again_and_logs_why(world, monkeypatch):
    reasons = []

    @contextlib.contextmanager
    def fake_record_run(source, target, reason=None):
        reasons.append((source, reason))
        yield {}

    monkeypatch.setattr("app.scraper.run_log.record_run", fake_record_run)
    world["register"]()
    out = ondemand._run_instant_sources("TOAST INC", ondemand.ScrapeDecision(True, "absent", 1))
    assert world["calls"] == ["wd", "sec", "wd"]
    assert reasons == [("wd", "absent"), ("sec", "absent"), ("wd", "new_ids")]
    assert out["names_run"] == ["wd", "sec"], "one source, listed once"


def test_wikidata_is_declared_to_read_the_stored_ids():
    from app.scraper.scraper_registry import get
    assert get("wikidata").reads_stored_ids and not get("sec_edgar").reads_stored_ids
