"""
Usage counters against a real ArcadeDB.

The whole store is `UPDATE … UPSERT` with `COALESCE(col, 0) + 1` — arithmetic inside
an upsert, which is exactly the kind of statement a mocked session accepts happily
while the real engine does something else (or nothing). If these counters silently
failed to increment, every number in the product report would be 1.
"""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def analytics(it_db):
    from app import analytics as mod
    return mod


def _rows(vtype):
    from app.db.arcadedb import run_sql
    return run_sql(f"SELECT FROM {vtype}")


def _row(vtype, key):
    from app.db.arcadedb import run_sql
    rows = run_sql(f"SELECT FROM {vtype} WHERE key = :k", {"k": key})
    return rows[0] if rows else None


class TestSearchDemand:
    def test_the_same_search_twice_is_one_row_counted_twice(self, analytics):
        analytics.record_search("Siemens", "DE", "selected")
        analytics.record_search("Siemens", "DE", "selected")
        assert len(_rows("SearchDemand")) == 1
        assert _row("SearchDemand", "siemens|DE")["searches"] == 2

    def test_the_outcomes_are_counted_apart(self, analytics):
        analytics.record_search("Siemens", "DE", "selected")
        analytics.record_search("Siemens", "DE", "zero")
        analytics.record_search("Siemens", "DE", "abandoned")
        row = _row("SearchDemand", "siemens|DE")
        # Three searches; one found nothing; one was acted on. An abandonment is
        # neither of those, and must not be counted as either.
        assert (row["searches"], row["zero_results"], row["selected"]) == (3, 1, 1)

    def test_the_first_sighting_is_kept_and_the_last_moves(self, analytics):
        analytics.record_search("Siemens", "DE", "zero")
        first = _row("SearchDemand", "siemens|DE")["first_seen"]
        analytics.record_search("Siemens", "DE", "zero")
        row = _row("SearchDemand", "siemens|DE")
        assert row["first_seen"] == first and row["last_seen"] >= first

    def test_a_different_country_is_a_different_row(self, analytics):
        analytics.record_search("Alphabet", "DE", "selected")
        analytics.record_search("Alphabet", "FR", "zero")
        assert len(_rows("SearchDemand")) == 2

    def test_what_nobody_found_is_readable_on_its_own(self, analytics):
        """The point of the whole feature: a ranked list of unmet demand."""
        from app.db.arcadedb import run_sql

        for _ in range(3):
            analytics.record_search("Handelsregister GmbH", "DE", "zero")
        analytics.record_search("Siemens", "DE", "selected")

        gaps = run_sql("SELECT query, zero_results FROM SearchDemand "
                       "WHERE zero_results > 0 ORDER BY zero_results DESC")
        assert [g["query"] for g in gaps] == ["Handelsregister GmbH"]
        assert gaps[0]["zero_results"] == 3


class TestUsageCounters:
    def test_an_interaction_accumulates(self, analytics):
        analytics.record_usage("export.csv")
        analytics.record_usage("export.csv")
        analytics.record_usage("export.png")
        assert _row("UsageCounter", "export.csv")["count"] == 2
        assert _row("UsageCounter", "export.png")["count"] == 1

    def test_clicked_positions_are_their_own_counters(self, analytics):
        analytics.record_rank(0)
        analytics.record_rank(3)
        analytics.record_rank(3)
        assert _row("UsageCounter", "result.rank.0")["count"] == 1
        assert _row("UsageCounter", "result.rank.3")["count"] == 2


class TestEndpointStats:
    def test_a_window_of_counts_is_added_to_what_is_there(self, analytics):
        analytics.flush_endpoint_stats({"GET /v1/search/ 2xx <100ms": 5})
        analytics.flush_endpoint_stats({"GET /v1/search/ 2xx <100ms": 3,
                                        "GET /v1/search/ 5xx <500ms": 1})
        assert _row("EndpointStat", "GET /v1/search/ 2xx <100ms")["count"] == 8
        assert _row("EndpointStat", "GET /v1/search/ 5xx <500ms")["count"] == 1

    def test_a_broken_write_does_not_raise_at_the_caller(self, analytics, monkeypatch):
        # This runs on a background task beside live requests; it may fail, but it
        # may never propagate.
        monkeypatch.setattr("app.db.arcadedb.run_sql",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        analytics.flush_endpoint_stats({"GET /x 2xx <100ms": 1})


class TestPruning:
    def _age(self, vtype, key, days):
        """Push a row's last_seen into the past. The key is asked for rather than
        written out: `normalize_entity_name` strips legal suffixes, so "Ancient
        Co" keys as `ancient|` and a hardcoded key ages nothing at all."""
        from app.db.arcadedb import run_sql
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        run_sql(f"UPDATE {vtype} SET last_seen = :t WHERE key = :k", {"t": old, "k": key})

    def test_idle_rows_go_and_current_ones_stay(self, analytics):
        analytics.record_search("Ancient Co", None, "zero")
        analytics.record_search("Current Co", None, "zero")
        self._age("SearchDemand", analytics.search_key("Ancient Co", None), days=400)

        result = analytics.prune(days=365)
        assert result["SearchDemand"] == 1
        assert [r["query"] for r in _rows("SearchDemand")] == ["Current Co"]

    def test_a_dry_run_deletes_nothing(self, analytics):
        analytics.record_search("Ancient Co", None, "zero")
        self._age("SearchDemand", analytics.search_key("Ancient Co", None), days=400)

        assert analytics.prune(days=365, dry_run=True)["SearchDemand"] == 1
        assert len(_rows("SearchDemand")) == 1        # still there

    def test_a_query_still_being_asked_survives_however_old_it_is(self, analytics):
        # Pruning goes by last_seen, not first_seen: a company people still look
        # for is current, and dropping it would throw away the trend.
        from app.db.arcadedb import run_sql

        analytics.record_search("Old Favourite", None, "selected")
        run_sql("UPDATE SearchDemand SET first_seen = :t WHERE key = :k",
                {"t": (datetime.now(timezone.utc) - timedelta(days=900)).isoformat(),
                 "k": analytics.search_key("Old Favourite", None)})
        assert analytics.prune(days=365)["SearchDemand"] == 0
        assert len(_rows("SearchDemand")) == 1

    def test_every_counter_type_is_pruned(self, analytics):
        analytics.record_usage("export.csv")
        analytics.flush_endpoint_stats({"GET /x 2xx <100ms": 1})
        self._age("UsageCounter", "export.csv", days=400)
        self._age("EndpointStat", "GET /x 2xx <100ms", days=400)

        result = analytics.prune(days=365)
        assert result["UsageCounter"] == 1 and result["EndpointStat"] == 1
        assert _rows("UsageCounter") == [] and _rows("EndpointStat") == []
