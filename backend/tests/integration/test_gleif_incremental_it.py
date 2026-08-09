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


def _rel(child, parent, status="ACTIVE", end_date=None,
         rtype="IS_DIRECTLY_CONSOLIDATED_BY", start_date=None):
    rel = {
        "StartNode": {"NodeID": _w(child),  "NodeIDType": _w("LEI")},
        "EndNode":   {"NodeID": _w(parent), "NodeIDType": _w("LEI")},
        "RelationshipType":   _w(rtype),
        "RelationshipStatus": _w(status),
    }
    if end_date or start_date:
        period = {"PeriodType": _w("RELATIONSHIP_PERIOD")}
        if start_date:
            period["StartDate"] = _w(start_date)
        if end_date:
            period["EndDate"] = _w(end_date)
        rel["RelationshipPeriods"] = {"RelationshipPeriod": period}
    return {"RelationshipRecord": {"Relationship": rel}}


ULTIMATE = "IS_ULTIMATELY_CONSOLIDATED_BY"


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


# ── The delta must preserve the full import's one-edge-per-pair fold ──────────
#
# The full importer folds a pair GLEIF states both ways into a single edge. The
# delta used to look its edges up **by marker**, so an ultimate-parent record for
# a folded pair matched nothing and created a second, parallel edge — quietly
# undoing the fold a few thousand pairs per night, which is worse than never
# having folded at all.

def _edges(it_db):
    return it_db.run_command(
        "MATCH (a:Entity {id:$p})-[r:OWNS]->(b:Entity {id:$c}) "
        "RETURN r.direct_or_indirect AS marker, r.also_ultimate AS also, "
        "r.since AS since, r.ultimate_since AS ult, r.until AS until",
        {"p": f"lei:{PARENT}", "c": f"lei:{CHILD}"})


def _apply(tmp_path, name, rels):
    from app.scraper.gleif_incremental import import_rr_delta
    return import_rr_delta(_zip(tmp_path, name, "relations", rels), "gleif-src", 92)


def test_an_ultimate_record_folds_into_an_existing_direct_edge(it_db, tmp_path):
    first = _apply(tmp_path, "a.json.zip", [_rel(CHILD, PARENT, start_date="2015-01-01")])
    assert first["created"] == 1

    second = _apply(tmp_path, "b.json.zip", [_rel(CHILD, PARENT, rtype=ULTIMATE)])
    assert second["folded"] == 1 and second["created"] == 0

    rows = _edges(it_db)
    assert len(rows) == 1, "a second parallel edge would undo the import-side fold"
    assert rows[0]["marker"] == "direct" and rows[0]["also"] is True


def test_a_direct_record_takes_over_an_existing_ultimate_edge(it_db, tmp_path):
    """Arriving in the other order must reach the same state — the direct claim is
    the more specific one, so it owns the edge."""
    _apply(tmp_path, "a.json.zip", [_rel(CHILD, PARENT, rtype=ULTIMATE, start_date="2018-06-01")])
    res = _apply(tmp_path, "b.json.zip", [_rel(CHILD, PARENT, start_date="2015-01-01")])
    assert res["folded"] == 1

    rows = _edges(it_db)
    assert len(rows) == 1
    assert rows[0]["marker"] == "direct" and rows[0]["also"] is True
    assert rows[0]["since"] == "2015-01-01"     # the direct period
    assert rows[0]["ult"] == "2018-06-01"       # the ultimate one, preserved


def test_both_records_in_one_delta_produce_one_edge(it_db, tmp_path):
    res = _apply(tmp_path, "a.json.zip", [
        _rel(CHILD, PARENT, start_date="2015-01-01"),
        _rel(CHILD, PARENT, rtype=ULTIMATE),
    ])
    assert res["created"] == 1 and res["folded"] == 1
    assert len(_edges(it_db)) == 1


def test_folding_is_idempotent(it_db, tmp_path):
    rels = [_rel(CHILD, PARENT, start_date="2015-01-01"), _rel(CHILD, PARENT, rtype=ULTIMATE)]
    _apply(tmp_path, "a.json.zip", rels)
    _apply(tmp_path, "b.json.zip", rels)
    assert len(_edges(it_db)) == 1


def test_retiring_the_ultimate_relationship_keeps_the_direct_holding(it_db, tmp_path):
    """Stamping `until` here would delete a holding GLEIF still asserts."""
    _apply(tmp_path, "a.json.zip", [
        _rel(CHILD, PARENT, start_date="2015-01-01"), _rel(CHILD, PARENT, rtype=ULTIMATE)])

    res = _apply(tmp_path, "b.json.zip",
                 [_rel(CHILD, PARENT, rtype=ULTIMATE, status="INACTIVE", end_date="2024-05-01")])
    assert res["closed"] == 1

    rows = _edges(it_db)
    assert len(rows) == 1
    assert rows[0]["until"] is None, "the direct holding is still live"
    assert rows[0]["marker"] == "direct"
    assert rows[0]["also"] is None, "but the parent is no longer claimed as the top"


def test_retiring_the_direct_relationship_leaves_the_ultimate_one(it_db, tmp_path):
    _apply(tmp_path, "a.json.zip", [
        _rel(CHILD, PARENT, start_date="2015-01-01"),
        _rel(CHILD, PARENT, rtype=ULTIMATE, start_date="2018-06-01")])

    res = _apply(tmp_path, "b.json.zip",
                 [_rel(CHILD, PARENT, status="INACTIVE", end_date="2024-05-01")])
    assert res["closed"] == 1

    rows = _edges(it_db)
    assert len(rows) == 1
    assert rows[0]["until"] is None
    assert rows[0]["marker"] == "indirect"          # what is left is the ultimate link
    assert rows[0]["since"] == "2018-06-01"         # with its own period restored


def test_an_unfolded_edge_still_closes_normally(it_db, tmp_path):
    """The ordinary single-relationship case must be untouched by all of the above."""
    _apply(tmp_path, "a.json.zip", [_rel(CHILD, PARENT)])
    res = _apply(tmp_path, "b.json.zip",
                 [_rel(CHILD, PARENT, status="INACTIVE", end_date="2024-05-01")])
    assert res["closed"] == 1
    assert _edges(it_db)[0]["until"] == "2024-05-01"


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


@pytest.fixture
def _gleif_enabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_BODS_GLEIF_ENABLED", True)


def test_update_refused_without_any_load(it_db, _gleif_enabled):
    """The incremental refuses to run until a load has baselined the graph."""
    from app.scraper.gleif_incremental import full_load_present, mark_full_load_done
    from app.scraper.runner import run_gleif_update

    assert full_load_present() is False
    # refuses before fetching anything — no baseline
    with pytest.raises(RuntimeError, match="No GLEIF load found"):
        run_gleif_update(interval="LastDay")

    mark_full_load_done()
    assert full_load_present() is True


# ── A subset load must not enable the nightly delta ───────────────────────────
#
# A delta carries every record GLEIF changed worldwide. Applied to a curated test
# database it does not refresh it — it imports the rest of the world into it. One
# night added 226,902 entity records and 18,720 edges to a 488-entity subset,
# because the subset import stamped the baseline marker exactly as a full load does.

def test_a_subset_load_does_not_satisfy_the_precondition(it_db):
    from app.scraper.gleif_incremental import full_load_present, load_scope, mark_full_load_done

    mark_full_load_done("subset")
    assert load_scope() == "subset"
    assert full_load_present() is False


def test_a_full_load_does(it_db):
    from app.scraper.gleif_incremental import full_load_present, mark_full_load_done

    mark_full_load_done("full")
    assert full_load_present() is True


def test_a_marker_predating_the_scope_field_is_treated_as_a_subset(it_db):
    """Rows the importer wrote when it stamped unconditionally carry no scope, so
    they cannot be trusted to be full. Guessing 'full' is what caused the flood."""
    from app.db.arcadedb import run_sql
    from app.scraper.gleif_incremental import full_load_present, load_scope

    run_sql("UPDATE ImportState SET key = 'gleif-full-load', last_run_at = '2026-01-01T00:00:00' "
            "UPSERT WHERE key = 'gleif-full-load'")
    assert load_scope() == "subset"
    assert full_load_present() is False


@pytest.mark.parametrize("kwargs,expected", [
    ({},                            "full"),
    ({"only_leis": {"SOMELEI"}},    "subset"),   # test-import.sh --only-file
    ({"limit": 1000},               "subset"),
    ({"filter_jurisdiction": "GB"}, "subset"),
])
def test_only_a_complete_pass_stamps_a_full_baseline(it_db, _gleif_enabled, monkeypatch,
                                                     kwargs, expected):
    """The bug itself, at its source. Narrowing the import by ANY of these leaves a
    partial entity baseline, but the importer stamped 'full' regardless — which is
    what re-enabled the nightly delta against a 488-entity curated database."""
    from app.scraper import gleif_lei_cdf, runner
    from app.scraper.gleif_incremental import load_scope

    monkeypatch.setattr(gleif_lei_cdf, "import_lei_cdf_entities",
                        lambda **k: {"entities": 0, "edges": 0})
    runner.run_import_gleif_lei_cdf("/nonexistent.zip", **kwargs)

    assert load_scope() == expected


def test_a_subset_baseline_skips_rather_than_fails(it_db, _gleif_enabled):
    """Sitting on a curated subset for weeks is legitimate, so the nightly run must
    not go red every night — that trains everyone to ignore the log."""
    from app.scraper.gleif_incremental import mark_full_load_done
    from app.scraper.runner import run_gleif_update

    mark_full_load_done("subset")
    result = run_gleif_update(interval="LastDay")     # no fetch, no raise

    assert result["status"] == "skipped"
    assert "curated subset" in result["reason"]

    runs = it_db.run_command(
        "MATCH (r:ScrapeRun {source:'gleif-update'}) RETURN r.status AS status, r.error AS note")
    assert [r["status"] for r in runs] == ["skipped"]
    assert "curated subset" in runs[0]["note"]
