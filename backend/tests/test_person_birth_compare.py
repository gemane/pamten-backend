"""Birth dates are compared at the precision both sources state, and only the
members who state one are held together by it (findings item 122)."""
import pytest

from app.routers.persons import birth_clusters, births_compare


@pytest.mark.parametrize("a, b, expected", [
    ("1957-08", "1957-08", "same"),                # two register months
    ("1957-08-11", "1957-08", "same"),             # Wikidata's day, the register's month
    ("1957-08", "1957-08-11", "same"),
    ("1957-08-11", "1957-08-11", "same"),
    ("1967-06-01", "1967-05", "different"),        # Vishal Sikka: the sources disagree
    ("1942-01-09", "1943-01", "different"),
    ("1957-08-11", "1957-08-20", "different"),
    ("1957-00-00", "1957-08", None),               # a Wikidata year agrees and settles nothing
    ("1957-00-00", "1957-00-00", None),
    ("1957", "1957", None),
    ("1957-00-00", "1958-03", "different"),        # … but a year that differs does
    ("1957-08-00", "1957-08-11", "same"),          # a month written with a zero day
    (None, "1957-08", None),
    ("", "", None),
])
def test_births_compare(a, b, expected):
    assert births_compare(a, b) == expected


def _m(pid, birth=None):
    return {"id": pid, "birth_date": birth}


def _ids(clusters):
    return sorted(sorted(m["id"] for m in c) for c in clusters)


def test_a_member_without_a_date_is_in_no_cluster():
    assert _ids(birth_clusters([_m("a", "1966-02"), _m("b", "1966-02"), _m("c")])) == [["a", "b"]]


def test_two_dates_give_two_clusters_and_a_lone_one_none():
    members = [_m("a", "1960-01"), _m("b", "1960-01"), _m("c", "1970-05"), _m("d", "1970-05-02"), _m("e", "1980-09")]
    assert _ids(birth_clusters(members)) == [["a", "b"], ["c", "d"]]


def test_a_month_between_two_different_days_holds_nobody_together():
    members = [_m("a", "1957-08-11"), _m("b", "1957-08"), _m("c", "1957-08-20")]
    assert birth_clusters(members) == []


def test_years_alone_cluster_nobody():
    assert birth_clusters([_m("a", "1957-00-00"), _m("b", "1957-00-00")]) == []
