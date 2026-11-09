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
