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


def _fake_cypher(cy):
    if "[r:OWNS]" in cy:
        return [{"a": DIXON, "b": BRK, "s": "sec-edgar", "u": None, "f": "13F"},
                {"a": BLK, "b": BRK, "s": "sec-edgar", "u": "2026-03-13", "f": "13G/A"}]
    return []


class TestSnapshot:
    def test_every_owns_row_carries_its_filing_type(self):
        snap = snapshot.take(_fake_sql, _fake_cypher)
        assert snap["owns_edges"] == [[BLK, BRK, "sec-edgar", "2026-03-13", "13G/A"],
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


def _snap(entities, edges):
    return {"entities": entities, "persons": [], "owns_edges": edges}


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


@pytest.mark.parametrize("value, expected", [("13F", True), ("13F-HR", True), ("13G/A", False), (None, False)])
def test_is_13f(value, expected):
    assert diff.is_13f(value) is expected
