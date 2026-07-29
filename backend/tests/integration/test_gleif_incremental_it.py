"""
Real-ArcadeDB test for the GLEIF **delta** importers (retirement-aware refresh).

Builds tiny synthetic LEI-CDF + RR delta zips and asserts the delta behaviour the
bulk importers don't have:
  (a) an ACTIVE consolidation adds an OWNS edge, a MERGED entity adds SUCCEEDED_BY,
      and an INACTIVE entity is marked (active=false), never deleted;
  (b) re-applying the same delta produces no duplicate edges (idempotent);
  (c) flipping a relationship to INACTIVE closes its OWNS edge (`until` stamped).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration


def _w(v):
    return {"$": v}


def _entity(lei, name, status="ACTIVE", reg="ISSUED", successor=None):
    rec = {
        "LEI": _w(lei),
        "Entity": {
            "LegalName": _w(name),
            "LegalAddress": {"Country": _w("US")},
            "EntityStatus": _w(status),
        },
        "Registration": {"RegistrationStatus": _w(reg)},
    }
    if successor:
        rec["Entity"]["SuccessorEntity"] = [{"SuccessorLEI": _w(successor)}]
    return rec


def _rel(child, parent, status="ACTIVE", end_date=None):
    rel = {
        "StartNode": {"NodeID": _w(child),  "NodeIDType": _w("LEI")},
        "EndNode":   {"NodeID": _w(parent), "NodeIDType": _w("LEI")},
        "RelationshipType":   _w("IS_DIRECTLY_CONSOLIDATED_BY"),
        "RelationshipStatus": _w(status),
    }
    if end_date:
        rel["RelationshipPeriods"] = {"RelationshipPeriod":
            {"PeriodType": _w("RELATIONSHIP_PERIOD"), "EndDate": _w(end_date)}}
    return {"RelationshipRecord": {"Relationship": rel}}


def _zip(tmp_path, name, key, items):
    zpath = tmp_path / name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name.replace(".zip", ""), json.dumps({key: items}))
    return str(zpath)


PARENT = "PARENTLEI00000000001"
CHILD  = "CHILDLEI000000000002"
GONE   = "DISSOLVEDLEI00000003"      # INACTIVE / LAPSED entity
OLD    = "MERGEDLEI0000000004"       # predecessor
NEW    = "SURVIVORLEI000000005"      # successor


def test_delta_apply_idempotent_and_retirement(it_db, tmp_path):
    from app.scraper.gleif_incremental import import_lei_cdf_delta, import_rr_delta

    lei_zip = _zip(tmp_path, "lei.json.zip", "records", [
        _entity(PARENT, "Parent Co"),
        _entity(CHILD, "Child Co"),
        _entity(GONE, "Dissolved Co", status="INACTIVE", reg="LAPSED"),
        _entity(OLD, "Old Co", reg="MERGED", successor=NEW),
        _entity(NEW, "Survivor Co"),
    ])
    rr_zip = _zip(tmp_path, "rr.json.zip", "relations", [_rel(CHILD, PARENT)])

    # (a) first apply
    lei = import_lei_cdf_delta(lei_zip, "gleif-src", 92)
    rr = import_rr_delta(rr_zip, "gleif-src", 92)
    assert lei["updated"] == 5
    assert lei["marked_inactive"] == 1
    assert lei["succession"] == 1
    assert rr["created"] == 1 and rr["closed"] == 0

    def owns_count():
        return it_db.run_command(
            "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) RETURN count(r) AS n",
            {"p": f"lei:{PARENT}", "c": f"lei:{CHILD}"})[0]["n"]

    assert owns_count() == 1
    # dissolved entity marked, not deleted
    gone = it_db.run_command("MATCH (e:Entity {id:$id}) RETURN e.active AS active, "
                             "e.gleif_registration_status AS reg",
                             {"id": f"lei:{GONE}"})
    assert gone and gone[0]["active"] is False and gone[0]["reg"] == "LAPSED"
    # succession edge
    succ = it_db.run_command(
        "MATCH (a:Entity {id:$o})-[r:SUCCEEDED_BY]->(b:Entity {id:$n}) RETURN count(r) AS n",
        {"o": f"lei:{OLD}", "n": f"lei:{NEW}"})[0]["n"]
    assert succ == 1

    # (b) re-apply the same delta → no duplicate edges
    lei2 = import_lei_cdf_delta(lei_zip, "gleif-src", 92)
    rr2 = import_rr_delta(rr_zip, "gleif-src", 92)
    assert rr2["created"] == 0 and rr2["updated"] == 1     # refreshed, not duplicated
    assert lei2["succession"] == 0                          # SUCCEEDED_BY already there
    assert owns_count() == 1

    # (c) relationship goes INACTIVE → its OWNS edge is closed (until stamped)
    closed_zip = _zip(tmp_path, "rr2.json.zip", "relations",
                      [_rel(CHILD, PARENT, status="INACTIVE", end_date="2024-05-01")])
    rr3 = import_rr_delta(closed_zip, "gleif-src", 92)
    assert rr3["closed"] == 1
    until = it_db.run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) RETURN r.until AS until",
        {"p": f"lei:{PARENT}", "c": f"lei:{CHILD}"})[0]["until"]
    assert until == "2024-05-01"
    assert owns_count() == 1                                # closed, not removed


def test_publish_checkpoint_roundtrip(it_db):
    """The gap-aware checkpoint (ImportState) persists across runs so the next run
    can size its catch-up window."""
    from app.scraper.gleif_incremental import (
        choose_catchup_interval,
        read_last_publish,
        write_last_publish,
    )

    assert read_last_publish() is None                     # nothing applied yet
    write_last_publish("2026-07-01 16:00:00")
    assert read_last_publish() == "2026-07-01 16:00:00"

    # re-write (idempotent UPSERT on the key — one row, updated in place)
    write_last_publish("2026-07-20 16:00:00")
    assert read_last_publish() == "2026-07-20 16:00:00"
    assert it_db.run_command("MATCH (s:ImportState) RETURN count(s) AS n")[0]["n"] == 1

    # a 3-day gap from that checkpoint → LastWeek covers it
    assert choose_catchup_interval(read_last_publish(), "2026-07-23 16:00:00") == "LastWeek"
