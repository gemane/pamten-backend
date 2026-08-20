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
import os
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


# ── only-existing: refresh what is here, ignore the rest of the world ─────────
#
# A curated database still wants its delta — that is how it stays current, and how
# the delta path itself gets exercised. What it must not do is import every company
# GLEIF changed worldwide.

IN_DB   = "INDBLEI0000000000001"
OUTSIDE = "OUTSIDELEI0000000002"


def _delta_zips(tmp_path, entities, relations):
    return (_zip(tmp_path, "lei-d.json.zip", "records", entities),
            _zip(tmp_path, "rr-d.json.zip", "relations", relations))


def test_only_existing_refreshes_a_company_that_is_here(it_db, tmp_path):
    from app.scraper.gleif_incremental import import_lei_cdf_delta

    it_db.run_command(f"CREATE (:Entity {{id:'lei:{IN_DB}', lei_id:'{IN_DB}', name:'Old Name'}})")
    lei_zip = _zip(tmp_path, "lei.json.zip", "records", [_entity(IN_DB, "New Name")])

    res = import_lei_cdf_delta(lei_zip, "gleif-src", 92, only_existing=True)

    assert res["updated"] == 1 and res["not_here"] == 0
    name = it_db.run_command(
        f"MATCH (e:Entity {{id:'lei:{IN_DB}'}}) RETURN e.name AS n")[0]["n"]
    assert name == "New Name", "the companies we DO hold must still be refreshed"


def test_only_existing_does_not_import_the_rest_of_the_world(it_db, tmp_path):
    """The 226,902 records. Every one of them was a company not in this database."""
    from app.scraper.gleif_incremental import import_lei_cdf_delta

    it_db.run_command(f"CREATE (:Entity {{id:'lei:{IN_DB}', lei_id:'{IN_DB}', name:'Here'}})")
    lei_zip = _zip(tmp_path, "lei.json.zip", "records",
                   [_entity(IN_DB, "Here"), _entity(OUTSIDE, "Somewhere Else")])

    res = import_lei_cdf_delta(lei_zip, "gleif-src", 92, only_existing=True)

    assert res["updated"] == 1 and res["not_here"] == 1
    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"] == 1


def test_without_the_flag_the_whole_delta_still_applies(it_db, tmp_path):
    """A full baseline must keep growing with GLEIF — the mode is opt-in."""
    from app.scraper.gleif_incremental import import_lei_cdf_delta

    lei_zip = _zip(tmp_path, "lei.json.zip", "records",
                   [_entity(IN_DB, "A"), _entity(OUTSIDE, "B")])
    import_lei_cdf_delta(lei_zip, "gleif-src", 92)
    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"] == 2


def test_only_existing_still_records_a_merger_between_two_known_companies(it_db, tmp_path):
    """Succession pairs are bare LEIs while the guest list holds node ids, so a
    missing `lei:` prefix here silently drops every merger instead of leaking."""
    from app.scraper.gleif_incremental import import_lei_cdf_delta

    for lei in (OLD, NEW):
        it_db.run_command(f"CREATE (:Entity {{id:'lei:{lei}', lei_id:'{lei}'}})")
    lei_zip = _zip(tmp_path, "lei.json.zip", "records",
                   [_entity(OLD, "Old Co", reg="MERGED", successor=NEW), _entity(NEW, "Survivor")])

    res = import_lei_cdf_delta(lei_zip, "gleif-src", 92, only_existing=True)

    assert res["succession"] == 1
    assert it_db.run_command(
        "MATCH (:Entity)-[r:SUCCEEDED_BY]->(:Entity) RETURN count(r) AS c")[0]["c"] == 1


def test_only_existing_skips_a_merger_into_a_company_we_do_not_hold(it_db, tmp_path):
    """Creating the successor would be the same leak by another route."""
    from app.scraper.gleif_incremental import import_lei_cdf_delta

    it_db.run_command(f"CREATE (:Entity {{id:'lei:{OLD}', lei_id:'{OLD}'}})")
    lei_zip = _zip(tmp_path, "lei.json.zip", "records",
                   [_entity(OLD, "Old Co", reg="MERGED", successor=OUTSIDE)])

    res = import_lei_cdf_delta(lei_zip, "gleif-src", 92, only_existing=True)

    assert res["succession"] == 0
    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"] == 1


def test_only_existing_keeps_a_relationship_between_two_known_companies(it_db, tmp_path):
    from app.scraper.gleif_incremental import import_rr_delta

    for lei in (PARENT, CHILD):
        it_db.run_command(f"CREATE (:Entity {{id:'lei:{lei}', lei_id:'{lei}'}})")
    rr_zip = _zip(tmp_path, "rr.json.zip", "relations", [_rel(CHILD, PARENT)])

    res = import_rr_delta(rr_zip, "gleif-src", 92, only_existing=True)

    assert res["created"] == 1 and res["not_here"] == 0


def test_only_existing_drags_in_neither_endpoint_of_an_unknown_relationship(it_db, tmp_path):
    """The RR importer creates its endpoint nodes, so this is the bigger leak: one
    relationship between two unknown companies would import both of them."""
    from app.scraper.gleif_incremental import import_rr_delta

    it_db.run_command(f"CREATE (:Entity {{id:'lei:{PARENT}', lei_id:'{PARENT}'}})")
    rr_zip = _zip(tmp_path, "rr.json.zip", "relations", [
        _rel(CHILD, PARENT),          # child is NOT here -> skip, don't create it
        _rel(OUTSIDE, IN_DB),         # neither end is here
    ])

    res = import_rr_delta(rr_zip, "gleif-src", 92, only_existing=True)

    assert res["created"] == 0 and res["not_here"] == 2
    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"] == 1
    assert it_db.run_command("MATCH ()-[r:OWNS]->() RETURN count(r) AS c")[0]["c"] == 0


def test_a_subset_baseline_selects_only_existing_by_itself(it_db, _gleif_enabled,
                                                           tmp_path, monkeypatch):
    """The whole point: the nightly cron keeps running against a curated database
    and exercising the delta path, without burying it."""
    from app.scraper import runner
    from app.scraper.gleif_incremental import mark_full_load_done

    it_db.run_command(f"CREATE (:Entity {{id:'lei:{IN_DB}', lei_id:'{IN_DB}', name:'Old'}})")
    mark_full_load_done("subset")
    lei_zip, rr_zip = _delta_zips(tmp_path, [_entity(IN_DB, "Fresh"),
                                             _entity(OUTSIDE, "Not Ours")], [])

    result = runner.run_gleif_update(interval="LastDay", lei_file=lei_zip, rr_file=rr_zip)

    assert result["status"] == "ok"                     # it RAN, it did not skip
    assert result["lei_cdf"]["updated"] == 1            # ours refreshed
    assert result["lei_cdf"]["not_here"] == 1           # the world ignored
    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"] == 1
    assert it_db.run_command(
        f"MATCH (e:Entity {{id:'lei:{IN_DB}'}}) RETURN e.name AS n")[0]["n"] == "Fresh"


def test_a_full_baseline_applies_the_whole_delta(it_db, _gleif_enabled, tmp_path):
    from app.scraper import runner
    from app.scraper.gleif_incremental import mark_full_load_done

    mark_full_load_done("full")
    lei_zip, rr_zip = _delta_zips(tmp_path, [_entity(IN_DB, "A"), _entity(OUTSIDE, "B")], [])

    runner.run_gleif_update(interval="LastDay", lei_file=lei_zip, rr_file=rr_zip)

    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"] == 2


def test_the_flag_overrides_the_baseline_either_way(it_db, _gleif_enabled, tmp_path):
    from app.scraper import runner
    from app.scraper.gleif_incremental import mark_full_load_done

    mark_full_load_done("full")                      # baseline says "apply everything"
    lei_zip, rr_zip = _delta_zips(tmp_path, [_entity(OUTSIDE, "B")], [])

    runner.run_gleif_update(interval="LastDay", lei_file=lei_zip, rr_file=rr_zip,
                            only_existing=True)      # ...but the caller says otherwise

    assert it_db.run_command("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"] == 0


def test_the_fetched_deltas_survive_the_apply_and_then_go(it_db, _gleif_enabled,
                                                          tmp_path, monkeypatch):
    """The download's temp directory must outlive the apply and not outlive the run.

    Both halves matter and they pull against each other. The nightly update leaked
    one `gleif-delta-*` directory per run for months — 135 MB of them in a /tmp that
    survives reboots. The obvious fix, wrapping the download in a `with` where it
    happens, deletes the files *before* the apply reads them: the download sits in
    an `if` and the apply is below it. Hence an ExitStack held open across the whole
    run, and hence this test, which fails on either mistake.
    """
    from app.scraper import runner
    from app.scraper.gleif_incremental import mark_full_load_done

    it_db.run_command(f"CREATE (:Entity {{id:'lei:{IN_DB}', lei_id:'{IN_DB}', name:'Old'}})")
    mark_full_load_done("full")
    lei_zip, rr_zip = _delta_zips(tmp_path, [_entity(IN_DB, "Fresh")], [])

    # Stand in for the publishes API and the download: hand back copies of the local
    # zips inside a temp dir, exactly as the real fetch would.
    seen: dict = {}

    def fake_download(publish, interval, dest_dir=None):
        import shutil as sh
        seen["dir"] = dest_dir
        return {"lei2": sh.copy(lei_zip, dest_dir), "rr": sh.copy(rr_zip, dest_dir)}

    monkeypatch.setattr("app.scraper.gleif_incremental.fetch_publish_metadata",
                        lambda: {"publish_date": "2026-08-20 16:00:00"})
    monkeypatch.setattr("app.scraper.gleif_incremental.download_deltas", fake_download)

    result = runner.run_gleif_update(interval="LastDay")

    # It read them — so they were still there when the apply ran.
    assert result["status"] == "ok" and result["lei_cdf"]["updated"] == 1
    assert it_db.run_command(
        f"MATCH (e:Entity {{id:'lei:{IN_DB}'}}) RETURN e.name AS n")[0]["n"] == "Fresh"
    # …and they are gone now.
    assert seen["dir"] and not os.path.exists(seen["dir"]), "the temp directory leaked"


def test_local_delta_files_are_never_deleted(it_db, _gleif_enabled, tmp_path):
    """Passing --lei-file/--rr-file means the files are the operator's, sitting in
    their own directory. Cleaning those up would delete a human's data."""
    from app.scraper import runner
    from app.scraper.gleif_incremental import mark_full_load_done

    it_db.run_command(f"CREATE (:Entity {{id:'lei:{IN_DB}', lei_id:'{IN_DB}', name:'Old'}})")
    mark_full_load_done("full")
    lei_zip, rr_zip = _delta_zips(tmp_path, [_entity(IN_DB, "Fresh")], [])

    runner.run_gleif_update(interval="LastDay", lei_file=lei_zip, rr_file=rr_zip)

    assert os.path.exists(lei_zip) and os.path.exists(rr_zip)
