"""
Real-ArcadeDB test for the GLEIF RR-CDF importer: builds a tiny synthetic
golden-copy zip (the {"$": …} wrapping + relations array) with a direct and an
ultimate parent for the same child, runs the importer, and asserts the
(parent)-[:OWNS]->(child) edges land with the direct/indirect marker.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration


def _w(v):
    return {"$": v}


def _rec(rtype, child, parent, start=None):
    rel = {
        "StartNode": {"NodeID": _w(child),  "NodeIDType": _w("LEI")},
        "EndNode":   {"NodeID": _w(parent), "NodeIDType": _w("LEI")},
        "RelationshipType":   _w(rtype),
        "RelationshipStatus": _w("ACTIVE"),
    }
    if start is not None:
        rel["RelationshipPeriods"] = {"RelationshipPeriod": [
            {"PeriodType": _w("RELATIONSHIP_PERIOD"), "StartDate": _w(start)},
        ]}
    return {"RelationshipRecord": {"Relationship": rel}}


def _rr_zip(tmp_path):
    # Child directly consolidated by DIRECTP (with a relationship start date),
    # ultimately by ULTP, plus a fund relationship that must be skipped.
    payload = {"relations": [
        _rec("IS_DIRECTLY_CONSOLIDATED_BY",   "CHILDLEI000000000001", "DIRECTPLEI0000000002",
             start="2015-03-04T00:00:00.000Z"),
        _rec("IS_ULTIMATELY_CONSOLIDATED_BY", "CHILDLEI000000000001", "ULTPLEI00000000000003"),
        _rec("IS_FUND-MANAGED_BY",            "FUNDLEI0000000000004", "MGRLEI00000000000005"),
    ]}
    zpath = tmp_path / "rr.json.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rr-golden-copy.json", json.dumps(payload))
    return str(zpath)


def test_imports_direct_and_indirect_parent_edges(it_db, tmp_path):
    from app.scraper.gleif_rr import import_rr_cdf

    result = import_rr_cdf(_rr_zip(tmp_path), "gleif-src", 92)
    assert result["records"] == 3
    assert result["direct"] == 1 and result["indirect"] == 1
    assert result["skipped"] == 1           # the fund relationship
    assert result["nodes"] == 3             # child + direct parent + ultimate parent

    edges = it_db.run_command(
        "MATCH (a:Entity)-[o:OWNS]->(b:Entity) "
        "RETURN a.lei_id AS parent, b.lei_id AS child, "
        "o.direct_or_indirect AS doi, o.ownership_type AS type, o.since AS since")
    got = {(e["parent"], e["child"], e["doi"]) for e in edges}
    assert ("DIRECTPLEI0000000002", "CHILDLEI000000000001", "direct") in got
    assert ("ULTPLEI00000000000003", "CHILDLEI000000000001", "indirect") in got
    assert all(e["type"] == "controlling" for e in edges)
    # the fund relationship produced no OWNS edge
    assert len(edges) == 2
    # the relationship period's start date lands as `since` (date only) for the timeline
    since_by_parent = {e["parent"]: e["since"] for e in edges}
    assert since_by_parent["DIRECTPLEI0000000002"] == "2015-03-04"
    assert since_by_parent["ULTPLEI00000000000003"] is None   # no period → no since
