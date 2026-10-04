"""The full profile's `as_of` parameter at the query-text level (mocked session):
the date filter reaches exactly the three dated sections and their counts, and
only when a date was asked for — the present must stay the present. The rule's
behaviour on real edges is pinned in tests/integration/test_profile_as_of_it.py."""
from unittest.mock import patch

import pytest

from app.routers.search import _active_clause

ENTITY = {"e": {"id": "e1", "name": "E", "type": "company"}}


class TestTheClause:
    def test_the_present_is_open_edges_only(self):
        assert _active_clause("r", None) == "r.until IS NULL"

    def test_a_date_tests_both_ends_and_spares_a_lower_bound(self):
        c = _active_clause("owns_r", "2019-12-31")
        # every basis but newly_listed is a lower bound (owns_merge.STATED_BASES)
        assert ("owns_r.since IS NULL OR (owns_r.since_basis IS NOT NULL AND "
                "owns_r.since_basis <> 'newly_listed') OR owns_r.since <= $as_of") in c
        assert "owns_r.until IS NULL OR owns_r.until > $as_of" in c


@pytest.mark.parametrize("bad", ["2019", "2019-12-31T00:00", "31-12-2019", "2019-12-311", "x"])
def test_a_malformed_date_is_refused_before_any_query(client, fake_db, bad):
    r = client.get("/search/entity/e1/full-profile", params={"as_of": bad})
    assert r.status_code == 422
    assert fake_db.calls == []


def _dated_calls(fake_db):
    return [(q, p) for q, p in fake_db.calls
            if any(k in q for k in ("[owns_r:OWNS]", "[sub_r:OWNS]", "[role_r:HAS_ROLE]",
                                    "count(DISTINCT owner)", "count(DISTINCT sub)", "count(DISTINCT p)"))]


@pytest.fixture
def no_sql():
    # the profile's claim lookups go through run_sql, outside the fake session —
    # a mocked suite must never reach a real server (the brute-force lockout)
    with patch("app.routers.search.run_sql", return_value=[]), \
         patch("app.claims.run_sql", return_value=[], create=True), \
         patch("app.db.arcadedb.run_sql", return_value=[]):
        yield


def test_with_a_date_every_dated_section_and_count_carries_it(client, fake_db, no_sql):
    fake_db.queue([ENTITY])
    r = client.get("/search/entity/e1/full-profile", params={"as_of": "2019-12-31"})
    assert r.status_code == 200, r.text
    dated = _dated_calls(fake_db)
    assert len(dated) == 6, [q[:60] for q, _ in dated]
    for q, p in dated:
        assert "$as_of" in q and "since_basis <> 'newly_listed'" in q and p["as_of"] == "2019-12-31", q
    # the undated sections never see it
    for q, p in fake_db.calls:
        if "SUCCEEDED_BY" in q or "DUAL_LISTED_WITH" in q:
            assert "$as_of" not in q and "as_of" not in p


def test_without_a_date_nothing_mentions_it_and_open_edges_rule(client, fake_db, no_sql):
    fake_db.queue([ENTITY])
    r = client.get("/search/entity/e1/full-profile")
    assert r.status_code == 200, r.text
    assert all("$as_of" not in q and "as_of" not in p for q, p in fake_db.calls)
    dated = _dated_calls(fake_db)
    assert len(dated) == 6
    assert all("until IS NULL" in q for q, _ in dated)
