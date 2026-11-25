"""
Scraper runner — the bulk imports and their deltas: GLEIF (LEI-CDF, relationship
records, successions, reporting exceptions, the daily update) and Companies
House (PSC snapshot and update, company data).

What this module needs of the runner — its own helpers, and the app-level names
it imports (the database, the settings, the write layer) — it reaches through
`runner` (`runner.db`, `runner._upsert_role`), not through imports of its own:
a test that replaces one of them on the runner then covers every pipeline, as
it did while this code was part of runner.py (until 2026-11-25).
"""
import os
import zipfile
from contextlib import ExitStack

from app.scraper import runner


# ── BODS (GLEIF / UK PSC) helpers ─────────────────────────────────────────────

def _post_bods_import() -> dict:
    """Housekeeping every BODS import needs, so it isn't a separate manual step:
    flag nominee/custodian entities the load added, and collapse duplicate active
    OWNS edges (CREATE EDGE isn't idempotent, so a re-import doubles them).
    Best-effort — a failure here must not fail the import."""
    from app.scraper.maintenance import flag_nominee_entities, deduplicate_owns_edges
    out: dict = {}
    try:
        out["nominees"] = flag_nominee_entities()
    except Exception as exc:  # noqa: BLE001
        runner.log.warning("post-import flag-nominees failed: %s", exc)
    try:
        out["edge_dedup"] = deduplicate_owns_edges()
    except Exception as exc:  # noqa: BLE001
        runner.log.warning("post-import edge dedup failed: %s", exc)
    return out


# ── GLEIF public entry points (golden copy) ───────────────────────────────────
# The OpenOwnership GLEIF BODS import was retired — GLEIF entities/relationships/
# succession now come from the current golden copy (LEI-CDF + RR-CDF), below.

def run_import_ch_psc(local_file: str, limit: int | None = None,
                      bulk_load: bool = False, batch_size: int = 400,
                      only_companies: set[str] | None = None,
                      digest_out: str | None = None) -> dict:
    """
    Import a Companies House PSC snapshot (current UK beneficial ownership, daily)
    — the replacement for the frozen OpenOwnership UK PSC BODS export. Reuses the
    UK PSC source. Checks SCRAPER_ENABLED.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    from app.scraper.companies_house_psc import import_ch_psc

    source_id = runner._ensure_source(runner.UK_PSC_SOURCE_NAME, runner.UK_PSC_SOURCE_URL, runner.BODS_UK_PSC_CREDIBILITY)
    runner.log.info("CH PSC: importing from %s (limit=%s)", local_file, limit)
    counts = import_ch_psc(
        filepath=local_file,
        source_id=source_id,
        credibility_score=runner.BODS_UK_PSC_CREDIBILITY,
        limit=limit,
        bulk_load=bulk_load,
        batch_size=batch_size,
        only_companies=only_companies,
        digest_out=digest_out,
    )
    # Stamp the baseline marker the incremental refresh rides on. Narrowing by
    # --limit or --only means the graph holds part of the register, so a refresh
    # must run in only-existing mode; recording *which* lets the refusal say why
    # rather than claim PSC was never loaded.
    from app.scraper.ch_psc_incremental import (
        mark_psc_load_done, snapshot_date, snapshot_entry, write_last_snapshot,
    )
    mark_psc_load_done("subset" if (limit or only_companies) else "full")
    # A digest written here IS the baseline for this snapshot, so record which
    # snapshot that was. Without it the first refresh cannot tell how big a gap it
    # is covering — it assumed one day, so a month's legitimate changes would be
    # measured against a single day's allowance — and the staleness guard has
    # nothing to compare against, leaving an older snapshot applyable over a newer
    # baseline. Found by running the real thing: it reported gap_days 1 for 24.
    if digest_out:
        entry = (snapshot_entry(zipfile.ZipFile(local_file))
                 if local_file.lower().endswith(".zip") else os.path.basename(local_file))
        write_last_snapshot(snapshot_date(entry), counts.get("digest_records", 0))
    return {"status": "ok", "source": runner.UK_PSC_SOURCE_NAME, **counts,
            **_post_bods_import()}


def run_ch_psc_update(local_file: str, digest: str | None = None,
                      limit: int | None = None, batch_size: int = 1000,
                      only_existing: bool | None = None, max_churn_pct: float = 5.0,
                      force: bool = False, dry_run: bool = False,
                      rebuild_digest: bool = False) -> dict:
    """
    Apply a Companies House PSC snapshot **incrementally**, by diffing it against
    the digest of the last one applied (see `app/scraper/ch_psc_incremental.py`).

    Companies House publishes no delta feed — only a full snapshot, overwritten
    daily — so the delta is computed locally. A snapshot is a complete state, so
    there is no catch-up window to miss: diffing against a week-old digest simply
    yields a week's changes.

    Deliberately **not scheduled**. It is driven by hand until it has proved itself
    over several real snapshots; `--dry-run` and the churn guard exist to make those
    runs legible.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable.")

    from app.db.schema import ensure_indexes
    from app.scraper import ch_psc_incremental as inc
    from app.scraper.run_log import record_run

    # Idempotent, and it is what creates the OWNS.psc_self_link index the whole
    # write path matches on — a refresh against a database that never had it would
    # full-scan every edge, per batch.
    ensure_indexes()

    digest = digest or inc.default_digest_path(local_file)
    entry = inc.snapshot_entry(zipfile.ZipFile(local_file)) if local_file.lower().endswith(".zip") \
        else os.path.basename(local_file)
    snap_date = inc.snapshot_date(entry)

    if rebuild_digest:
        # Escape hatch: re-establish the baseline digest without touching the graph.
        # Loud, because it silently forfeits whatever changed since the last one.
        runner.log.warning("CH PSC: rebuilding the digest from %s — this FORFEITS any changes "
                    "since the last applied snapshot", entry)
        counts = inc.write_digest(local_file, digest, limit=limit)
        inc.write_last_snapshot(snap_date, counts["records"])
        return {"status": "ok", "source": runner.UK_PSC_SOURCE_NAME, "rebuilt": True, **counts}

    scope = inc.psc_load_scope()
    if scope is None:
        raise RuntimeError(
            "No Companies House PSC load found — the refresh rides on top of a full "
            "snapshot import. Run `manage.py ch-psc --file … --digest-out …` first.")
    if not os.path.exists(digest):
        raise RuntimeError(
            f"No baseline digest at {digest} — it is written by `ch-psc --digest-out`, "
            "or rebuild one with `ch-psc-update --rebuild-digest` (which forfeits a day).")

    source_id = runner._ensure_source(runner.UK_PSC_SOURCE_NAME, runner.UK_PSC_SOURCE_URL, runner.BODS_UK_PSC_CREDIBILITY)
    with record_run("ch-psc-update", snap_date) as run:
        # A subset baseline holds part of the register, so refreshing it whole would
        # not update it — it would drag the rest of the UK in. Same policy as GLEIF.
        only_existing = scope != "full" if only_existing is None else only_existing
        if only_existing:
            run["note"] = ("only-existing mode: refreshing the companies this database "
                           "already holds, ignoring the rest of the register")

        last = inc.read_last_snapshot() or {}
        if last.get("projection_version") not in (None, inc.PROJECTION_VERSION) and not force:
            raise RuntimeError(
                f"the stored digest was built by projection v{last.get('projection_version')}, "
                f"this code is v{inc.PROJECTION_VERSION} — every digest is invalidated by "
                "that change. Rebuild with --rebuild-digest.")
        if last.get("snapshot_date") and snap_date <= last["snapshot_date"] and not force:
            return {"status": "skipped", "source": runner.UK_PSC_SOURCE_NAME,
                    "reason": f"snapshot {snap_date} is not newer than the last applied "
                              f"{last['snapshot_date']}"}

        new_digest = inc.new_digest_tempfile(digest)
        runner.log.info("CH PSC refresh: digesting %s", entry)
        dcounts = inc.write_digest(local_file, new_digest, limit=limit)
        diff = inc.diff_digests(digest, new_digest)
        gap = inc.days_since(last.get("snapshot_date"))
        ok, why = inc.churn_allowed(diff, max_churn_pct, gap)
        summary = {"snapshot_date": snap_date, "added": diff.added, "changed": diff.changed,
                   "vanished": len(diff.vanished), "churn_pct": round(inc.churn_pct(diff), 3),
                   "records": dcounts["records"], "gap_days": gap}
        if not ok and not force:
            os.unlink(new_digest)
            raise RuntimeError(f"CH PSC refresh refused: {why}")
        if dry_run:
            os.unlink(new_digest)
            run["total"] = diff.total
            return {"status": "dry-run", "source": runner.UK_PSC_SOURCE_NAME, **summary}

        applied = inc.apply_diff(local_file, diff, source_id, runner.BODS_UK_PSC_CREDIBILITY,
                                 until_date=snap_date, only_existing=only_existing,
                                 batch_size=batch_size)
        # Only now: a crash before this leaves tomorrow's diff a superset of today's,
        # which is redone idempotently. Rotating first would lose it outright.
        inc.rotate_digest(new_digest, digest)
        inc.write_last_snapshot(snap_date, dcounts["records"])
        run["total"] = diff.total

    return {"status": "ok", "source": runner.UK_PSC_SOURCE_NAME, **summary, **applied}


def run_import_basic_company_data(local_file: str, limit: int | None = None,
                                  bulk_load: bool = False, batch_size: int = 400,
                                  only_companies: set[str] | None = None) -> dict:
    """
    Enrich UK companies — number-keyed (gb-coh:{number}) or holding the number
    under a GLEIF id — with names/addresses/former names/SIC codes from a Companies
    House BasicCompanyData snapshot — the companion to the PSC import, which leaves
    controlled companies un-named. Enrichment only (no
    nodes/edges created), so no _post_bods_import housekeeping is needed. Gated on
    SCRAPER_ENABLED.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    from app.scraper.basic_company_data import import_basic_company_data

    runner.log.info("CH BasicData: importing from %s (limit=%s)", local_file, limit)
    counts = import_basic_company_data(
        filepath=local_file,
        credibility_score=runner.CH_REGISTER_CREDIBILITY,
        limit=limit,
        bulk_load=bulk_load,
        batch_size=batch_size,
        only_companies=only_companies,
    )
    return {"status": "ok", "source": "Companies House Register", **counts}


def run_import_gleif_succession(local_file: str, limit: int | None = None,
                                only_leis: set[str] | None = None,
                                emit_leis_path: str | None = None) -> dict:
    """
    Import GLEIF LEI-CDF succession (MERGED/DUPLICATE/RETIRED → SuccessorLEI) into
    SUCCEEDED_BY edges. Reuses the GLEIF source + flags. `local_file` is a
    pre-downloaded LEI-CDF golden-copy .json/.zip (multi-GB — a local batch job,
    not a URL fetch, so no download path here). Checks SCRAPER_ENABLED.
    `only_leis` restricts to the successions touching the curated subset and
    `emit_leis_path` writes the endpoints it adds, for a naming pass (item 85b).
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    from app.scraper.gleif_succession import import_lei_cdf_succession

    source_id = runner._ensure_source(runner.GLEIF_SOURCE_NAME, runner.GLEIF_SOURCE_URL, runner.BODS_GLEIF_CREDIBILITY)
    runner.log.info("GLEIF succession: importing from %s (limit=%s)", local_file, limit)
    counts = import_lei_cdf_succession(
        filepath=local_file,
        source_id=source_id,
        credibility_score=runner.BODS_GLEIF_CREDIBILITY,
        limit=limit,
        only_leis=only_leis,
        emit_leis_path=emit_leis_path,
    )
    return {"status": "ok", "source": runner.GLEIF_SOURCE_NAME, **counts}


def run_import_gleif_lei_cdf(local_file: str, limit: int | None = None,
                             filter_jurisdiction: str | None = None,
                             bulk_load: bool = False,
                             only_leis: set[str] | None = None) -> dict:
    """
    Import GLEIF entities from the LEI-CDF golden copy (current, authoritative) —
    the replacement for the frozen OpenOwnership GLEIF BODS entity data. Reuses
    the GLEIF source + flags. Checks SCRAPER_ENABLED.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    from app.scraper.gleif_lei_cdf import import_lei_cdf_entities

    source_id = runner._ensure_source(runner.GLEIF_SOURCE_NAME, runner.GLEIF_SOURCE_URL, runner.BODS_GLEIF_CREDIBILITY)
    runner.log.info("GLEIF LEI-CDF entities: importing from %s (limit=%s, jur=%s)",
             local_file, limit, filter_jurisdiction)
    counts = import_lei_cdf_entities(
        filepath=local_file,
        source_id=source_id,
        credibility_score=runner.BODS_GLEIF_CREDIBILITY,
        limit=limit,
        filter_jurisdiction=filter_jurisdiction,
        bulk_load=bulk_load,
        only_leis=only_leis,
    )
    # Stamp the baseline marker so the incremental `gleif-update` knows whether the
    # full load has run (it refuses to apply deltas onto an un-baselined graph).
    # Narrowing the import by ANY of these means the entity baseline is partial, and
    # a delta — which carries every record changed worldwide — would not refresh that
    # subset but bury it. This used to stamp "full" unconditionally, so the curated
    # test import re-enabled the nightly delta against a 488-entity database.
    from app.scraper.gleif_incremental import mark_full_load_done
    mark_full_load_done("subset" if (only_leis or limit or filter_jurisdiction) else "full")
    # The registration-day rule needs the child's LEI registration date AND the
    # edge, whichever import brings the second: the test import loads the
    # families' companies after their relationships, and its RR pass labelled 0.
    from app.scraper.gleif_rr import mark_registration_day
    return {"status": "ok", "source": runner.GLEIF_SOURCE_NAME, **counts,
            "duplicate_names": runner._duplicate_name_summary(),
            "registration_day": mark_registration_day()}


def run_import_gleif_repex(local_file: str, limit: int | None = None) -> dict:
    """
    Import GLEIF reporting exceptions — the published reasons companies give for
    naming no parent (see `app/scraper/gleif_repex.py`). Writes onto entities this
    database already holds and creates none, so it is safe to run against a
    curated subset as well as a full load. Reuses the GLEIF source + flags.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    from app.scraper.gleif_repex import import_repex

    # The source node is ensured for consistency with the other GLEIF imports, even
    # though an exception is a property on an existing entity rather than a new
    # node or edge — the provenance is the entity's, already stamped by LEI-CDF.
    runner._ensure_source(runner.GLEIF_SOURCE_NAME, runner.GLEIF_SOURCE_URL, runner.BODS_GLEIF_CREDIBILITY)
    runner.log.info("GLEIF repex: importing from %s (limit=%s)", local_file, limit)
    counts = import_repex(filepath=local_file, limit=limit)
    return {"status": "ok", "source": runner.GLEIF_SOURCE_NAME, **counts}


def run_import_gleif_rr(local_file: str, limit: int | None = None,
                        only_leis: set[str] | None = None,
                        emit_leis_path: str | None = None) -> dict:
    """
    Import GLEIF RR-CDF (Level 2) direct/ultimate consolidation parents as
    direct/indirect OWNS edges. Reuses the GLEIF source + flags. `local_file` is a
    pre-downloaded RR-CDF golden-copy .json/.zip. Checks SCRAPER_ENABLED.

    `only_leis` restricts to the corporate family of those seed LEIs (test subset);
    `emit_leis_path` writes the family's LEIs for a follow-up entity-naming pass.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    from app.scraper.gleif_rr import import_rr_cdf, mark_registration_day
    from app.scraper.maintenance import deduplicate_owns_edges

    source_id = runner._ensure_source(runner.GLEIF_SOURCE_NAME, runner.GLEIF_SOURCE_URL, runner.BODS_GLEIF_CREDIBILITY)
    runner.log.info("GLEIF RR-CDF: importing from %s (limit=%s)", local_file, limit)
    counts = import_rr_cdf(
        filepath=local_file,
        source_id=source_id,
        credibility_score=runner.BODS_GLEIF_CREDIBILITY,
        limit=limit,
        only_leis=only_leis,
        emit_leis_path=emit_leis_path,
    )
    # RR consolidation edges overlap the GLEIF parent edges from the BODS import,
    # so collapse the duplicates automatically (keeps the direct/indirect-flagged
    # edge) — no separate "remember to dedup" step.
    runner.log.info("GLEIF RR-CDF: deduplicating overlapping OWNS edges")
    dedup = deduplicate_owns_edges()
    # starts that are only the child's LEI registration day: "at least since"
    reg_day = mark_registration_day()
    return {"status": "ok", "source": runner.GLEIF_SOURCE_NAME, **counts, "edge_dedup": dedup,
            "registration_day": reg_day}


def run_gleif_update(interval: str = "auto", lei_file: str | None = None,
                     rr_file: str | None = None, limit: int | None = None,
                     only_existing: bool | None = None,
                     repex_file: str | None = None) -> dict:
    """
    Apply a GLEIF **delta** update on top of the full golden-copy load — the
    retirement-aware daily refresh (see `app/scraper/gleif_incremental.py`). It
    idempotently upserts changed entities + succession edges and upserts/closes
    changed OWNS edges against the live-indexed DB (no `--bulk-load`, no whole-DB
    dedup), and is logged as a `gleif-update` ScrapeRun (visible in GET /scraper/runs).

    `interval="auto"` (the default) is **gap-aware**: it checkpoints the last GLEIF
    publish it applied (an `ImportState` node) and, on each run, picks the smallest
    delta window (LastDay/Week/Month) that still covers the gap since that
    checkpoint — so a few missed daily runs self-heal on the next one. A gap wider
    than ~30 days can't be covered by a delta and raises (run a full reload). Pass an
    explicit `interval` (IntraDay/LastDay/LastWeek/LastMonth) to override, or
    pre-downloaded `lei_file`/`rr_file`/`repex_file` to skip the fetch.
    """
    if not runner.settings.SCRAPER_ENABLED:
        raise PermissionError(
            "Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable."
        )

    from app.scraper.gleif_repex import import_repex
    from app.scraper.gleif_incremental import (
        choose_catchup_interval,
        downloaded_deltas,
        fetch_publish_metadata,
        load_scope,
        import_lei_cdf_delta,
        import_rr_delta,
        read_last_publish,
        write_last_publish,
    )
    from app.db.schema import ensure_indexes
    from app.scraper.run_log import record_run

    # Idempotent, ~1s. Guarantees the checkpoint type (ImportState) + indexes exist
    # even when the cron runs before the API has ever started on a fresh DB — the
    # intended "full-import.sh, then let the daily delta take over" bootstrap.
    ensure_indexes()

    source_id = runner._ensure_source(runner.GLEIF_SOURCE_NAME, runner.GLEIF_SOURCE_URL, runner.BODS_GLEIF_CREDIBILITY)
    # ExitStack so the downloaded deltas (when we fetch any) are removed however
    # the run ends, without the happy path having to nest another `with`.
    with ExitStack() as stack, record_run("gleif-update", interval) as run:
        # The delta rides on top of the full golden copy. A delta carries every
        # record GLEIF changed worldwide, so applying it to anything less than that
        # baseline does not refresh the graph — it floods it.
        scope = load_scope()
        if scope is None:
            raise RuntimeError(
                "No GLEIF load found — the incremental update rides on top of the "
                "full golden copy. Run the full load first (full-import.sh / "
                "`manage.py gleif-lei-cdf`), then re-run.")
        # A subset baseline still gets its delta — that is how a curated database
        # stays current, and how the delta path itself gets exercised — but in
        # only-existing mode: refresh the companies that are here, ignore the rest
        # of the world. Applying a delta whole to a subset does not refresh it.
        only_existing = scope != "full" if only_existing is None else only_existing
        if only_existing:
            run["note"] = ("only-existing mode: refreshing the companies this database "
                           "already holds, ignoring records for the rest of the world")
            runner.log.info("GLEIF update: %s", run["note"])
        current_publish = None
        if lei_file and rr_file:
            resolved = "local"
            runner.log.info("GLEIF update: using local delta files")
        else:
            publish = fetch_publish_metadata()
            current_publish = publish.get("publish_date")
            if (interval or "auto").lower() == "auto":
                last = read_last_publish()
                resolved = choose_catchup_interval(last, current_publish)
                if resolved is None:
                    raise RuntimeError(
                        f"GLEIF delta can't cover the gap since last applied publish "
                        f"{last!r} (current {current_publish!r}) — too stale for a delta. "
                        "Run a full reload (full-import.sh).")
                runner.log.info("GLEIF update: auto interval=%s (last applied publish=%s, current=%s)",
                         resolved, last, current_publish)
            else:
                resolved = interval
                runner.log.info("GLEIF update: fetching %s deltas", resolved)
            # Held open across the whole apply below: the files have to survive
            # until they are read, and be gone once they are. `download_deltas`
            # alone leaks its temp directory, which is what it had been doing here
            # nightly for months.
            fetched = stack.enter_context(downloaded_deltas(publish, resolved))
            lei_file, rr_file = fetched["lei2"], fetched["rr"]
            repex_file = repex_file or fetched.get("repex")

        runner.log.info("GLEIF update: applying LEI-CDF delta %s", lei_file)
        lei = import_lei_cdf_delta(lei_file, source_id, runner.BODS_GLEIF_CREDIBILITY, limit=limit,
                                   only_existing=only_existing)
        runner.log.info("GLEIF update: applying RR delta %s", rr_file)
        rr = import_rr_delta(rr_file, source_id, runner.BODS_GLEIF_CREDIBILITY, limit=limit,
                             only_existing=only_existing)
        # Reporting exceptions last, so a company that gained a real parent in this
        # same delta is written before the reason it once had none. The importer
        # only ever updates entities that exist, so it needs no only_existing mode:
        # a statement about a company we do not carry lands nowhere by construction.
        repex = {}
        if repex_file:
            runner.log.info("GLEIF update: applying repex delta %s", repex_file)
            repex = import_repex(repex_file, limit=limit)
        run["total"] = lei["updated"] + rr["created"] + rr["closed"]
        # Advance the checkpoint only after a clean apply, and only when we fetched a
        # published delta in full (not local files, not a --limit spot check).
        if current_publish and not limit:
            write_last_publish(current_publish)

    return {"status": "ok", "source": runner.GLEIF_SOURCE_NAME, "interval": resolved,
            "publish_date": current_publish, "lei_cdf": lei, "rr": rr, "repex": repex}
