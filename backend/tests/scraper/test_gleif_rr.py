"""Unit tests for GLEIF RR-CDF relationship parsing (DB not involved).

End-to-end write (edge with direct_or_indirect) is covered against a real
ArcadeDB in tests/integration/test_gleif_rr_it.py."""

import json
import zipfile

from app.scraper.gleif_rr import (
    _collapse, _family_of, _fold, _node_lei, _rr_edge, _v, import_rr_cdf,
)


def _rec(rtype, child, parent, status="ACTIVE", child_type="LEI", parent_type="LEI",
         periods=None, updated=None, rr_status=None, quantifiers=None, validation=None):
    rel = {
        "StartNode": {"NodeID": {"$": child}, "NodeIDType": {"$": child_type}},
        "EndNode":   {"NodeID": {"$": parent}, "NodeIDType": {"$": parent_type}},
        "RelationshipType":   {"$": rtype},
        "RelationshipStatus": {"$": status},
    }
    if periods is not None:
        rel["RelationshipPeriods"] = {"RelationshipPeriod": periods}
    if quantifiers is not None:
        rel["RelationshipQuantifiers"] = {"RelationshipQuantifier": quantifiers}
    rec = {"RelationshipRecord": {"Relationship": rel}}
    registration = {}
    if updated:
        registration["LastUpdateDate"] = {"$": updated}
    if rr_status:
        registration["RegistrationStatus"] = {"$": rr_status}
    for key, value in (validation or {}).items():
        registration[key] = {"$": value}
    if registration:
        rec["RelationshipRecord"]["Registration"] = registration
    return rec


def _quant(amount, method="ACCOUNTING_CONSOLIDATION", units="PERCENTAGE"):
    """A RelationshipQuantifier as the golden copy writes it (units optional)."""
    q = {"MeasurementMethod": {"$": method}, "QuantifierAmount": {"$": amount}}
    if units is not None:
        q["QuantifierUnits"] = {"$": units}
    return q


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
            ("PARENT", "CHILD", "direct", None, None, True, None, None, False, None, False, {})

    def test_ultimate_is_indirect(self):
        assert _rr_edge(_rec("IS_ULTIMATELY_CONSOLIDATED_BY", "CHILD", "PARENT")) == \
            ("PARENT", "CHILD", "indirect", None, None, True, None, None, False, None, False, {})

    def test_captures_relationship_period_start_as_since(self):
        # The RELATIONSHIP_PERIOD start date is the ownership start → `since` (date only).
        edge = _rr_edge(_rec(
            "IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=[_period("ACCOUNTING_PERIOD", "2023-05-17T00:00:00.000Z",
                             "2023-09-30T00:00:00.000Z"),
                     _period("RELATIONSHIP_PERIOD", "2023-05-17T00:00:00.000Z")]))
        assert edge == ("PARENT", "CHILD", "direct", "2023-05-17", None, True, None, None, False, None, False, {})

    def test_captures_relationship_period_end_as_until(self):
        edge = _rr_edge(_rec(
            "IS_ULTIMATELY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=[_period("RELATIONSHIP_PERIOD", "2009-05-11T00:00:00.000Z",
                             "2020-01-01T00:00:00.000Z")]))
        assert edge == ("PARENT", "CHILD", "indirect", "2009-05-11", "2020-01-01", True, None, None, False, None, False, {})

    def test_ignores_non_relationship_periods(self):
        # Only ACCOUNTING/DOCUMENT periods → no relationship start captured.
        edge = _rr_edge(_rec(
            "IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=[_period("ACCOUNTING_PERIOD", "2021-01-01T00:00:00.000Z",
                             "2021-12-31T00:00:00.000Z")]))
        assert edge == ("PARENT", "CHILD", "direct", None, None, True, None, None, False, None, False, {})

    def test_period_can_be_a_single_object(self):
        edge = _rr_edge(_rec(
            "IS_DIRECTLY_CONSOLIDATED_BY", "CHILD", "PARENT",
            periods=_period("RELATIONSHIP_PERIOD", "2017-12-05T00:00:00.000Z")))
        assert edge == ("PARENT", "CHILD", "direct", "2017-12-05", None, True, None, None, False, None, False, {})

    def test_fund_and_branch_types_skipped(self):
        for t in ("IS_FUND-MANAGED_BY", "IS_SUBFUND_OF", "IS_FEEDER_TO",
                  "IS_INTERNATIONAL_BRANCH_OF"):
            assert _rr_edge(_rec(t, "C", "P")) is None

    def test_an_ended_relationship_is_kept_as_history(self):
        # INACTIVE used to be dropped: a holding sold in 2020 never existed in 2015
        edge = _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", status="INACTIVE",
                             periods=[_period("RELATIONSHIP_PERIOD", "2010-03-01T00:00:00Z",
                                              "2020-06-30T00:00:00Z")],
                             updated="2020-07-02T08:00:00Z"))
        assert edge == ("P", "C", "direct", "2010-03-01", "2020-06-30", False, "2020-07-02", None, False, None, False, {})

    def test_an_ended_relationship_without_an_end_date_ended_by_its_last_update(self):
        edge = _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", status="INACTIVE",
                             periods=[_period("RELATIONSHIP_PERIOD", "2010-03-01T00:00:00Z")],
                             updated="2021-11-05T08:00:00Z"))
        assert edge == ("P", "C", "direct", "2010-03-01", "2021-11-05", False, "2021-11-05",
                        "gleif_inactive", False, None, False, {})
        # not even a last update: nothing to draw
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", status="INACTIVE")) is None

    def test_an_unknown_status_is_skipped(self):
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", status="LAPSED")) is None

    def test_a_lapsed_record_is_read_as_unmaintained(self):
        """GLEIF: LAPSED = "not renewed by its NextRenewalDate". The relationship
        is still ACTIVE on it — Metaswitch "consolidated by Microsoft", last
        updated 2023-06-02, sold in 2025 — so it is drawn, and flagged."""
        edge = _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", rr_status="LAPSED",
                             updated="2023-06-02T00:10:55Z"))
        assert edge == ("P", "C", "direct", None, None, True, "2023-06-02", None, True, None, False, {})
        # PUBLISHED, or no status at all (older fixtures), is maintained
        for st in ("PUBLISHED", "PENDING_TRANSFER", "PENDING_ARCHIVAL", None):
            assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", rr_status=st))[8] is False

    def test_the_stated_share_is_read_as_the_stake(self):
        """49,527 of the ~260k consolidation records state one (2026-10-06):
        40,025 say 100, 8,765 between 50 and 99.99, 453 below 50 — a
        consolidated interest, stored as the register stated it."""
        edge = _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("100.00")]))
        assert edge[9:11] == (100.0, False)
        assert _rr_edge(_rec("IS_ULTIMATELY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("50.1")]))[9] == 50.1
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("33.333333")]))[9] == 33.3333
        # 16,785 of them state no units: under ACCOUNTING_CONSOLIDATION the
        # amount is a percentage all the same
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("75", units=None)]))[9] == 75.0
        # a single quantifier arrives as a dict, as the JSON does for one entry
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=_quant("60")))[9] == 60.0

    def test_other_methods_and_units_are_not_a_stake(self):
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("100", method="OTHER")]))[9:11] == (None, False)
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("100", units="SHARES")]))[9:11] == (None, False)
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("n/a")]))[9:11] == (None, False)
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P"))[9:11] == (None, False)

    def test_an_implausible_share_is_refused_not_stored(self):
        """269 records at or below 0 and 15 above 100 on 2026-10-06: entry
        errors, not facts — the checklist's plausibility bound at the writer.
        Refused and flagged so the import counts and logs them; the edge is
        still drawn, without a stake."""
        for bad in ("0", "-5", "100.01", "1000"):
            edge = _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", quantifiers=[_quant(bad)]))
            assert edge is not None and edge[9:11] == (None, True), bad
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P",
                             quantifiers=[_quant("0.01")]))[9:11] == (0.01, False)

    def test_how_far_the_lou_checked_the_record_comes_along(self):
        """ValidationSources / ValidationDocuments as stated — provenance on the
        edge and its claim, never a credibility change: 42 % of the active
        consolidation records (109,060 of 260,337 on 2026-10-06) are
        ENTITY_SUPPLIED_ONLY, and a penalty would drop them out of the tier."""
        edge = _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", validation={
            "ValidationSources": "FULLY_CORROBORATED", "ValidationDocuments": "ACCOUNTS_FILING"}))
        assert edge[11] == {"validation_sources": "FULLY_CORROBORATED",
                            "validation_documents": "ACCOUNTS_FILING"}
        # only what the record states: no None placeholders
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", validation={
            "ValidationSources": "ENTITY_SUPPLIED_ONLY"}))[11] == {
                "validation_sources": "ENTITY_SUPPLIED_ONLY"}
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P"))[11] == {}

    def test_an_annulled_or_duplicate_record_is_no_statement(self):
        # "erroneous or invalid after it was issued" / the non-surviving copy
        for st in ("ANNULLED", "DUPLICATE"):
            assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", rr_status=st)) is None
            # the status alone decides — even an ACTIVE, dated relationship on it
            assert _rr_edge(_rec("IS_ULTIMATELY_CONSOLIDATED_BY", "C", "P", rr_status=st,
                                 periods=[_period("RELATIONSHIP_PERIOD", "2015-01-01T00:00:00Z")])) is None

    def test_the_period_that_describes_the_record(self):
        # ended 2015, begun again 2018: the open period, whatever the file order
        for order in (1, -1):
            periods = [_period("RELATIONSHIP_PERIOD", "2010-01-01T00:00:00Z", "2015-01-01T00:00:00Z"),
                       _period("RELATIONSHIP_PERIOD", "2018-01-01T00:00:00Z")][::order]
            edge = _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", periods=periods))
            assert edge[3:5] == ("2018-01-01", None)
        # all ended: the latest-ending
        periods = [_period("RELATIONSHIP_PERIOD", "2018-01-01T00:00:00Z", "2022-01-01T00:00:00Z"),
                   _period("RELATIONSHIP_PERIOD", "2010-01-01T00:00:00Z", "2015-01-01T00:00:00Z")]
        assert _rr_edge(_rec("IS_DIRECTLY_CONSOLIDATED_BY", "C", "P", periods=periods,
                             status="INACTIVE"))[3:5] == ("2018-01-01", "2022-01-01")

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
        for marker, since, until, *rest in assertions:
            _fold(pairs, "P", "C", marker, since, until, unmaintained=bool(rest and rest[0]),
                  stake=(rest[1] if len(rest) > 1 else None),
                  validation=(rest[2] if len(rest) > 2 else None))
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
        assert since == "2015-01-01"                    # the earlier of the two
        assert extra["ultimate_since"] == "2018-06-01"  # the other is not lost

    def test_the_edge_starts_at_the_earlier_relationship(self):
        """Top of the tree since 2005, the direct parent only since 2020: the
        parent has held the child since 2005 — time travel reads `since`, and
        the direct date alone hid the holding for 2005–2019."""
        _m, since, _u, extra = self._folded(
            ("direct", "2020-01-01", None), ("indirect", "2005-01-01", None))
        assert since == "2005-01-01"
        assert "ultimate_since" not in extra     # it IS the edge's start

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

    def test_the_holding_ends_when_both_relationships_have(self):
        # one record's end used to close the edge while the other still stood
        _m, _s, until, _x = self._folded(
            ("direct", None, "2020-01-01"), ("indirect", None, "2022-01-01"))
        assert until == "2022-01-01"
        _m, _s, until, _x = self._folded(
            ("direct", None, "2020-01-01"), ("indirect", None, None))
        assert until is None

    def test_the_record_dates_become_the_evidence_date(self):
        pairs: dict = {}
        _fold(pairs, "P", "C", "direct", "2015-01-01", None, "2025-03-01")
        _fold(pairs, "P", "C", "indirect", "2015-01-01", None, "2026-01-04")
        assert _collapse(pairs[("P", "C")])[3]["source_date"] == "2026-01-04"

    def test_a_lapsed_record_alone_makes_the_edge_stale(self):
        """62,934 of the ~260k ACTIVE consolidation records on 2026-10-06 stand
        on a LAPSED child — a quarter of GLEIF's ownership. Dimmed and dated by
        its evidence date, never closed: nobody stated an end."""
        # dimmed WITH its reason and date (the record's last update), so the
        # timeline can say why the edge went grey
        lapsed = {"stale": True, "stale_reason": "lei_lapsed", "stale_since": None}
        assert self._folded(("direct", "2022-04-11", None, True))[3] == lapsed
        assert self._folded(("indirect", None, None, True))[3] == lapsed
        assert "stale" not in self._folded(("direct", "2022-04-11", None, False))[3]

    def test_a_maintained_record_keeps_a_folded_pair_fresh(self):
        """Stale only when EVERY record the edge stands on has lapsed: a
        maintained ultimate record still confirms the pair."""
        assert "stale" not in self._folded(("direct", None, None, True), ("indirect", None, None, False))[3]
        assert "stale" not in self._folded(("direct", None, None, False), ("indirect", None, None, True))[3]
        both = self._folded(("direct", None, None, True), ("indirect", None, None, True))[3]
        assert both == {"also_ultimate": True, "stale": True, "stale_reason": "lei_lapsed", "stale_since": None}

    def test_a_folded_pair_both_lapsed_is_dimmed_with_its_reason_and_the_later_date(self):
        """Findings item 32: the folded edge was marked stale without why or since
        when (Microsoft → Activision Blizzard International B.V.)."""
        pairs: dict = {}
        _fold(pairs, "P", "C", "direct", "2026-07-01", None, "2026-07-01", unmaintained=True)
        _fold(pairs, "P", "C", "indirect", "2026-07-01", None, "2025-03-01", unmaintained=True)
        extra = _collapse(pairs[("P", "C")])[3]
        assert (extra["stale"], extra["stale_reason"], extra["stale_since"]) == (True, "lei_lapsed", "2026-07-01")
        assert extra["stale_since"] == extra["source_date"]

    def test_the_stated_share_rides_on_the_edge(self):
        assert self._folded(("direct", None, None, False, 100.0))[3] == {"stake_percent": 100.0}
        assert self._folded(("indirect", None, None, False, 51.0))[3] == {"stake_percent": 51.0}
        assert "stake_percent" not in self._folded(("direct", None, None, False, None))[3]

    def test_the_direct_figure_wins_and_the_ultimate_one_fills(self):
        """A direct parent's share is the holding; the ultimate parent's figure
        is the consolidated interest from the top — used only where the direct
        record states none."""
        both = self._folded(("direct", None, None, False, 60.0), ("indirect", None, None, False, 100.0))[3]
        assert both["stake_percent"] == 60.0
        fill = self._folded(("direct", None, None, False, None), ("indirect", None, None, False, 100.0))[3]
        assert fill["stake_percent"] == 100.0
        none = self._folded(("direct", None, None, False, None), ("indirect", None, None, False, None))[3]
        assert "stake_percent" not in none

    def test_how_far_it_was_checked_follows_the_same_rule(self):
        full = {"validation_sources": "FULLY_CORROBORATED", "validation_documents": "ACCOUNTS_FILING"}
        own = {"validation_sources": "ENTITY_SUPPLIED_ONLY"}
        assert self._folded(("direct", None, None, False, None, own))[3] == own
        assert self._folded(("indirect", None, None, False, None, full))[3] == full
        # the direct record's checking wins; the ultimate's fills a gap
        both = self._folded(("direct", None, None, False, None, own), ("indirect", None, None, False, None, full))[3]
        assert both["validation_sources"] == "ENTITY_SUPPLIED_ONLY" and "validation_documents" not in both
        fill = self._folded(("direct", None, None, False, None, {}), ("indirect", None, None, False, None, full))[3]
        assert fill["validation_sources"] == "FULLY_CORROBORATED"
        assert "validation_sources" not in self._folded(("direct", None, None, False, None, {}))[3]


class TestFamilyOf:
    def test_walks_down_and_up_the_tree(self):
        children = {"ROOT": ["A", "B"], "A": ["C"]}
        parents = {"A": ["ROOT"], "B": ["ROOT"], "C": ["A"]}
        assert _family_of({"ROOT"}, children, parents) == {"ROOT", "A", "B", "C"}
        # from a leaf: down = nothing, up = its ancestors
        assert _family_of({"C"}, children, parents) == {"C", "A", "ROOT"}


def _rr_zip(tmp_path, edges):
    """edges: list of (child, parent, rtype[, rr_status[, stake amount[, validation]]])."""
    records = [_rec(e[2], e[0], e[1], rr_status=(e[3] if len(e) > 3 else None),
                    quantifiers=([_quant(e[4])] if len(e) > 4 and e[4] else None),
                    validation=(e[5] if len(e) > 5 else None)) for e in edges]
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
        # filing_type rides in extra on every RR edge — it is what finally
        # distinguishes RR provenance from LEI-CDF in the stored graph.
        assert written[0]["extra"] == {"stale": False, "also_ultimate": True, "filing_type": "RR"}

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
            # "nothing to record" means no fold artifacts — the constant RR tag is
        # deliberate and carries no nulls.
        assert written[0]["extra"] == {"stale": False, "filing_type": "RR"}   # a flag is never unset

    def test_a_lapsed_record_draws_a_stale_edge(self, tmp_path, monkeypatch):
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "LAPSED"),
            ("OTHER", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "PUBLISHED"),
        ])
        assert res["edges"] == 2 and res["unmaintained"] == 1
        by_child = {w["owned_id"]: w["extra"] for w in written}
        assert by_child["lei:CHILD"] == {"filing_type": "RR", "stale": True,
                                         "stale_reason": "lei_lapsed", "stale_since": None}
        assert by_child["lei:OTHER"] == {"stale": False, "filing_type": "RR"}

    def test_a_maintained_ultimate_record_keeps_the_folded_pair_fresh(self, tmp_path, monkeypatch):
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "LAPSED"),
            ("CHILD", "PARENT", "IS_ULTIMATELY_CONSOLIDATED_BY", "PUBLISHED"),
        ])
        assert res["edges"] == 1 and res["unmaintained"] == 0
        assert written[0]["extra"]["stale"] is False

    def test_the_stated_share_is_written_and_an_implausible_one_refused(self, tmp_path, monkeypatch):
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "PUBLISHED", "100.00"),
            ("OTHER", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "PUBLISHED", "250"),
            ("THIRD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "PUBLISHED"),
        ])
        assert res["edges"] == 3 and res["with_stake"] == 1 and res["implausible"] == 1
        by_child = {w["owned_id"]: w for w in written}
        assert by_child["lei:CHILD"]["extra"]["stake_percent"] == 100.0
        assert "stake_percent" not in by_child["lei:OTHER"]["extra"]    # refused, edge still drawn
        assert "stake_percent" not in by_child["lei:THIRD"]["extra"]    # none stated, no null added
        assert by_child["lei:THIRD"]["stake_percent"] is None

    def test_how_far_each_record_was_checked_is_written_and_counted(self, tmp_path, monkeypatch):
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "PUBLISHED", None,
             {"ValidationSources": "ENTITY_SUPPLIED_ONLY", "ValidationDocuments": "OTHER_OFFICIAL_DOCUMENTS"}),
            ("OTHER", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "PUBLISHED", None,
             {"ValidationSources": "FULLY_CORROBORATED", "ValidationDocuments": "ACCOUNTS_FILING"}),
            ("THIRD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "PUBLISHED"),
        ])
        assert res["entity_supplied"] == 1
        by_child = {w["owned_id"]: w["extra"] for w in written}
        assert by_child["lei:CHILD"] == {"stale": False, "filing_type": "RR", "validation_sources": "ENTITY_SUPPLIED_ONLY",
                                         "validation_documents": "OTHER_OFFICIAL_DOCUMENTS"}
        assert by_child["lei:OTHER"]["validation_sources"] == "FULLY_CORROBORATED"
        assert by_child["lei:THIRD"] == {"stale": False, "filing_type": "RR"}
        # not a credibility change
        assert {w["credibility_score"] for w in written} == {92}

    def test_an_annulled_record_is_counted_and_not_drawn(self, tmp_path, monkeypatch):
        res, written = self._run(tmp_path, monkeypatch, [
            ("CHILD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "ANNULLED"),
            ("OTHER", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY", "DUPLICATE"),
            ("THIRD", "PARENT", "IS_DIRECTLY_CONSOLIDATED_BY"),
        ])
        assert res["annulled"] == 2 and res["skipped"] == 0
        assert [w["owned_id"] for w in written] == ["lei:THIRD"]
