"""The /rebuild kit's snapshot and its diff (findings item 51).

The snapshot's OWNS rows carry the filing type, and the diff tells a 13F
holder that came or went WITH its filer node (the relevance sample's boundary
moving — Dixon Financial, rank 100 of 100) from a lost edge. Snapshots from
before item 51 carry four fields an edge and are still read."""
import importlib.util
import pathlib

import pytest

KIT = pathlib.Path(__file__).resolve().parents[2] / ".claude" / "skills" / "rebuild" / "kit"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


snapshot = _load("snapshot")
diff = _load("diff_snapshots")

BRK, DIXON, BLK = "lei:5493000C01ZX7D35SD85", "sec:0001909993", "sec:0001364742"


def _fake_sql(sql):
    if "schema:types" in sql:
        return [{"name": "Entity"}, {"name": "OWNS"}]
    if "count(*)" in sql:
        return [{"n": 2}]
    if sql.startswith("SELECT id, name, source_id"):
        return [{"id": BRK, "name": "BERKSHIRE HATHAWAY INC", "source_id": "gleif", "sec_cik": "0001067983",
                 "lei_id": "5493000C01ZX7D35SD85", "wikidata_id": None},
                {"id": DIXON, "name": "DIXON FNANCIAL SERVICES, INC.", "source_id": "sec-edgar",
                 "sec_cik": "0001909993", "lei_id": None, "wikidata_id": None}]
    if sql.startswith("SELECT id FROM"):
        return [{"id": BRK}, {"id": DIXON}]
    return []


GROUP = "33333333-3333-3333-3333-333333333333"
OLD, NEW = "lei:529900NBET57WDRXAR22", "lei:529900G1S6D7T5ZIX198"     # Random House LLC -> Penguin Random House LLC
lei_ids = _load("lei_ids")


def _fake_cypher(cy):
    if "[r:OWNS]" in cy:
        return [{"a": DIXON, "b": BRK, "s": "sec-edgar", "u": None, "f": "13F"},
                {"a": BLK, "b": BRK, "s": "sec-edgar", "u": "2026-03-13", "f": "13G/A"},
                {"a": GROUP, "b": BRK, "s": "sec-edgar", "u": None, "f": "13D"}]
    if "[r:SUCCEEDED_BY]" in cy:
        return [{"a": OLD, "b": NEW, "s": "gleif"}]
    if "VotingGroup" in cy:
        # collect() gives the members in storage order, with a None for a party
        # whose node has neither name nor full_name
        return [{"id": GROUP, "name": "Voting group · 3 parties", "nn": "voting group berkshire hathaway",
                 "members": ["Warren Buffett", "GEICO Corporation", None, "BNSF Master Retirement Trust"]}]
    return []


class TestSnapshot:
    def test_every_owns_row_carries_its_filing_type(self):
        snap = snapshot.take(_fake_sql, _fake_cypher)
        assert snap["owns_edges"] == [[GROUP, BRK, "sec-edgar", None, "13D"],
                                      [BLK, BRK, "sec-edgar", "2026-03-13", "13G/A"],
                                      [DIXON, BRK, "sec-edgar", None, "13F"]]
        assert all(len(row) == len(diff.OWNS_FIELDS) for row in snap["owns_edges"])

    def test_the_cypher_asks_for_the_filing_type(self):
        asked = []
        def cy(q):
            asked.append(q)
            return _fake_cypher(q)
        snapshot.take(_fake_sql, cy)
        owns = next(q for q in asked if "[r:OWNS]" in q)
        assert "r.filing_type AS f" in owns

    def test_voting_groups_are_dumped_with_their_members_sorted(self):
        # findings item 56: a group's uuid is minted anew by every rebuild; its
        # identity is the issuer (in the name key) and the roster
        snap = snapshot.take(_fake_sql, _fake_cypher)
        assert snap["voting_groups"] == [[GROUP, "Voting group · 3 parties", "voting group berkshire hathaway",
                                         ["BNSF Master Retirement Trust", "GEICO Corporation", "Warren Buffett"]]]

    def test_the_group_cypher_reads_the_members_through_member_of(self):
        asked = []
        def cy(q):
            asked.append(q)
            return _fake_cypher(q)
        snapshot.take(_fake_sql, cy)
        groups = next(q for q in asked if "VotingGroup" in q)
        assert "OPTIONAL MATCH (m)-[:MEMBER_OF]->(g)" in groups and "coalesce(m.name, m.full_name)" in groups


def _snap(entities, edges, groups=None):
    snap = {"entities": entities, "persons": [], "owns_edges": edges}
    if groups is not None:
        snap["voting_groups"] = groups
    return snap


ISSUER = [BRK, "BERKSHIRE HATHAWAY INC", "gleif", "0001067983", "5493000C01ZX7D35SD85", None]
DIXON_NODE = [DIXON, "DIXON FNANCIAL SERVICES, INC.", "sec-edgar", "0001909993", None, None]
BLK_NODE = [BLK, "BlackRock Finance, Inc.", "sec-edgar", "0001364742", None, None]


class TestDiff:
    def test_a_13f_holder_gone_with_its_node_is_sample_churn(self):
        prev = _snap([ISSUER, DIXON_NODE], [[DIXON, BRK, "sec-edgar", None, "13F"]])
        new = _snap([ISSUER], [])
        c = diff.classify(prev, new)
        assert sum(c["churn_gone"].values()) == 1 and not c["gone"] and not c["new"] and not c["churn_new"]

    def test_a_13f_holder_new_with_its_node_is_sample_churn(self):
        prev = _snap([ISSUER], [])
        new = _snap([ISSUER, DIXON_NODE], [[DIXON, BRK, "sec-edgar", None, "13F"]])
        c = diff.classify(prev, new)
        assert sum(c["churn_new"].values()) == 1 and not c["new"] and not c["gone"]

    def test_a_13f_edge_lost_by_a_filer_the_graph_keeps_is_a_finding(self):
        # findings item 40: BlackRock stayed in the graph, its 13F edge went
        prev = _snap([ISSUER, BLK_NODE], [[BLK, BRK, "sec-edgar", None, "13F"]])
        new = _snap([ISSUER, BLK_NODE], [])
        c = diff.classify(prev, new)
        assert sum(c["gone"].values()) == 1 and not c["churn_gone"]

    def test_a_non_13f_edge_gone_with_its_node_is_a_finding(self):
        prev = _snap([ISSUER, BLK_NODE], [[BLK, BRK, "sec-edgar", None, "13G/A"]])
        new = _snap([ISSUER], [])
        c = diff.classify(prev, new)
        assert sum(c["gone"].values()) == 1 and not c["churn_gone"]

    def test_a_snapshot_from_before_item_51_is_read_without_a_filing_type(self):
        prev = _snap([ISSUER, DIXON_NODE], [[DIXON, BRK, "sec-edgar", None]])
        new = _snap([ISSUER], [])
        assert diff.edge([DIXON, BRK, "sec-edgar", None])["filing_type"] is None
        c = diff.classify(prev, new)
        # no filing type to go by: judged as a finding, never silently as churn
        assert sum(c["gone"].values()) == 1 and not c["churn_gone"]

    def test_uuid_ends_are_named_by_their_names(self):
        u1, u2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        prev = _snap([ISSUER, [u1, "Sub LLC", "sec-edgar", None, None, None]], [[BRK, u1, "sec-edgar", None, "EX-21"]])
        new = _snap([ISSUER, [u2, "Sub LLC", "sec-edgar", None, None, None]], [[BRK, u2, "sec-edgar", None, "EX-21"]])
        c = diff.classify(prev, new)
        assert not c["gone"] and not c["new"]


G1, G2 = "44444444-4444-4444-4444-444444444444", "55555555-5555-5555-5555-555555555555"
ROSTER_A = ["BNSF Master Retirement Trust", "GEICO Corporation", "Warren Buffett"]
ROSTER_B = ["Berkshire Hathaway Consolidated Pension Plan Master Trust", "GEICO Corporation"]


def _group(gid, roster, nn="voting group berkshire hathaway"):
    return [gid, f"Voting group · {len(roster)} parties", nn, roster]


class TestDiffVotingGroups:
    """findings item 56: 37 group-owned edges read as gone + new at every verify."""

    def test_a_groups_edge_survives_the_re_minted_uuid(self):
        prev = _snap([ISSUER], [[G1, BRK, "sec-edgar", None, "13D"]], [_group(G1, ROSTER_A)])
        new = _snap([ISSUER], [[G2, BRK, "sec-edgar", None, "13D"]], [_group(G2, ROSTER_A)])
        c = diff.classify(prev, new)
        assert not c["gone"] and not c["new"]

    def test_two_blocs_over_one_issuer_are_two_keys(self):
        # the name key carries only the issuer: Better Home & Finance has six
        # groups on dev, 24 distinct name keys for 37 groups
        assert diff.group_key(_group(G1, ROSTER_A)) != diff.group_key(_group(G2, ROSTER_B))
        prev = _snap([ISSUER], [[G1, BRK, "sec-edgar", None, "13D"]], [_group(G1, ROSTER_A)])
        new = _snap([ISSUER], [[G2, BRK, "sec-edgar", None, "13D"]], [_group(G2, ROSTER_B)])
        c = diff.classify(prev, new)
        assert sum(c["gone"].values()) == 1 and sum(c["new"].values()) == 1, "a changed roster is a changed group"

    def test_the_key_names_the_issuer_and_the_members(self):
        assert diff.group_key(_group(G1, ROSTER_A)) == (
            "voting group berkshire hathaway [BNSF Master Retirement Trust, GEICO Corporation, Warren Buffett]")

    def test_a_snapshot_from_before_item_56_keeps_the_raw_uuid(self):
        prev = _snap([ISSUER], [[G1, BRK, "sec-edgar", None, "13D"]])            # no voting_groups list
        new = _snap([ISSUER], [[G2, BRK, "sec-edgar", None, "13D"]], [_group(G2, ROSTER_A)])
        assert diff.names(prev)[BRK] == BRK and G1 not in diff.names(prev)
        c = diff.classify(prev, new)
        assert sum(c["gone"].values()) == 1 and sum(c["new"].values()) == 1, "the one-time transition, never silent"


@pytest.mark.parametrize("value, expected", [("13F", True), ("13F-HR", True), ("13G/A", False), (None, False)])
def test_is_13f(value, expected):
    assert diff.is_13f(value) is expected


class TestSuccession:
    """Findings item 89: the SUCCEEDED_BY edges as a list in the snapshot, a
    diff section by name, and the LEI list step 7 feeds the succession pass."""

    def test_the_snapshot_dumps_every_succession_edge(self):
        snap = snapshot.take(_fake_sql, _fake_cypher)
        assert snap["succeeded_by"] == [[OLD, NEW, "gleif"]]

    def test_the_diff_names_the_links_gone_and_new(self, capsys):
        names_ = [[OLD, "Random House LLC", "gleif", None, "529900NBET57WDRXAR22", None],
                  [NEW, "Penguin Random House LLC", "gleif", None, "529900G1S6D7T5ZIX198", None]]
        before = {"entities": names_, "persons": [], "succeeded_by": []}
        after = {"entities": names_, "persons": [], "succeeded_by": [[OLD, NEW, "gleif"], [NEW, NEW, "gleif"]]}
        diff._print_succession(before, after)
        out = capsys.readouterr().out
        assert "succession (SUCCEEDED_BY): 0 -> 2; gone 0, new 2" in out
        assert f"+ ('{OLD}', '{NEW}') (Random House LLC -> Penguin Random House LLC) 1" in out
        assert "self-loops (a merge folded both ends, item 88)" in out        # the item-88 shape is called out

    def test_a_snapshot_from_before_the_list_prints_a_note_and_no_error(self, capsys):
        diff._print_succession({"entities": [], "persons": []}, {"entities": [], "persons": [], "succeeded_by": []})
        assert "no edge list in one snapshot (before item 89)" in capsys.readouterr().out

    def test_the_lei_list_is_every_lei_node_bare_and_sorted(self):
        def q(sql):
            assert "lei:%" in sql
            return [{"id": NEW}, {"id": OLD}, {"id": "lei:short"}]
        assert lei_ids.ids(q) == ["529900G1S6D7T5ZIX198", "529900NBET57WDRXAR22"]
        assert lei_ids.ids(lambda sql: None) == []


class TestSourceCalls:
    def test_the_snapshot_keeps_the_calls_per_source_and_kind(self):
        def sql(q):
            if "FROM SourceCall" in q:
                return [{"source": "sec-edgar", "kind": "ok", "n": 9000},
                        {"source": "bucket:edgar", "kind": "hit", "n": 4000},
                        {"source": "sec-edgar", "kind": "429", "n": 2}]
            return _fake_sql(q)
        snap = snapshot.take(sql, _fake_cypher)
        assert snap["source_calls"] == {"bucket:edgar": {"hit": 4000},
                                        "sec-edgar": {"ok": 9000, "429": 2}}

    def test_the_diff_prints_them_previous_against_new(self, capsys):
        diff._print_source_calls({"source_calls": {"sec-edgar": {"ok": 9000}}},
                                 {"source_calls": {"sec-edgar": {"ok": 8700, "429": 2},
                                                   "gleif": {"ok": 40}}})
        out = capsys.readouterr().out
        assert "gleif                  ok - -> 40" in out
        assert "sec-edgar              429 - -> 2, ok 9000 -> 8700" in out

    def test_a_snapshot_from_before_the_counts_prints_nothing(self, capsys):
        diff._print_source_calls({}, {})
        assert capsys.readouterr().out == ""


def _o(a, b, p, c=None, f="13D/A", st=False):
    return {"a": a, "b": b, "an": a.upper(), "bn": b.upper(), "p": p, "c": c, "f": f, "st": st}


class TestOver100:
    """Findings item 97: the companies whose disclosed stakes add up over
    100 %, as the panel sums them (the app's `_ownership_summary`)."""

    def test_a_company_over_100_is_listed_with_its_owners_highest_first(self):
        out = snapshot.over_100([_o("x", "sirius", 49.5, st=True), _o("y", "sirius", 40.15), _o("z", "sirius", 37.1)])
        assert out == [["sirius", "SIRIUS", 126.75, [["x", "X", 49.5, "13D/A", True],
                                                      ["y", "Y", 40.15, "13D/A", False],
                                                      ["z", "Z", 37.1, "13D/A", False]]]]

    @pytest.mark.parametrize("rows", [
        [_o("x", "c", 60), _o("y", "c", 40.4)],                       # within the panel's 0.5 tolerance
        [_o("x", "c", 60), _o("x", "c", 55)],                         # one owner, two edges: its highest counts, alone
        [_o("c", "c", 99), _o("y", "c", 60)],                         # a self-loop is dropped, as on the panel
        [_o("x", "c", 60, "Class A"), _o("y", "c", 60, "Class B")],   # two named classes: no single total
        [_o("x", "c", None), _o("y", "c", 90)],                       # an unknown stake adds nothing
    ])
    def test_what_the_panel_does_not_flag_is_not_listed(self, rows):
        assert snapshot.over_100(rows) == []

    def test_an_indirect_owner_beside_a_direct_one_is_not_summed(self):
        # findings item 101: Heineken N.V. (indirect) beside 3-101-949828 S.A. (direct)
        rows = [{**_o("heineken", "florida", 100.0, f="RR"), "d": "indirect"},
                {**_o("costa", "florida", 100.0, f="RR"), "d": "direct"}]
        assert snapshot.over_100(rows) == []
        # and a company that is over 100 % lists only what was added
        rows += [{**_o("z", "florida", 30.0), "d": None}]
        assert [[o[0] for o in r[3]] for r in snapshot.over_100(rows)] == [["costa", "z"]]
        assert snapshot.over_100(rows)[0][2] == 130.0

    def test_a_controller_s_13d_beside_the_company_it_controls_is_not_summed(self):
        # findings item 102: BRC's 13D/A on AmBev counts AB InBev's shares; BRC
        # votes 52.3 % of AB InBev in its bloc, so it controls it
        rows = [_o("brc", "ambev", 71.9), _o("abi", "ambev", 61.73, f="EX-8.1"),
                {**_o("brc", "abi", None), "v": 52.3, "ot": "minority"}]
        assert snapshot.over_100(rows) == []
        # a passive holding in the other owner is no control: both are summed
        rows[2] = {**_o("brc", "abi", 7.0, f="13G")}
        assert [r[2] for r in snapshot.over_100(rows)] == [133.63]

    def test_one_owner_counts_once_with_its_highest_stake(self):
        rows = [_o("x", "c", 60), _o("x", "c", 30), _o("y", "c", 45)]
        assert [r[2] for r in snapshot.over_100(rows)] == [105.0]
        assert snapshot.over_100(list(reversed(rows)))[0][3][0][2] == 60

    def test_the_snapshot_reads_live_drawn_edges_only(self):
        seen = []
        def cy(q):
            seen.append(q)
            return _fake_cypher(q)
        snap = snapshot.take(_fake_sql, cy)
        q = next(x for x in seen if "[o:OWNS]" in x)
        assert "o.until IS NULL" in q and "COALESCE(o.shortcut, false) <> true" in q
        assert "o.direct_or_indirect AS d" in q        # the summary needs the mode (item 101)
        assert snap["over_100"] == [] and snap["cycles"] == []


class TestCycles:
    @pytest.mark.parametrize("edges, expected", [
        ([("a", "b"), ("b", "c")], []),                                        # a chain
        ([("texaco", "chevron"), ("chevron", "texaco")], [["chevron", "texaco"]]),
        ([("s", "s"), ("s", "t")], [["s"]]),                                    # a self-loop on its own
        ([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "e"), ("e", "d")],
         [["a", "b", "c"], ["d", "e"]]),                                        # two components
        ([("a", "b"), ("b", "a"), ("b", "b")], [["a", "b"], ["b"]]),            # the loop inside a cycle too
    ])
    def test_cycles(self, edges, expected):
        assert snapshot.cycles([{"a": a, "b": b} for a, b in edges]) == expected

    def test_a_long_chain_does_not_recurse(self):
        rows = [{"a": f"n{i}", "b": f"n{i + 1}"} for i in range(5000)] + [{"a": "n5000", "b": "n0"}]
        assert len(snapshot.cycles(rows)[0]) == 5001


class TestChecksDiff:
    ENT = [["lei:S", "SIRIUS XM", "gleif", None, "S", None], ["u-1", "Chubb Seguros Chile", "sec-edgar", None, None, None],
           ["sec:T", "Texaco", "sec-edgar", "T", None, None], ["sec:C", "CHEVRON", "sec-edgar", "C", None, None]]

    def _s(self, over, cyc, ent=None):
        return {"entities": ent or self.ENT, "persons": [], "over_100": over, "cycles": cyc}

    def test_a_uuid_issuer_re_minted_is_the_same_issuer(self, capsys):
        ent2 = [["u-2", "Chubb Seguros Chile", "sec-edgar", None, None, None]] + self.ENT[2:] + self.ENT[:1]
        diff._print_checks(self._s([["u-1", "Chubb Seguros Chile", 198.95, []]], []),
                           self._s([["u-2", "Chubb Seguros Chile", 198.95, []]], [], ent2))
        assert "over 100 %: 1 -> 1; gone 0, new 0" in capsys.readouterr().out

    def test_gone_new_and_changed_are_named(self, capsys):
        diff._print_checks(self._s([["lei:S", "SIRIUS XM", 213.59, []], ["u-1", "Chubb Seguros Chile", 198.95, []]],
                                   [["sec:C", "sec:T"]]),
                           self._s([["lei:S", "SIRIUS XM", 79.0, []]], [["lei:S"]]))
        out = capsys.readouterr().out
        assert "-  198.95  Chubb Seguros Chile\n" in out
        assert "~  213.59 ->   79.00  SIRIUS XM  lei:S" in out
        assert "ownership cycles: 1 -> 1; gone 1, new 1" in out
        assert "- CHEVRON (sec:C) <-> Texaco (sec:T)" in out
        assert "+ self-loop SIRIUS XM (lei:S)" in out

    def test_a_previous_snapshot_without_the_checks_lists_all_as_new(self, capsys):
        diff._print_checks({"entities": [], "persons": []}, self._s([["lei:S", "SIRIUS XM", 213.59, []]], []))
        assert "over 100 %: - -> 1; gone 0, new 1 (no check in the previous snapshot" in capsys.readouterr().out

    def test_a_new_snapshot_without_the_checks_prints_nothing(self, capsys):
        diff._print_checks({}, {"entities": [], "persons": []})
        assert capsys.readouterr().out == ""
