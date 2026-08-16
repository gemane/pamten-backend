"""
The rules behind the usage counters, without a database.

Two of these carry the design rather than merely checking it: what makes two
searches one row, and what the key space is allowed to contain. Both are decided
here and nowhere else.
"""
import pytest

from app import analytics


class TestWhatMakesTwoSearchesOneRow:
    def test_the_same_name_written_differently_is_one_question(self):
        # Otherwise the demand list is split across spellings of the same company
        # and nothing rises to the top.
        assert analytics.search_key("Alphabet Inc.", None) == analytics.search_key("alphabet", None)
        assert analytics.search_key("  SIEMENS  ", None) == analytics.search_key("Siemens", None)

    def test_two_countries_are_two_questions(self):
        # "Alphabet in France" and "Alphabet in Germany" have different answers;
        # merging them would hide exactly the gap the zero-result count is for.
        assert analytics.search_key("Alphabet", "FR") != analytics.search_key("Alphabet", "DE")

    def test_a_country_is_not_the_same_as_no_country(self):
        assert analytics.search_key("Alphabet", "DE") != analytics.search_key("Alphabet", None)

    def test_the_country_case_does_not_split_a_row(self):
        assert analytics.search_key("Alphabet", "de") == analytics.search_key("Alphabet", "DE")


class TestTheKeySpaceIsClosed:
    """A public endpoint feeds this. An open key space would be unbounded row
    growth and a junk-injection vector in one."""

    def test_an_unknown_usage_event_is_refused(self):
        with pytest.raises(ValueError, match="unknown usage event"):
            analytics.record_usage("something.invented")

    def test_a_known_event_is_accepted(self, monkeypatch):
        written: list = []
        monkeypatch.setattr("app.db.arcadedb.run_sql",
                            lambda *a, **k: written.append(a) or [])
        analytics.record_usage("export.csv")
        assert written

    def test_an_unknown_search_outcome_is_refused(self):
        with pytest.raises(ValueError, match="unknown search outcome"):
            analytics.record_search("acme", None, "hovered")

    def test_a_rank_beyond_the_bucket_range_is_refused(self):
        with pytest.raises(ValueError, match="out of range"):
            analytics.record_rank(analytics.MAX_RANK + 1)
        with pytest.raises(ValueError, match="out of range"):
            analytics.record_rank(-1)


class TestLatencyBuckets:
    """Buckets rather than a mean: a mean hides the tail, and the tail is what is
    worth fixing."""

    def test_a_fast_request(self):
        assert analytics.latency_bucket(12) == "<100ms"

    def test_the_boundary_belongs_to_the_slower_bucket(self):
        # 100ms is not "<100ms". Off by one here silently flatters every report.
        assert analytics.latency_bucket(99.9) == "<100ms"
        assert analytics.latency_bucket(100) == "<500ms"

    def test_the_tail_has_somewhere_to_go(self):
        assert analytics.latency_bucket(30_000) == ">=5000ms"


class TestWhatIsNotStored:
    """The privacy design, asserted rather than described.

    If any of these ever fails, the record of processing has become untrue — and
    that is a legal document, not a comment.
    """

    def test_the_search_write_carries_no_identifier(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr("app.db.arcadedb.run_sql",
                            lambda sql, params=None: captured.update(sql=sql, params=params or {}) or [])
        analytics.record_search("Siemens", "DE", "selected")

        blob = f"{captured['sql']} {captured['params']}".lower()
        for forbidden in ("user", "session", "ip", "fingerprint", "token"):
            assert forbidden not in blob, f"{forbidden!r} reached the analytics write"

    def test_the_query_is_capped(self, monkeypatch):
        # An unbounded free-text column on a public endpoint is a storage bug and
        # a privacy one: whatever someone pastes into the box lands here.
        captured: dict = {}
        monkeypatch.setattr("app.db.arcadedb.run_sql",
                            lambda sql, params=None: captured.update(params=params or {}) or [])
        analytics.record_search("x" * 500, None, "zero")
        assert len(captured["params"]["q"]) == 120
