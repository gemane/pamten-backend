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


def _build(tmp_path, monkeypatch, counts: dict[str, int]):
    """A faked build over snapshots holding the first N of C1..C9 (oldest
    first; the last date is the latest publish)."""
    import zipfile
    days = list(counts)
    pubs = {d: {"publish_date": d, "url": d, "record_count": n} for d, n in counts.items()}
    monthly = {date.fromisoformat(d[:8] + "01") if i else date.fromisoformat(d): d
               for i, d in enumerate(days[:-1])}
    monkeypatch.setattr(h, "latest_publish", lambda c: pubs[days[-1]])
    monkeypatch.setattr(h, "find_publish", lambda c, day: pubs.get(monthly.get(day, days[-1])))

    def download(client, url, dest):
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("rr.json", json.dumps(
                {"relations": [_rec(f"C{i}", "P") for i in range(1, counts[url] + 1)]}))
    monkeypatch.setattr(h, "_download", download)
    monkeypatch.setattr(h, "_tmp_dir", lambda: str(tmp_path))
    out = str(tmp_path / "hist.jsonl.gz")
    res = h.build_history(out, start=date.fromisoformat(days[0]),
                          end=date.fromisoformat(days[-2]), client=object(), echo=lambda *a: None)
    _, periods = h.read_intervals(out)
    return res, {p["child"]: p["until"] for p in periods if p["until"]}


def test_a_dip_is_skipped_and_ends_nothing(tmp_path, monkeypatch):
    # 2023-08-01: 281,309 records between 398,842 and ~400k — a broken publish
    res, ended = _build(tmp_path, monkeypatch, {"2018-02-09": 8, "2018-03-01": 2,
                                                "2018-04-01": 8, "2026-10-05": 8})
    assert res["snapshots"] == 3 and "2018-03-01" in res["skipped"][0]
    assert ended == {}


def test_a_real_drop_is_believed_and_blocks_nothing_after_it(tmp_path, monkeypatch):
    # 2019-09: −10.3 % and it stayed — a clean-up; a fixed floor would have
    # skipped every snapshot after it, to today
    res, ended = _build(tmp_path, monkeypatch, {"2018-02-09": 9, "2018-03-01": 6,
                                                "2018-04-01": 6, "2018-05-01": 5, "2026-10-05": 5})
    assert res["skipped"] == [] and res["snapshots"] == 5
    assert ended == {"C7": "2018-03-01", "C8": "2018-03-01", "C9": "2018-03-01", "C6": "2018-05-01"}


def test_the_latest_is_never_held_back(tmp_path, monkeypatch):
    res, ended = _build(tmp_path, monkeypatch, {"2018-02-09": 8, "2026-10-05": 4})
    assert res["skipped"] == [] and res["last"] == "2026-10-05"
    assert set(ended) == {"C5", "C6", "C7", "C8"}


def test_a_small_shrink_is_no_dip(tmp_path, monkeypatch):
    # exactly 5 % down, then back: within the tolerance, so believed — no dip
    res, ended = _build(tmp_path, monkeypatch, {"2018-02-09": 20, "2018-03-01": 19,
                                                "2018-04-01": 20, "2026-10-05": 20})
    assert res["skipped"] == [] and ended == {"C20": "2018-03-01"}


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


class TestRefuted:
    """Activision Blizzard: Microsoft "ultimate parent since 2001-07-03",
    registered 2026-07 — while King.com named Activision its own ultimate
    parent until 2026-07. A top of a tree has no parent."""

    KING = {"parent": "ATVI", "child": "KING", "first_seen": "2021-05-01", "last_seen": "2026-07-01",
            "until": "2026-08-01", "marker": "indirect", "since": "2021-05-11"}
    MSFT = {"parent": "MSFT", "child": "ATVI", "first_seen": "2026-08-01", "last_seen": "2026-10-05",
            "until": None, "marker": "direct", "since": "2001-07-03"}
    IDS = {"ATVI": "lei:ATVI", "KING": "lei:KING", "MSFT": "lei:MSFT"}

    def test_the_last_snapshot_as_top_comes_from_ultimate_only_periods(self):
        direct = {**self.KING, "parent": "X", "marker": "direct", "last_seen": "2026-09-01"}
        assert h.tops([self.KING, direct]) == {"ATVI": "2026-07-01"}

    def test_a_start_before_the_child_stopped_being_top_is_refuted(self):
        assert h.refuted_by(self.MSFT, h.tops([self.KING])) == "2026-07-01"

    def test_a_start_after_it_or_a_child_still_top_is_not(self):
        top = h.tops([self.KING])
        assert h.refuted_by({**self.MSFT, "since": "2026-07-15"}, top) is None
        # the child still top while the relationship was already listed: stale
        # data somewhere, but nothing says which side is wrong
        assert h.refuted_by({**self.MSFT, "first_seen": "2026-06-01"}, top) is None
        assert h.refuted_by({**self.MSFT, "since": None}, top) is None

    def test_the_edge_with_the_refuted_date_is_corrected(self):
        edges = {("lei:MSFT", "lei:ATVI"): [{"rid": "#1:1", "since": "2001-07-03", "until": None}]}
        plan = h.plan_history([self.KING, self.MSFT], self.IDS, edges)
        assert plan["correct"] == [("#1:1", "2001-07-03", "2026-08-01", "2026-07-01")]

    def test_another_sources_date_on_the_edge_is_left_alone(self):
        edges = {("lei:MSFT", "lei:ATVI"): [{"rid": "#1:1", "since": "2023-10-13", "until": None}]}
        plan = h.plan_history([self.KING, self.MSFT], self.IDS, edges)
        assert plan["correct"] == [] and plan["skipped"]["stated_start"] == 1

    def test_an_ended_period_is_written_with_the_corrected_start(self):
        ended = {**self.MSFT, "last_seen": "2026-09-01", "until": "2026-10-05"}
        ((_, _, props, _),) = [c for c in h.plan_history([self.KING, ended], self.IDS, {})["create"]
                               if c[0] == "lei:MSFT"]
        assert (props["since"], props["since_basis"], props["since_not_before"]) == \
            ("2026-08-01", "gleif_first_seen", "2026-07-01")
