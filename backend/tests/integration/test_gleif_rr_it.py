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


def _rec(rtype, child, parent, start=None, rr_status=None, updated=None):
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
    rec = {"RelationshipRecord": {"Relationship": rel}}
    registration = {}
    if rr_status:
        registration["RegistrationStatus"] = _w(rr_status)
    if updated:
        registration["LastUpdateDate"] = _w(updated)
    if registration:
        rec["RelationshipRecord"]["Registration"] = registration
    return rec


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


def _same_parent_both_ways_zip(tmp_path, ultimate_start=None):
    """One parent stated as BOTH the direct and the ultimate consolidator of one
    child — 88,839 of the full golden copy's 257,651 consolidation edges."""
    payload = {"relations": [
        _rec("IS_DIRECTLY_CONSOLIDATED_BY",   "CHILDLEI000000000001", "PARENTLEI0000000002",
             start="2015-03-04T00:00:00.000Z"),
        _rec("IS_ULTIMATELY_CONSOLIDATED_BY", "CHILDLEI000000000001", "PARENTLEI0000000002",
             start=ultimate_start),
    ]}
    zpath = tmp_path / "rr-both.json.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rr-golden-copy.json", json.dumps(payload))
    return str(zpath)


def test_a_pair_stated_both_ways_becomes_one_edge(it_db, tmp_path):
    """The duplicate edge is never created, so nothing downstream has to prove it
    redundant: no second owner row in the profile, nothing for the graph to hide."""
    from app.scraper.gleif_rr import import_rr_cdf

    result = import_rr_cdf(_same_parent_both_ways_zip(tmp_path), "gleif-src", 92)
    assert result["records"] == 2
    assert result["edges"] == 1 and result["collapsed"] == 1

    edges = it_db.run_command(
        "MATCH (a:Entity)-[o:OWNS]->(b:Entity) RETURN o.direct_or_indirect AS doi, "
        "o.since AS since, o.also_ultimate AS also")
    assert len(edges) == 1
    assert edges[0]["doi"] == "direct"          # the more specific claim survives
    assert edges[0]["since"] == "2015-03-04"
    assert edges[0]["also"] is True             # but the ultimate assertion is recorded


def test_a_differing_ultimate_period_survives_on_the_edge(it_db, tmp_path):
    """6.9% of folded pairs on the real file state two different relationship
    starts. Deleting the twin instead of folding it would lose 6,138 dates."""
    from app.scraper.gleif_rr import import_rr_cdf

    import_rr_cdf(_same_parent_both_ways_zip(tmp_path, ultimate_start="2018-06-01T00:00:00.000Z"),
                  "gleif-src", 92)

    edges = it_db.run_command(
        "MATCH (a:Entity)-[o:OWNS]->(b:Entity) "
        "RETURN o.since AS since, o.ultimate_since AS ult")
    assert len(edges) == 1
    assert edges[0]["since"] == "2015-03-04"    # the direct relationship period
    assert edges[0]["ult"] == "2018-06-01"      # the ultimate one, not lost


def _zip_of(tmp_path, name, records):
    zpath = tmp_path / name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("rr.json", json.dumps({"relations": records}))
    return str(zpath)


def _edge(it_db, parent, child):
    rows = it_db.run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "RETURN r.stale AS stale, r.source_date AS sd, r.until AS until, r.source_id AS src",
        {"p": f"lei:{parent}", "c": f"lei:{child}"})
    return rows[0] if rows else None


# ── The record's own registration status ─────────────────────────────────────
#
# GLEIF's LAPSED = "not renewed by its NextRenewalDate": the LOU stopped
# maintaining the record, so its ACTIVE relationship is the last word, not a
# current one (Metaswitch "consolidated by Microsoft", last updated 2023-06-02,
# sold 2025-03). 62,934 of ~260k active consolidation records on 2026-10-06.

LAPSED_CHILD = "LAPSEDCHILD000000001"
FRESH_CHILD = "FRESHCHILD0000000002"
PARENT = "PARENTLEI00000000003"
ANNULLED_CHILD = "ANNULLEDCHILD0000004"


def test_an_edge_on_a_lapsed_record_is_drawn_stale_and_dated(it_db, tmp_path):
    from app.scraper.gleif_rr import import_rr_cdf

    res = import_rr_cdf(_zip_of(tmp_path, "rr-status.json.zip", [
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", LAPSED_CHILD, PARENT, start="2022-04-11T19:51:10Z",
             rr_status="LAPSED", updated="2023-06-02T00:10:55Z"),
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", FRESH_CHILD, PARENT, rr_status="PUBLISHED",
             updated="2026-09-01T00:00:00Z"),
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", ANNULLED_CHILD, PARENT, rr_status="ANNULLED"),
    ]), "gleif-src", 92)
    assert res["edges"] == 2 and res["unmaintained"] == 1 and res["annulled"] == 1

    lapsed = _edge(it_db, PARENT, LAPSED_CHILD)
    assert lapsed["stale"] is True
    assert lapsed["sd"] == "2023-06-02"          # the evidence date time travel dims after
    assert lapsed["until"] is None               # unmaintained, not ended
    fresh = _edge(it_db, PARENT, FRESH_CHILD)
    assert not fresh["stale"]
    assert _edge(it_db, PARENT, ANNULLED_CHILD) is None   # no statement, no edge


def test_the_heal_marks_an_edge_drawn_before_the_status_was_read(it_db, tmp_path):
    """A database loaded by the old importer: the same pairs, no mark. One pass
    over the current golden copy stamps them; a pair another source holds and
    a pair with a maintained record are left alone; a dry run writes nothing."""
    from app.scraper.gleif_rr import heal_registration_status, import_rr_cdf

    # the old world: records without a status field, nothing stale
    import_rr_cdf(_zip_of(tmp_path, "rr-old.json.zip", [
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", LAPSED_CHILD, PARENT),
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", FRESH_CHILD, PARENT),
    ]), "gleif-src", 92)
    assert not _edge(it_db, PARENT, LAPSED_CHILD)["stale"]
    # a pair another source drew for the same parent
    it_db.run_command(
        "MATCH (a:Entity {id:$p}) CREATE (o:Entity {id:$c, name:'Other Co', lei_id:$lei}) "
        "CREATE (a)-[:OWNS {source_id:'sec-src', filing_type:'EX-21', ownership_type:'controlling'}]->(o)",
        {"p": f"lei:{PARENT}", "c": "lei:OTHERSRC00000000005", "lei": "OTHERSRC00000000005"})

    today = _zip_of(tmp_path, "rr-today.json.zip", [
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", LAPSED_CHILD, PARENT, rr_status="LAPSED"),
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", FRESH_CHILD, PARENT, rr_status="PUBLISHED"),
        _rec("IS_DIRECTLY_CONSOLIDATED_BY", "OTHERSRC00000000005", PARENT, rr_status="LAPSED"),
    ])
    dry = heal_registration_status(today, "gleif-src", dry_run=True)
    assert dry["unmaintained"] == 2 and dry["marked"] == 0
    assert not _edge(it_db, PARENT, LAPSED_CHILD)["stale"]

    res = heal_registration_status(today, "gleif-src")
    assert res["unmaintained"] == 2 and res["marked"] == 1 and res["not_here"] == 1
    assert _edge(it_db, PARENT, LAPSED_CHILD)["stale"] is True
    assert not _edge(it_db, PARENT, FRESH_CHILD)["stale"]
    assert not _edge(it_db, PARENT, "OTHERSRC00000000005")["stale"]   # SEC's edge, SEC's mark
    # idempotent
    again = heal_registration_status(today, "gleif-src")
    assert again["marked"] == 1 and _edge(it_db, PARENT, LAPSED_CHILD)["stale"] is True
