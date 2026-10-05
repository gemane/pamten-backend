"""GLEIF relationship history from the archive — the pure parts: which
snapshots, the periods they make, the file, and what applying would write."""
import io
import json
from datetime import date

import pytest

from app.scraper import gleif_rr_history as h


def _rec(child, parent, rtype="IS_DIRECTLY_CONSOLIDATED_BY", status="ACTIVE", start=None):
    rel = {"StartNode": {"NodeID": {"$": child}, "NodeIDType": {"$": "LEI"}},
           "EndNode": {"NodeID": {"$": parent}, "NodeIDType": {"$": "LEI"}},
           "RelationshipType": {"$": rtype}, "RelationshipStatus": {"$": status}}
    if start:
        rel["RelationshipPeriods"] = {"RelationshipPeriod": {
            "StartDate": {"$": f"{start}T00:00:00.000Z"}, "PeriodType": {"$": "RELATIONSHIP_PERIOD"}}}
    return {"RelationshipRecord": {"Relationship": rel,
                                   "Registration": {"LastUpdateDate": {"$": "2020-01-01T00:00:00Z"}}}}


def _file(*recs) -> io.BytesIO:
    return io.BytesIO(json.dumps({"relations": list(recs)}).encode())


class TestMonths:
    def test_the_archive_start_then_the_first_of_each_month(self):
        assert h.month_days(date(2018, 2, 9), date(2018, 5, 3)) == [
            date(2018, 2, 9), date(2018, 3, 1), date(2018, 4, 1), date(2018, 5, 1)]

    def test_across_the_year_end(self):
        assert h.month_days(date(2019, 11, 1), date(2020, 1, 31))[-2:] == [date(2019, 12, 1), date(2020, 1, 1)]


class TestFindPublish:
    def test_takes_the_first_slot_that_exists(self):
        seen = []

        class Resp:
            def __init__(self, code, body):
                self.status_code, self._body = code, body

            def json(self):
                return self._body

        class Client:
            def get(self, url):
                seen.append(url.rsplit("/", 1)[1])
                if seen[-1] == "20190102-0800":
                    return Resp(200, {"data": {"publish_date": "2019-01-02 08:00:00",
                        "full_file": {"json": {"url": "https://example.com/rr.zip", "record_count": 5}}}})
                return Resp(404, {})
        pub = h.find_publish(Client(), date(2019, 1, 1))
        assert pub == {"publish_date": "2019-01-02", "url": "https://example.com/rr.zip", "record_count": 5}
        assert seen == ["20190101-0000", "20190101-0800", "20190101-1600", "20190102-0000", "20190102-0800"]


class TestSnapshotPairs:
    def test_one_pair_for_direct_and_ultimate_the_earlier_stated_start(self):
        pairs, records = h.snapshot_pairs(_file(
            _rec("C", "P", start="2015-03-01"),
            _rec("C", "P", "IS_ULTIMATELY_CONSOLIDATED_BY", start="2012-01-01"),
            _rec("C", "U", "IS_ULTIMATELY_CONSOLIDATED_BY"),
            _rec("C", "Q", status="INACTIVE"),            # ended: not present
            _rec("F", "M", "IS_FUND-MANAGED_BY"),         # not consolidation
        ))
        assert records == 5
        assert pairs == {("P", "C"): ("direct", "2012-01-01"), ("U", "C"): ("indirect", None)}


class TestHistory:
    def test_present_gone_back_again_is_two_periods(self):
        hi = h.History()
        hi.observe("2018-02-09", {("P", "C"): ("direct", None)})
        hi.observe("2018-03-01", {("P", "C"): ("direct", None)})
        hi.observe("2018-04-01", {})
        hi.observe("2018-05-01", {("P", "C"): ("indirect", "2018-04-20")})
        periods = sorted(hi.periods(), key=lambda p: p["first_seen"])
        assert periods == [
            {"parent": "P", "child": "C", "first_seen": "2018-02-09", "last_seen": "2018-03-01",
             "until": "2018-04-01", "marker": "direct", "since": None},
            {"parent": "P", "child": "C", "first_seen": "2018-05-01", "last_seen": "2018-05-01",
             "until": None, "marker": "indirect", "since": "2018-04-20"},
        ]

    def test_a_period_is_direct_if_any_snapshot_said_so_and_keeps_the_earliest_start(self):
        hi = h.History()
        hi.observe("2019-01-01", {("P", "C"): ("indirect", "2016-01-01")})
        hi.observe("2019-02-01", {("P", "C"): ("direct", "2014-05-05")})
        hi.observe("2019-03-01", {("P", "C"): ("indirect", None)})
        (p,) = hi.periods()
        assert (p["marker"], p["since"], p["last_seen"]) == ("direct", "2014-05-05", "2019-03-01")

    def test_snapshots_must_come_oldest_first(self):
        hi = h.History()
        hi.observe("2019-02-01", {})
        with pytest.raises(ValueError):
            hi.observe("2019-01-01", {})


def test_the_intervals_file_round_trips(tmp_path):
    hi = h.History()
    hi.observe("2018-02-09", {("P", "C"): ("direct", None), ("Q", "C"): ("direct", None)})
    hi.observe("2018-03-01", {("P", "C"): ("direct", None)})
    path = str(tmp_path / "hist.jsonl.gz")
    assert h.write_intervals(path, hi, ["2018-04 (no publish)"]) == 2
    header, periods = h.read_intervals(path)
    assert header["snapshots"] == ["2018-02-09", "2018-03-01"]
    assert header["skipped"] == ["2018-04 (no publish)"]
    assert {(p["parent"], p["until"]) for p in periods} == {("Q", "2018-03-01"), ("P", None)}
    assert not (tmp_path / "hist.jsonl.gz.tmp").exists()


def _p(parent="P", child="C", first="2018-02-09", last="2020-05-01", until="2020-06-01",
       marker="direct", since=None):
    return {"parent": parent, "child": child, "first_seen": first, "last_seen": last,
            "until": until, "marker": marker, "since": since}


IDS = {"P": "lei:P", "C": "gb-coh:1", "Q": "lei:Q"}


class TestPlan:
    def test_an_ended_period_becomes_a_closed_edge_with_a_lower_bound_start(self):
        plan = h.plan_history([_p()], IDS, {})
        ((owner, owned, props, claim),) = plan["create"]
        assert (owner, owned, claim) == ("lei:P", "gb-coh:1", True)   # merged child found by lei
        assert props["since"] == "2018-02-09" and props["since_basis"] == "gleif_first_seen"
        assert props["until"] == "2020-06-01" and props["until_reason"] == "gleif_dropped"
        assert props["source_date"] == "2020-05-01" and props["filing_type"] == "RR"
        assert props["direct_or_indirect"] == "direct"

    def test_a_stated_start_is_kept_and_carries_no_basis(self):
        ((_, _, props, _),) = h.plan_history([_p(since="2009-07-01")], IDS, {})["create"]
        assert props["since"] == "2009-07-01" and "since_basis" not in props

    def test_a_past_period_of_a_pair_still_current_writes_no_claim(self):
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#1:1", "since": "2021-01-01", "until": None}]}
        periods = [_p(), _p(first="2021-02-01", last="2026-10-05", until=None, since="2021-01-01")]
        plan = h.plan_history(periods, IDS, edges)
        ((_, _, _, claim),) = plan["create"]
        assert claim is False
        assert plan["fill"] == [] and plan["skipped"]["stated_start"] == 1

    def test_an_open_period_fills_only_an_undated_current_edge(self):
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#1:1", "since": None, "until": None}]}
        plan = h.plan_history([_p(until=None, last="2026-10-05")], IDS, edges)
        assert plan["fill"] == [("#1:1", "2018-02-09")] and plan["create"] == []

    def test_an_open_period_without_its_edge_creates_nothing(self):
        plan = h.plan_history([_p(until=None)], IDS, {})
        assert plan["create"] == [] and plan["fill"] == []
        assert plan["skipped"]["open_without_edge"] == 1

    def test_the_last_period_ended_but_current_in_the_graph_is_left_to_the_delta(self):
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#1:1", "since": None, "until": None}]}
        plan = h.plan_history([_p()], IDS, edges)
        assert plan["create"] == [] and plan["skipped"]["current_in_graph"] == 1

    def test_a_second_run_writes_nothing(self):
        written = h.plan_history([_p()], IDS, {})["create"][0][2]
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#9:9", **written}]}
        plan = h.plan_history([_p()], IDS, edges)
        assert plan["create"] == [] and plan["skipped"]["already_written"] == 1

    def test_the_importers_inactive_edge_for_the_same_end_is_not_doubled(self):
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#2:2", "since": "2015-01-01", "until": "2020-05-17",
                                          "until_reason": "gleif_inactive"}]}
        plan = h.plan_history([_p()], IDS, edges)
        assert plan["create"] == [] and plan["skipped"]["already_written"] == 1

    def test_an_earlier_ended_edge_does_not_hide_a_later_period(self):
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#2:2", "since": "2010-01-01", "until": "2015-01-01"}]}
        assert len(h.plan_history([_p()], IDS, edges)["create"]) == 1

    def test_a_gap_inside_one_stated_relationship_is_not_a_second_one(self):
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#1:1", "since": "2012-01-01", "until": None}]}
        periods = [_p(since="2012-01-01"), _p(first="2020-07-01", last="2026-10-05", until=None,
                                              since="2012-01-01")]
        plan = h.plan_history(periods, IDS, edges)
        # joined into one period first (join_holes): no ended edge, the current
        # edge's stated start kept
        assert plan["create"] == [] and plan["fill"] == []
        assert plan["skipped"]["stated_start"] == 1

    def test_a_company_not_in_the_graph_is_skipped(self):
        plan = h.plan_history([_p(parent="X")], IDS, {})
        assert plan["create"] == [] and plan["skipped"]["unresolved"] == 1


def test_the_new_basis_is_a_lower_bound_for_time_travel():
    from app.scraper.owns_merge import is_lower_bound
    assert is_lower_bound(h.SINCE_BASIS)


def test_the_build_skips_a_snapshot_that_shrank_and_ends_nothing_by_it(tmp_path, monkeypatch):
    # a truncated publish would otherwise end every relationship it lacks
    import zipfile
    files = {
        "2018-02-09": [_rec("C1", "P"), _rec("C2", "P"), _rec("C3", "P"), _rec("C4", "P"), _rec("C5", "P")],
        "2018-03-01": [_rec("C1", "P")],                                       # 1 of 5 records
        "2026-10-05": [_rec("C1", "P"), _rec("C2", "P"), _rec("C3", "P"), _rec("C4", "P")],
    }
    pubs = {d: {"publish_date": d, "url": d, "record_count": len(r)} for d, r in files.items()}
    monkeypatch.setattr(h, "latest_publish", lambda c: pubs["2026-10-05"])
    monkeypatch.setattr(h, "find_publish", lambda c, day: pubs.get(
        "2018-02-09" if day == date(2018, 2, 9) else "2018-03-01" if day == date(2018, 3, 1) else "2026-10-05"))

    def download(client, url, dest):
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("rr.json", json.dumps({"relations": files[url]}))
    monkeypatch.setattr(h, "_download", download)
    monkeypatch.setattr(h, "_tmp_dir", lambda: str(tmp_path))
    out = str(tmp_path / "hist.jsonl.gz")
    res = h.build_history(out, end=date(2018, 4, 1), client=object(), echo=lambda *a: None)
    assert res["snapshots"] == 2 and "2018-03-01" in res["skipped"][0]
    _, periods = h.read_intervals(out)
    ended = {p["child"]: p["until"] for p in periods if p["until"]}
    assert ended == {"C5": "2026-10-05"}           # not C2–C5 on 2018-03-01


class TestHoles:
    def test_gone_and_back_with_the_same_stated_start_is_one_relationship(self):
        joined = h.join_holes([
            _p(first="2021-02-01", last="2026-10-05", until=None, marker="indirect", since="2012-01-01"),
            _p(first="2018-02-09", last="2019-08-01", until="2019-09-01", since="2012-01-01"),
        ])
        assert joined == [_p(first="2018-02-09", last="2026-10-05", until=None, since="2012-01-01")]

    def test_a_different_start_is_a_new_period(self):
        assert len(h.join_holes([_p(since="2012-01-01"),
                                 _p(first="2021-02-01", until=None, since="2021-01-15")])) == 2

    def test_without_stated_starts_nothing_says_they_are_the_same(self):
        assert len(h.join_holes([_p(), _p(first="2021-02-01", until=None)])) == 2

    def test_a_hole_writes_no_ended_edge(self):
        edges = {("lei:P", "gb-coh:1"): [{"rid": "#1:1", "since": "2012-01-01", "until": None}]}
        periods = [_p(since="2012-01-01", until="2019-09-01"),
                   _p(first="2019-10-01", last="2026-10-05", until=None, since="2012-01-01")]
        plan = h.plan_history(periods, IDS, edges)
        assert plan["create"] == [] and plan["skipped"]["stated_start"] == 1
