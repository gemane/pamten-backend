"""Unit tests for GLEIF RR-CDF relationship parsing (DB not involved).

End-to-end write (edge with direct_or_indirect) is covered against a real
ArcadeDB in tests/integration/test_gleif_rr_it.py."""

import json
import zipfile

from app.scraper.gleif_rr import _family_of, _node_lei, _rr_edge, _v, import_rr_cdf


def _rec(rtype, child, parent, status="ACTIVE", child_type="LEI", parent_type="LEI"):
    return {"RelationshipRecord": {"Relationship": {
        "StartNode": {"NodeID": {"$": child}, "NodeIDType": {"$": child_type}},
        "EndNode":   {"NodeID": {"$": parent}, "NodeIDType": {"$": parent_type}},
        "RelationshipType":   {"$": rtype},
        "RelationshipStatus": {"$": status},
    }}}


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
            ("PARENT", "CHILD", "direct")

    def test_ultimate_is_indirect(self):
        assert _rr_edge(_rec("IS_ULTIMATELY_CONSOLIDATED_BY", "CHILD", "PARENT")) == \
            ("PARENT", "CHILD", "indirect")

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
