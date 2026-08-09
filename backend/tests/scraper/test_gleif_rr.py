"""Unit tests for GLEIF RR-CDF relationship parsing (DB not involved).

End-to-end write (edge with direct_or_indirect) is covered against a real
ArcadeDB in tests/integration/test_gleif_rr_it.py."""

import json
import zipfile

from app.scraper.gleif_rr import (
    _collapse, _family_of, _fold, _node_lei, _rr_edge, _v, import_rr_cdf,
)


def _rec(rtype, child, parent, status="ACTIVE", child_type="LEI", parent_type="LEI",
         periods=None):
    rel = {
        "StartNode": {"NodeID": {"$": child}, "NodeIDType": {"$": child_type}},
        "EndNode":   {"NodeID": {"$": parent}, "NodeIDType": {"$": parent_type}},
        "RelationshipType":   {"$": rtype},
        "RelationshipStatus": {"$": status},
    }
    if periods is not None:
        rel["RelationshipPeriods"] = {"RelationshipPeriod": periods}
    return {"RelationshipRecord": {"Relationship": rel}}


def _period(ptype, start=None, end=None):
    p = {"PeriodType": {"$": ptype}}
    if start is not None:
        p["StartDate"] = {"$": start}
    if end is not None:
        p["EndDate"] = {"$": end}
    return p


class TestUnwrap:
    def test_v_unwraps_and_trims(self):
        assert _v({"$": " ACTIVE "}) == "ACTIVE"
        assert _v({"$": ""}) is None
        assert _v(None) is None

    def test_node_lei_only_for_lei_nodes(self):
        assert _node_lei({"NodeID": {"$": "ABC"}, "NodeIDType": {"$": "LEI"}}) == "ABC"
        assert _node_lei({"NodeID": {"$": "ABC"}, "NodeIDType": {"$": "OTHER"}}) is None


class TestRrEdge:
    def test_direct_consolidation(self):
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT")) == \
            ("PARENT", "CHILD", "direct", None, None)

    def test_ultimate_is_indirect(self):
        assert _rr_edge(_rec("IS_ULTIMATELY_CONSOLIDATED_BY", "CHILD", "PARENT")) == \
            ("PARENT", "CHILD", "indirect", None, None)

    def test_captures_relationship_period_start_as_since(self):
        # The RELATIONSHIP_PERIOD start date is the ownership start → `since` (date only).
        edge = _rr_edge(_rec(
            "IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=[_period("ACCOUNTING_PERIOD", "2023-05-17T00:00:00.000Z",
                             "2023-09-30T00:00:00.000Z"),
                     _period("RELATIONSHIP_PERIOD", "2023-05-17T00:00:00.000Z")]))
        assert edge == ("PARENT", "CHILD", "direct", "2023-05-17", None)

    def test_captures_relationship_period_end_as_until(self):
        edge = _rr_edge(_rec(
            "IS_ULTIMATELY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=[_period("RELATIONSHIP_PERIOD", "2009-05-11T00:00:00.000Z",
                             "2020-01-01T00:00:00.000Z")]))
        assert edge == ("PARENT", "CHILD", "indirect", "2009-05-11", "2020-01-01")

    def test_ignores_non_relationship_periods(self):
        # Only ACCOUNTING/DOCUMENT periods → no relationship start captured.
        edge = _rr_edge(_rec(
            "IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=[_period("ACCOUNTING_PERIOD", "2021-01-01T00:00:00.000Z",
                             "2021-12-31T00:00:00.000Z")]))
        assert edge == ("PARENT", "CHILD", "direct", None, None)

    def test_period_can_be_a_single_object(self):
        edge = _rr_edge(_rec(
            "IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=_period("RELATIONSHIP_PERIOD", "2017-12-05T00:00:00.000Z")))
        assert edge == ("PARENT", "CHILD", "direct", "2017-12-05", None)

    def test_fund_and_branch_types_skipped(self):
        for t in ("IS_FUND-MANAGED_BY", "IS_SUBFUND_OF", "IS_FEEDER_TO",
                  "IS_INTERNATIONAL_BRANCH_OF"):
            assert _rr_edge(_rec(t, "C", "P")) is None

    def test_inactive_skipped(self):
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", status="INACTIVE")) is None

    def test_self_reference_skipped(self):
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "X", "X")) is None

    def test_non_lei_node_skipped(self):
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", parent_type="MIC")) is None


class TestCollapse:
    """GLEIF states a pair twice whenever the direct parent is also the ultimate
    one. Folding those into a single edge is what stops the graph and the profile
    disagreeing about who owns what — measured on the full golden copy, 88,839 of
    257,651 consolidation edges are this case."""

    def _folded(self, *assertions):
        pairs: dict = {}
        for marker, since, until in assertions:
            _fold(pairs, "P", "C", marker, since, until)
        return _collapse(pairs[("P", "C")])

    def test_only_direct_is_unchanged(self):
        assert self._folded(("direct", "2015-01-01", None)) == \
            ("direct", "2015-01-01", None, {})

    def test_only_ultimate_stays_indirect(self):
        """The load-bearing case: GLEIF gave the top of the chain but not its
        steps, so this edge is the only route to the company. Turning it into a
        'direct' edge would assert a holding GLEIF never stated."""
        assert self._folded(("indirect", "2009-05-11", None)) == \
            ("indirect", "2009-05-11", None, {})

    def test_both_collapse_to_one_direct_edge(self):
        marker, since, until, extra = self._folded(
            ("direct", "2015-01-01", None), ("indirect", "2015-01-01", None))
        assert (marker, since, until) == ("direct", "2015-01-01", None)
        assert extra == {"also_ultimate": True}

    def test_the_order_of_the_two_records_does_not_matter(self):
        forward = self._folded(("direct", "2015-01-01", None), ("indirect", "2018-06-01", None))
        reverse = self._folded(("indirect", "2018-06-01", None), ("direct", "2015-01-01", None))
        assert forward == reverse

    def test_a_differing_ultimate_period_is_kept(self):
        """6.9% of the folded pairs on the real file — a parent can be the direct
        consolidator years before it becomes the ultimate one. Dropping the second
        date would lose 6,138 real relationship starts."""
        _marker, since, _until, extra = self._folded(
            ("direct", "2015-01-01", None), ("indirect", "2018-06-01", None))
        assert since == "2015-01-01"                    # the direct claim wins
        assert extra["ultimate_since"] == "2018-06-01"  # the other is not lost

    def test_a_matching_period_is_not_duplicated_onto_the_edge(self):
        _m, _s, _u, extra = self._folded(
            ("direct", "2015-01-01", None), ("indirect", "2015-01-01", None))
        assert "ultimate_since" not in extra

    def test_the_ultimate_record_fills_a_gap(self):
        """Never leaves the edge less dated than the pair of records was."""
        _m, since, _u, extra = self._folded(
            ("direct", None, None), ("indirect", "2018-06-01", None))
        assert since == "2018-06-01"
        assert "ultimate_since" not in extra    # it was used, not shadowed

    def test_a_differing_until_is_kept(self):
        _m, _s, until, extra = self._folded(
            ("direct", None, "2020-01-01"), ("indirect", None, "2022-01-01"))
        assert until == "2020-01-01"
        assert extra["ultimate_until"] == "2022-01-01"


class TestFamilyOf:
    def test_walks_down_and_up_the_tree(self):
        children = {"ROOT": ["A", "B"], "A": ["C"]}
        parents = {"A": ["ROOT"], "B": ["ROOT"], "C": ["A"]}
        assert _family_of({"ROOT"}, children, parents) == {"ROOT", "A", "B", "C"}
        # from a leaf: down = nothing, up = its ancestors
        assert _family_of({"C"}, children, parents) == {"C", "A", "ROOT"}


def _rr_zip(tmp_path, edges):
    """edges: list of (child, parent, rtype)."""
    records = [_rec(rt, c, p) for (c, p, rt) in edges]
    zpath = tmp_path / "rr.json.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("rr.json", json.dumps({"relations": records}))
    return str(zpath)


class TestRrFamilySubset:
    """`--only` seed LEIs → import the whole corporate family + emit its LEIs."""

    def test_imports_only_the_family_and_emits_leis(self, tmp_path, monkeypatch):
        from app.scraper import gleif_rr as m
        written = []
        monkeypatch.setattr(m, "_owns",
            lambda batch, owner_id, owned_id, **k: written.append((owner_id, owned_id)))
        monkeypatch.setattr(m._BatchWriter, "entity", lambda self, nid, props: None)
        monkeypatch.setattr(m._BatchWriter, "flush", lambda self: None)

        z = _rr_zip(tmp_path, [
            ("A", "ROOT", "IS_DIRECTLY_CONSOLIDATED_BY"),
            ("B", "ROOT", "IS_ULTIMATELY_CONSOLIDATED_BY"),
            ("C", "A", "IS_DIRECTLY_CONSOLIDATED_BY"),
            ("Z", "OUTSIDER", "IS_DIRECTLY_CONSOLIDATED_BY"),   # unrelated family
        ])
        emit = tmp_path / "fam.txt"
        res = import_rr_cdf(z, "src", 92, only_leis={"ROOT"}, emit_leis_path=str(emit))

        assert res["family"] == 4                       # ROOT, A, B, C
        assert res["edges"] == 3                         # the OUTSIDER→Z edge excluded
        assert ("lei:OUTSIDER", "lei:Z") not in written
        assert ("lei:ROOT", "lei:A") in written and ("lei:A", "lei:C") in written
        assert set(emit.read_text().split()) == {"ROOT", "A", "B", "C"}


class TestCollapseThroughTheImporter:
    """The fold has to survive the streaming loop, not just the helper — the two
    records for one pair can sit anywhere in the file."""

    def _run(self, tmp_path, monkeypatch, edges):
        from app.scraper import gleif_rr as m
        written: list[dict] = []
        monkeypatch.setattr(m, "_owns", lambda batch, **k: written.append(k))
        monkeypatch.setattr(m._BatchWriter, "entity", lambda self, nid, props: None)
        monkeypatch.setattr(m._BatchWriter, "flush", lambda self: None)
        res = import_rr_cdf(_rr_zip(tmp_path, edges), "src", 92)
        return res, written

    def test_one_edge_for_a_pair_stated_both_ways(self, tmp_path, monkeypatch):
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY"),
            ("CHILD", "PARENT", "IS_ULTIMATELY_CONSOLIDATED_BY"),
        ])
        assert res["records"] == 2          # both records were read
        assert res["edges"] == 1            # one edge written
        assert res["collapsed"] == 1
        assert written[0]["direct_or_indirect"] == "direct"
        assert written[0]["extra"] == {"also_ultimate": True}

    def test_the_records_may_be_far_apart_in_the_file(self, tmp_path, monkeypatch):
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_ULTIMATELY_CONSOLIDATED_BY"),
            ("OTHER", "ELSEWHERE", "IS_DIRECTLY_CONSOLIDATED_BY"),
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY"),
        ])
        assert res["edges"] == 2 and res["collapsed"] == 1
        pair = next(w for w in written if w["owner_id"] == "lei:PARENT")
        assert pair["direct_or_indirect"] == "direct"

    def test_different_parents_are_not_collapsed(self, tmp_path, monkeypatch):
        """The ordinary two-level case: the ultimate parent is a *different*
        company, so its edge is the only link to the child from up there."""
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "DIRECTP", "IS_DIRECTLY_CONSOLIDATED_BY"),
            ("CHILD", "ULTP",    "IS_ULTIMATELY_CONSOLIDATED_BY"),
        ])
        assert res["edges"] == 2 and res["collapsed"] == 0
        assert {w["owner_id"] for w in written} == {"lei:DIRECTP", "lei:ULTP"}
        by_owner = {w["owner_id"]: w["direct_or_indirect"] for w in written}
        assert by_owner == {"lei:DIRECTP": "direct", "lei:ULTP": "indirect"}

    def test_no_extra_props_when_there_is_nothing_to_record(self, tmp_path, monkeypatch):
        """Every other importer, and every uncollapsed edge, keeps the exact
        property set it had before — no nulls added across ~170k edges."""
        _res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY"),
        ])
        assert written[0]["extra"] == {}
