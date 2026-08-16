"""
The measurement endpoint.

Public and unauthenticated on the write side, because the searches worth knowing
about include the signed-out ones — which also makes it the most floodable thing
in the API. Admin-only on the read side, because free text people typed is not
something to hand out.

The database is stubbed here; `tests/integration/test_analytics_it.py` covers the
counters themselves.
"""
import pytest

from app import analytics
from app.config import settings
from app.routers import analytics as router


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "ANALYTICS_ENABLED", True)
    with router._lock:
        router._events.clear()
    yield
    with router._lock:
        router._events.clear()


@pytest.fixture
def recorded(monkeypatch):
    """Everything that would have been written."""
    calls: list = []
    monkeypatch.setattr(analytics, "record_search",
                        lambda q, c, o: calls.append(("search", q, c, o)))
    monkeypatch.setattr(analytics, "record_usage", lambda e: calls.append(("usage", e)))
    monkeypatch.setattr(analytics, "record_rank", lambda r: calls.append(("rank", r)))
    return calls


def _post(client, **body):
    return client.post("/v1/analytics/event", json=body)


class TestRecording:
    def test_a_settled_search_is_recorded(self, client, recorded):
        r = _post(client, kind="search", query="Siemens", country="DE", outcome="selected")
        assert r.status_code == 204
        assert ("search", "Siemens", "DE", "selected") in recorded

    def test_a_chosen_result_also_counts_its_position(self, client, recorded):
        _post(client, kind="search", query="Siemens", outcome="selected", rank=3)
        assert ("rank", 3) in recorded

    def test_a_position_is_only_counted_when_something_was_chosen(self, client, recorded):
        # An abandoned search has no clicked rank; counting one would invent a
        # click that never happened.
        _post(client, kind="search", query="Siemens", outcome="abandoned", rank=3)
        assert not any(c[0] == "rank" for c in recorded)

    def test_a_usage_event_is_recorded(self, client, recorded):
        assert _post(client, kind="usage", event="export.csv").status_code == 204
        assert ("usage", "export.csv") in recorded


class TestWhatIsRefused:
    def test_an_unknown_event_name_changes_nothing(self, client, recorded):
        # Rejected quietly: a 4xx here would tell a prober which keys exist, and
        # measurement must never surface anything to the user either way.
        assert _post(client, kind="usage", event="made.up").status_code == 204

    def test_an_over_long_query_is_rejected_by_the_schema(self, client, recorded):
        r = _post(client, kind="search", query="x" * 500, outcome="zero")
        assert r.status_code == 422 and recorded == []

    def test_a_country_that_is_not_iso_2_is_rejected(self, client, recorded):
        assert _post(client, kind="search", query="a", country="Germany",
                     outcome="zero").status_code == 422

    def test_an_impossible_rank_is_rejected(self, client, recorded):
        assert _post(client, kind="search", query="a", outcome="selected",
                     rank=999).status_code == 422

    def test_a_search_without_an_outcome_records_nothing(self, client, recorded):
        assert _post(client, kind="search", query="Siemens").status_code == 204
        assert recorded == []


class TestTheFlag:
    def test_nothing_is_recorded_while_measurement_is_off(self, client, recorded, monkeypatch):
        monkeypatch.setattr(settings, "ANALYTICS_ENABLED", False)
        assert _post(client, kind="usage", event="export.csv").status_code == 204
        assert recorded == []


class TestFlooding:
    def test_an_anonymous_flood_is_capped(self, client, recorded):
        for _ in range(router.EVENT_RATE_LIMIT):
            assert _post(client, kind="usage", event="export.csv").status_code == 204
        assert _post(client, kind="usage", event="export.csv").status_code == 429

    def test_the_cap_is_generous_enough_for_a_real_session(self):
        # Settled searches and deliberate clicks only — a few dozen an hour is a
        # heavy user. A cap set near that would silently drop their data.
        assert router.EVENT_RATE_LIMIT >= 100


class TestReadingItBack:
    def test_the_lists_are_admin_only(self, client, make_token):
        for path in ("/v1/analytics/searches", "/v1/analytics/usage", "/v1/analytics/endpoints"):
            assert client.get(path).status_code in (401, 403), path
            viewer = {"Authorization": f"Bearer {make_token(role='viewer')}"}
            assert client.get(path, headers=viewer).status_code == 403, path

    def test_an_admin_gets_the_page_and_the_total(self, client, make_token, monkeypatch):
        rows = [{"key": "siemens|DE", "query": "Siemens", "searches": 9}]
        monkeypatch.setattr(router, "_page",
                            lambda vtype, order_by, response, skip, limit: rows)
        admin = {"Authorization": f"Bearer {make_token(role='admin')}"}
        r = client.get("/v1/analytics/searches", headers=admin)
        assert r.status_code == 200 and r.json() == rows
