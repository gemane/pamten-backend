"""
Shared graph-write instrumentation for scrapers — the first slice of the scraper
write layer (Phase 1 of the multi-scraper refactor).

It holds the **touched-node collectors + the post-scrape auto-dedup framework**
every scraper reuses — a scrape entry point wrapped in `@with_autodedup` collects
the persons/entities it creates or updates and, on completion, runs the scoped
high-confidence auto-merge over just those, never a full-DB scan — and, since the
module split, the **shared write layer itself**: `_ensure_source` and the
entity/person/owns/role/succession upserts every source calls. SEC-form-specific
writers live in `sec_writer.py`; orchestration stays in `runner.py`, which
re-exports these names for compatibility.
"""
import contextvars
import functools
from datetime import datetime, timezone

from app import former_names as former
from app.claims import KIND_OWNS, KIND_ROLE, KIND_SUCCESSION, record_claim
from app.roles import canonical_role, combined_start, end_seats_at_death, periods_overlap
from app.database import db
from app.entity_resolution import resolve_entity_id
from app.scraper.owns_merge import SINCE_FIELDS, combine_since
from app.scraper.edge_schema import OWNS_PROPS, edge_create_clause, owns_props
from app.node_ids import mint_node_id
from app import addresses
from app.scraper.mapper import is_nominee_name, normalize_cik, normalize_entity_name, parse_full_name
import logging

from app.config import settings

log = logging.getLogger(__name__)


# During a scrape, collect the ids of the persons/entities it created or updated, so
# the post-scrape auto-dedup can be scoped to just them — a full-DB scan per company
# is O(all nodes) and crawls once the DB grows to millions of rows.
_touched_persons: contextvars.ContextVar = contextvars.ContextVar("touched_persons", default=None)
_touched_entities: contextvars.ContextVar = contextvars.ContextVar("touched_entities", default=None)

# The TARGET entity of the scrape (the searched company), so the outermost scope can
# stamp its on-demand freshness — distinct from the touched-sets (which hold every node
# the scrape brushed). Holds {"id", "depth"} or None.
_scrape_target: contextvars.ContextVar = contextvars.ContextVar("scrape_target", default=None)


def set_scrape_target(entity_id: str, depth: int = 0) -> None:
    """Record the scrape's target entity + the depth reached, so the outermost
    `_with_autodedup` scope stamps its freshness. No-op if id is falsy. Called by the
    source runners once they've resolved/created the searched company's node. When several
    sources run in ONE scope (Wikidata + SEC under an on-demand ensure), keep the DEEPEST
    depth for the same target — so a depth-blind source (SEC → 0) can't lower Wikidata's."""
    if not entity_id:
        return
    depth = int(depth)
    cur = _scrape_target.get()
    if cur and cur.get("id") == entity_id:
        depth = max(depth, int(cur.get("depth", 0)))
    _scrape_target.set({"id": entity_id, "depth": depth})


def _stamp_scrape_freshness(target: dict) -> None:
    """Stamp on-demand freshness on the target entity: `last_scraped_at` (now),
    `on_demand_scraped=true`, and `scrape_depth` bumped to the deepest pass so far
    (max — so a shallow SEC pass never lowers a deeper Wikidata one). Best-effort."""
    from app.db.arcadedb import run_command   # local import avoids an import cycle
    now = datetime.now(timezone.utc).isoformat()
    try:
        run_command(
            "MATCH (e:Entity {id: $id}) "
            "SET e.last_scraped_at = $now, e.on_demand_scraped = true, "
            "e.scrape_depth = CASE WHEN $d > coalesce(e.scrape_depth, -1) "
            "THEN $d ELSE e.scrape_depth END",
            {"id": target["id"], "now": now, "d": int(target.get("depth", 0))})
    except Exception as exc:  # noqa: BLE001 - never fail a scrape on the freshness stamp
        log.error("Freshness stamp failed for %s: %s", target.get("id"), exc)


def _record_touched(person_id: str) -> str:
    """Note a person id in the active scrape's touched-set (if one is active), and
    return it — so callers can `return _record_touched(pid)`."""
    bucket = _touched_persons.get()
    if bucket is not None and person_id:
        bucket.add(person_id)
    return person_id


def _record_touched_entity(entity_id: str) -> str:
    bucket = _touched_entities.get()
    if bucket is not None and entity_id:
        bucket.add(entity_id)
    return entity_id


def _run_scoped_autodedup(touched_persons: list, touched_entities: list) -> dict:
    """Scoped, high-confidence auto-merge over the ids a scrape touched — persons
    (SEC 'Page Lawrence' ↔ Wikidata 'Larry Page') and entities (the same company
    under different ids/sources, e.g. two GLEIF LEIs at one registered address).
    Only high-confidence merges apply; the rest stay for review. Best-effort — a
    dedup failure never fails the scrape. Gated by SCRAPER_AUTODEDUP_ENABLED."""
    out: dict = {}
    if not settings.SCRAPER_AUTODEDUP_ENABLED:
        return out
    try:
        from app.routers.persons import deduplicate_high_confidence
        dd = deduplicate_high_confidence(apply=True, seed_ids=touched_persons)
        out["deduplication"] = {"merged_count": dd["merged_count"], "review_count": dd["review_count"]}
    except Exception as exc:  # noqa: BLE001 - never fail a scrape on dedup
        log.error("Auto-dedup (persons) after scrape failed: %s", exc)
        out["deduplication"] = {"status": "error", "detail": str(exc)}
    try:
        from app.scraper.maintenance import deduplicate_entities_for
        ed = deduplicate_entities_for(touched_entities, apply=True)
        out["entity_deduplication"] = {"merged_count": ed["entities_merged"], "review_count": ed["needs_review"]}
    except Exception as exc:  # noqa: BLE001 - never fail a scrape on dedup
        log.error("Auto-dedup (entities) after scrape failed: %s", exc)
        out["entity_deduplication"] = {"status": "error", "detail": str(exc)}
    return out


def _scrape_target_after(target: dict | None) -> dict | None:
    """The target's id after the scoped dedup, which may have merged it away.

    MergedId keeps a forwarding address for exactly this; following it means the
    coordinates land on the surviving company rather than on a node the graph no
    longer serves.
    """
    if not target or not target.get("id"):
        return target
    try:
        from app.database import db
        from app.merged_ids import resolve_current_id
        with db.get_session() as session:
            survivor = resolve_current_id(session, target["id"])
        return {**target, "id": survivor} if survivor else target
    except Exception:  # noqa: BLE001 - a lookup failure just means "unchanged"
        return target


def _geocode_after_scrape(target: dict | None) -> dict:
    """Place the company the user just scraped, so its pin exists when they look.

    **The target only, not everything the scrape touched.** A depth-2 scrape can
    touch hundreds of entities, and Nominatim allows one request a second — the
    user would be waiting minutes. The target costs at most two requests (its
    headquarters and its registered office), usually fewer once the shared
    address cache has seen the address before.

    Everything else is left to the batch pass, which is not on a user's clock.

    Best-effort, like the dedup beside it: a company without a pin is a smaller
    problem than a scrape that failed.
    """
    if not (settings.SCRAPER_GEOCODE_ENABLED and settings.GEOCODING_ENABLED):
        return {}
    if not target or not target.get("id"):
        return {}
    try:
        from app.scraper.geocode_backfill import geocode_entities
        res = geocode_entities([target["id"]])
        return {"geocoding": {"geocoded": res["geocoded"]}}
    except Exception as exc:  # noqa: BLE001 - never fail a scrape on geocoding
        log.error("Geocoding after scrape failed for %s: %s", target.get("id"), exc)
        return {"geocoding": {"status": "error", "detail": str(exc)}}


def _with_autodedup(fn):
    """Wrap a scrape entry point so it collects the persons/entities it touches and,
    when it finishes, runs the scoped auto-dedup and stitches the summary into the
    returned dict. Re-entrant: a scrape nested inside another (run_scrape_all calls
    the single-source runners) shares the outer collector and skips its own dedup,
    so the merge runs exactly once — at the outermost scrape."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if _touched_persons.get() is not None:      # nested — outer scope owns dedup
            return fn(*args, **kwargs)
        ptoken = _touched_persons.set(set())
        etoken = _touched_entities.set(set())
        ttoken = _scrape_target.set(None)
        target = None
        try:
            result = fn(*args, **kwargs)
            touched_p = list(_touched_persons.get() or [])
            touched_e = list(_touched_entities.get() or [])
            target = _scrape_target.get()
        finally:
            _touched_persons.reset(ptoken)
            _touched_entities.reset(etoken)
            _scrape_target.reset(ttoken)
        if target:
            _stamp_scrape_freshness(target)
        if isinstance(result, dict):
            result.update(_run_scoped_autodedup(touched_p, touched_e))
            # After the dedup, not before: a merge can fold the target into a
            # survivor, and geocoding the id that no longer exists would write
            # coordinates nobody reads.
            result.update(_geocode_after_scrape(_scrape_target_after(target)))
        return result
    return wrapper


# ── The shared write layer ────────────────────────────────────────────────────
# Moved here from runner.py as this module's own docstring promised since
# Phase 1: the entity/person/owns/role upserts every scraper shares. Their
# behaviour is unchanged; runner re-exports the names so existing imports and
# test patch targets keep working.

def _now_iso() -> str:
    """UTC timestamp for last_scraped_at provenance."""
    return datetime.now(timezone.utc).isoformat()


# ── Database helpers ──────────────────────────────────────────────────────────

def _ensure_source(name: str, url: str, credibility: int, type_: str = "register") -> str:
    """The id of the Source node for `name`, creating the node when it is new.

    One helper for every source (Wikidata / SEC / OpenCorporates / GLEIF /
    UK PSC) — pass its constants. The id is STABLE (`sources.source_id_for`:
    the catalogue's id for a catalogue label, a slug of the name otherwise),
    the same on every instance; a node keyed the old way — a random uuid,
    before 2026-10-24 — is found by its name and keeps its id until
    `manage.py heal-source-ids` re-keys it, so nothing is ever created twice."""
    from app.scraper.sources import KNOWN_SOURCES, source_id_for, source_key_for
    key = source_key_for(name)
    meta = KNOWN_SOURCES.get(key) if key else None
    source_id = source_id_for(name, type_)
    with db.get_session() as session:
        rec = session.run("MATCH (s:Source {id: $id}) RETURN s.id AS id", id=source_id).single()
        if rec:
            return rec["id"]
        rec = session.run("MATCH (s:Source {name: $name}) RETURN s.id AS id", name=name).single()
        if rec:
            return rec["id"]
        session.run(
            """
            CREATE (s:Source {
                id: $id, name: $name, url: $url,
                credibility_score: $score, type: $type, registry_key: $key
            })
            """,
            id=source_id, name=name, url=url, score=credibility,
            type=(meta or {}).get("type", type_), key=key,
        )
        return source_id


def _person_search_text(full_name: str, aliases: list[str] | None) -> str:
    """What `/search` matches a person on.

    Persons are found through a FULL_TEXT index on `search_text`, exactly as
    entities are — and unlike entities, nothing was writing it. Every person a
    scraper created was therefore invisible to search: 174 of 177 on the dev
    graph, including Larry Page, who is in the graph as Google's founder and
    could not be found by name.

    Same recipe as `manage.py backfill-search` uses, so a fresh write and a
    backfilled row are identical.
    """
    return " ".join(part for part in [full_name or "", *(aliases or [])] if part).strip()


def _upsert_owns(owner_id: str, owned_id: str, source_id: str,
                 source_url: str | None = None, source_date: str | None = None,
                 owner_label: str = "Entity", credibility_score: int = 80,
                 since: str | None = None, until: str | None = None,
                 read_from: str | None = None):
    """Create an active OWNS edge if one doesn't already exist, and record this
    source's claim behind it.

    ``read_from``: how reliably the source's values were read
    (`edge_schema.READ_GRADES`) — "field" for the JSON this writer's callers
    bring (Wikidata's SPARQL rows). Stamped on the claim and on an edge this
    write creates; an existing edge keeps its own holder's grade.

    Stamps per-entry provenance (source_url/source_date/last_scraped_at). On a
    re-scrape of an existing edge, refresh last_scraped_at so the UI shows when
    the fact was last confirmed against the source.

    The edge holds one answer; the claim holds *this* source's answer. That
    matters on the existing-edge path below, which is reached whenever a second
    source confirms a relationship a first source already recorded.

    That path leaves source_id alone — the edge stays attributed to whoever
    created it — and therefore must leave source_url alone too. It used to
    overwrite the URL while keeping the id, so the edge cited one source with
    another's link: Sergey Brin's holding in Alphabet was attributed to SEC EDGAR
    and linked to wikidata.org. Invisible until the row menu started naming the
    source above the link, and then obvious.

    So the URL and date are **backfilled, never replaced** — `COALESCE(existing,
    new)`, the convention `_upsert_entity` states — and the id, url and date on an
    edge always describe one source. The second source's assertion is not lost:
    it is recorded as its own claim, which is what claims are for.
    `last_scraped_at` still moves, because "confirmed again just now" is true
    whoever confirmed it.

    Both endpoints are labelled (owner is Entity or Person, owned is always
    Entity) so the id lookups use the per-type index — a label-less
    `MATCH (a {id}), (b {id})` full-scans every node (~14s on 3M) per edge.
    """
    if owner_id == owned_id:
        log.warning("refusing a self-owning edge on %s", owner_id)
        return
    owner_label = owner_label if owner_label in ("Entity", "Person") else "Entity"
    now = _now_iso()
    # The statement's own period (Wikidata P580/P582), and the day the source
    # says so — its evidence date when it gives no start.
    source_date = source_date or now[:10]
    create_clause = edge_create_clause(OWNS_PROPS)
    record_claim(
        kind=KIND_OWNS, from_id=owner_id, to_id=owned_id, source_id=source_id,
        since=since, until=until, source_url=source_url, source_date=source_date,
        credibility_score=credibility_score, read_from=read_from,
    )
    # A claims-only source may assert (the claim above) but not draw —
    # see sources.edge_writes_suppressed. RELATED_TO/DUAL_LISTED writers are
    # deliberately ungated: SEC-only paths, gated when a need appears.
    from app.scraper.sources import edge_writes_suppressed
    if edge_writes_suppressed(source_id):
        return None
    with db.get_session() as session:
        if until:
            # An ENDED holding is history: its own closed edge (once), never a
            # change to the pair's current one.
            same = session.run(
                f"""
                MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                WHERE r.until = $until AND ((r.since IS NULL AND $since IS NULL) OR r.since = $since)
                RETURN r LIMIT 1
                """, oid=owner_id, nid=owned_id, until=until, since=since).single()
            if not same:
                session.run(
                    f"""
                    MATCH (a:{owner_label} {{id: $oid}}), (b:Entity {{id: $nid}})
                    CREATE (a)-[:OWNS {{{create_clause}}}]->(b)
                    """,
                    oid=owner_id, nid=owned_id,
                    **owns_props(ownership_type="unknown", source_id=source_id,
                                 credibility_score=credibility_score, since=since, until=until,
                                 source_url=source_url, source_date=source_date,
                                 last_scraped_at=now, stale=False, read_from=read_from))
            return
        exists = session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
            WHERE r.until IS NULL RETURN r LIMIT 1
            """,
            oid=owner_id,
            nid=owned_id,
        ).single()
        if exists:
            # A source may refresh the freshness of an edge at or below its own
            # credibility, and no higher. `last_scraped_at` is what the UI shows
            # as "last confirmed against the source", and what the staleness pass
            # reads — a Wikidata visit re-confirming an SEC edge would otherwise
            # launder a register fact's freshness through a community source. The
            # claim above is recorded either way: corroboration is exactly what a
            # lower-tier source confirming a register edge is worth.
            #
            # The refresh also clears `stale` — the mark the maintenance pass puts
            # on community edges nothing has confirmed for months — because a
            # legitimate re-confirmation is precisely what staleness is not.
            session.run(
                f"""
                MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                WHERE r.until IS NULL
                SET r.last_scraped_at = CASE WHEN COALESCE(r.credibility_score, 0) <= $cred
                                             THEN $now ELSE r.last_scraped_at END,
                    r.stale           = CASE WHEN COALESCE(r.credibility_score, 0) <= $cred
                                             THEN false ELSE r.stale END,
                    r.source_url  = COALESCE(r.source_url,  $surl),
                    r.source_date = COALESCE(r.source_date, $sdate)
                """,
                oid=owner_id, nid=owned_id, now=now, cred=credibility_score,
                surl=source_url, sdate=source_date,
            )
            # The start, combined like every writer's: the earliest wins, a
            # stated one over a lower bound on the same day.
            held = exists["r"]
            started = combine_since({f: held.get(f) for f in SINCE_FIELDS}, {"since": since})
            if started["since"] != held.get("since") or started["since_basis"] != held.get("since_basis"):
                session.run(
                    f"""
                    MATCH (a:{owner_label} {{id: $oid}})-[r:OWNS]->(b:Entity {{id: $nid}})
                    WHERE r.until IS NULL
                    SET r.since = $s, r.since_basis = $b, r.since_source_url = $u
                    """, oid=owner_id, nid=owned_id, s=started["since"],
                    b=started["since_basis"], u=started["since_source_url"])
            return
        session.run(
            f"""
            MATCH (a:{owner_label} {{id: $oid}}), (b:Entity {{id: $nid}})
            CREATE (a)-[:OWNS {{{create_clause}}}]->(b)
            """,
            oid=owner_id, nid=owned_id,
            # The full schema bag — Wikidata states no stake, so almost all of
            # it is null, but the key set matches every other writer's, which
            # is the point: the same schema, however sparse the source.
            **owns_props(ownership_type="unknown", source_id=source_id,
                         credibility_score=credibility_score, since=since,
                         source_url=source_url, source_date=source_date,
                         last_scraped_at=now, stale=False, read_from=read_from),
        )


def _upsert_succession(predecessor_id: str, successor_id: str, source_id: str,
                       since: str | None = None,
                       source_url: str | None = None, source_date: str | None = None,
                       credibility_score: int = 80):
    """Create a SUCCEEDED_BY edge (predecessor → successor) if none exists.

    Models corporate succession/rename (e.g. Twitter → X Corp., from Wikidata
    P1366 'replaced by' / P1365 'replaces'). `since` is when the succession took
    effect (Wikidata P585 qualifier). Directed predecessor → successor; a
    re-scrape refreshes provenance instead of duplicating the edge. Both
    endpoints are Entity, labelled so the id lookups use the per-type index.
    """
    record_claim(kind=KIND_SUCCESSION, from_id=predecessor_id, to_id=successor_id,
                 source_id=source_id, since=since, source_url=source_url,
                 source_date=source_date, credibility_score=credibility_score)
    # A claims-only source may assert (the claim above) but not draw —
    # see sources.edge_writes_suppressed. RELATED_TO/DUAL_LISTED writers are
    # deliberately ungated: SEC-only paths, gated when a need appears.
    from app.scraper.sources import edge_writes_suppressed
    if edge_writes_suppressed(source_id):
        return None
    if predecessor_id == successor_id:
        return  # guard against a self-loop from bad data
    now = _now_iso()
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (a:Entity {id: $pid})-[r:SUCCEEDED_BY]->(b:Entity {id: $sid})
            RETURN r LIMIT 1
            """,
            pid=predecessor_id, sid=successor_id,
        ).single()
        if exists:
            session.run(
                """
                MATCH (a:Entity {id: $pid})-[r:SUCCEEDED_BY]->(b:Entity {id: $sid})
                SET r.last_scraped_at = $now,
                    r.since       = COALESCE($since, r.since),
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                pid=predecessor_id, sid=successor_id, now=now,
                since=since, surl=source_url, sdate=source_date,
            )
            return
        session.run(
            """
            MATCH (a:Entity {id: $pid}), (b:Entity {id: $sid})
            CREATE (a)-[:SUCCEEDED_BY {
                since: $since, source_id: $srcid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now
            }]->(b)
            """,
            pid=predecessor_id, sid=successor_id, srcid=source_id, since=since,
            score=credibility_score, surl=source_url, sdate=source_date, now=now,
        )


def _matching_role(session, person_id: str, entity_id: str, role: str,
                   open_only: bool = True) -> list[dict]:
    """Existing HAS_ROLE edges asserting the SAME position as `role`.

    Matched on `canonical_role`, so "Director" (Form D) and "Board Member"
    (Wikidata) are one seat, not two rows. Returns the stored edges' props —
    the caller updates by the stored label, and may relabel it when the
    incoming assertion is more credible (the panel shows the best word for
    the position, and every asserter still shows via the claims table)."""
    want = canonical_role(role)
    rows = session.run(
        """
        MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
        RETURN r.role AS role, r.since AS since, r.until AS until,
               r.credibility_score AS cred
        """,
        pid=person_id, eid=entity_id)
    out = []
    for r in rows:
        if canonical_role(r.get("role") or "") != want:
            continue
        if open_only and r.get("until"):
            continue
        out.append({"role": r.get("role"), "since": r.get("since"),
                    "until": r.get("until"), "cred": r.get("cred") or 0})
    return out


def _relabel_if_more_credible(session, person_id: str, entity_id: str,
                              stored_label: str, new_label: str,
                              stored_cred: int, new_cred: int,
                              source_id: str, read_from: str | None = None) -> None:
    """A more credible source naming the same position takes over the label.

    The edge stays ONE edge; only its display word, source and credibility
    upgrade (Wikidata's "Board Member" becomes SEC's "Director"). Never
    downgrades — a later community re-assertion leaves a statutory label
    alone."""
    if new_cred <= stored_cred or new_label == stored_label:
        return
    session.run(
        """
        MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
        WHERE r.role = $old
        SET r.role = $new, r.source_id = $sid, r.credibility_score = $cred,
            r.read_from = $rfrom
        """,
        pid=person_id, eid=entity_id, old=stored_label, new=new_label,
        sid=source_id, cred=new_cred, rfrom=read_from)


def _same_seat(session, person_id: str, entity_id: str, role: str,
               since: str | None, until: str | None) -> dict | None:
    """The stored seat an assertion of `role` for `since`–`until` is about:
    the same position (`canonical_role`) over a touching period
    (`roles.periods_overlap`) — an open seat first, then the spell it
    touches. None when the assertion is a new spell (or the first). An
    assertion without a start cannot found a spell: it is about the seat
    held, and its end (if any) is the caller's to apply only to a seat that
    began by then."""
    matches = _matching_role(session, person_id, entity_id, role, open_only=False)
    matches.sort(key=lambda m: (m["until"] is not None, m["since"] or ""))
    return next((m for m in matches
                 if since is None or periods_overlap(m["since"], m["until"], since, until)), None)


def _upsert_role(person_id: str, entity_id: str, role: str, source_id: str,
                 since: str | None = None, until: str | None = None,
                 source_url: str | None = None, credibility_score: int = 80,
                 read_from: str | None = None):
    """Create a HAS_ROLE edge if one doesn't already exist.

    ``read_from``: how reliably the seat was read (`edge_schema.READ_GRADES`);
    on the claim, on a seat this write creates, and on a relabelled one.

    Matched on the position (`canonical_role`) over a touching period
    (`roles.periods_overlap`, `_same_seat`): nobody holds one post at one
    company twice at once, so an assertion dated 2022-07-14 is the seat held
    open since 2022-07-11 — Wikidata's appointment day beside the SEC's Form 3
    date used to be two "Director" rows — and the seat keeps the earlier day
    (`roles.combined_start`; one date at two precisions keeps the finer:
    Ryan Roslansky's LinkedIn CEO statement as 2020-06-01 and "June 2020").
    An undated assertion of a held role says nothing new, and a dated one
    teaches an undated seat its date. Two spells that do not touch stay two
    seats: Larry Page, CEO of Google 1998–2001 and again from 2011.
    """
    now = _now_iso()
    # The claim is dated by the day the source said it — the scrape day for
    # a knowledge base, as the owns claims are; the 290 Wikidata role claims
    # on dev had no date at all (2026-10-25; `heal-model-gaps` dates them by
    # their first sighting).
    record_claim(kind=KIND_ROLE, from_id=person_id, to_id=entity_id, source_id=source_id,
                 role=role, since=since, until=until, source_url=source_url,
                 source_date=now[:10],
                 credibility_score=credibility_score, read_from=read_from)
    # HAS_ROLE is deliberately NOT gated by claims-only: the mode distrusts a
    # source's OWNERSHIP structure, and people are exactly what the Wikidata
    # verdict wanted kept. Suppressing roles orphaned every Wikidata person
    # (absent from panels) and starved the person dedup of its shared-company
    # corroboration — Bill Gates and "Gates William H Iii" could never merge.
    with db.get_session() as session:
        exists = _same_seat(session, person_id, entity_id, role, since, until)
        if exists:
            # The seat this assertion is about, matched by ITS stored dates;
            # the start becomes the finer of one date at two precisions, else
            # the earliest — "June 2020" learning its day takes the day, an
            # undated seat learns its date, and the Form 3 three days after
            # Wikidata's appointment day leaves the earlier day in place.
            start = combined_start(exists["since"], since)
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role
                  AND (($stored IS NULL AND r.since IS NULL) OR r.since = $stored)
                  AND (($stored_until IS NULL AND r.until IS NULL) OR r.until = $stored_until)
                SET r.last_scraped_at = $now,
                    r.since = $start,
                    r.source_date = CASE WHEN $stored IS NULL AND $start IS NOT NULL
                                         THEN $start ELSE r.source_date END,
                    r.source_url = COALESCE(r.source_url, $surl)
                """,
                pid=person_id, eid=entity_id, role=exists["role"],
                stored=exists["since"], stored_until=exists["until"],
                start=start, now=now, surl=source_url,
            )
            if until and not exists["until"]:
                # The source now states the END of a seat we hold open — a
                # Wikidata end time added later. It used to be dropped here, so
                # a CEO who left in 2015 was still CEO in 2020 and today. Only a
                # seat that began by then (an old end must not close a new one).
                session.run(
                    """
                    MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                    WHERE r.role = $role AND r.until IS NULL
                      AND (($since IS NULL AND r.since IS NULL) OR r.since = $since)
                      AND (r.since IS NULL OR r.since <= $until)
                    SET r.until = $until, r.until_source_url = $surl
                    """,
                    pid=person_id, eid=entity_id, role=exists["role"], since=start,
                    until=until, surl=source_url)
            _relabel_if_more_credible(session, person_id, entity_id,
                                      exists["role"], role, exists["cred"],
                                      credibility_score, source_id, read_from)
            return

        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until, until_source_url: $until_surl,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now,
                read_from: $rfrom
            }]->(e)
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
            since=since,
            until=until,
            until_surl=source_url if until else None,
            sid=source_id,
            score=credibility_score,
            rfrom=read_from,
            # An undated seat is still dated by its evidence: the source lists
            # it TODAY. Without it the seat was dimmed in every year, the
            # present included.
            sdate=since or now[:10],
            surl=source_url, now=now,
        )
    if until is None:
        end_seats_at_death(person_id)      # a seat opened for someone since dead ends at the death


def _search_text(name: str, description: str | None, aliases: list[str] | None) -> str:
    """FULL_TEXT search field: name + description + aliases (same recipe as the
    Wikidata scraper and manage.py backfill-search)."""
    return " ".join(p for p in (
        name, description or "", " ".join(aliases or [])) if p).strip()


def _merge_aliases(existing: list[str] | None, new: list[str] | None,
                   current_name: str | None = None) -> list[str]:
    """Union of `existing` + `new` aliases, order-preserving, deduped
    case-insensitively, excluding the entity's current legal name."""
    out: list[str] = []
    seen: set[str] = set()
    skip = (current_name or "").strip().lower()
    for a in list(existing or []) + list(new or []):
        a = (a or "").strip()
        k = a.lower()
        if a and k != skip and k not in seen:
            seen.add(k)
            out.append(a)
    return out


#: what EDGAR states of a filer's business address (app.addresses)
_SEC_HQ = ("full", "city", "country", "street", "postcode")
_SEC_HQ_FILL = addresses.fill_clause("e", addresses.HEADQUARTERS, _SEC_HQ)
_SEC_HQ_CREATE = addresses.create_map(addresses.HEADQUARTERS, _SEC_HQ)


def _hq_params(headquarters: dict | None) -> dict:
    """Query parameters for a headquarters, every one present (None when not
    stated). Every write is a COALESCE, so a missing address leaves whatever
    is already there; the parts, so the geocoder never has to re-parse the
    string."""
    hq = headquarters or {}
    return addresses.parts(addresses.HEADQUARTERS, street=hq.get("street"), city=hq.get("city"),
                           postcode=hq.get("postcode"), country=hq.get("country"),
                           full=hq.get("address"), empty=_SEC_HQ)


def _resolve_by_name(session, name: str, name_norm: str, country: str | None) -> str | None:
    """The node a name alone can stand for. With a ``country``: the node of
    that name (exact, then by name key) registered THERE, else one of that
    name whose country is unknown; a namesake registered elsewhere is a
    company of its own, so None rather than it. Without a country: any node
    of that name, as `resolve_entity_id` finds it."""
    if not country:
        return resolve_entity_id(session, name=name, name_normalized=name_norm)
    for field, value in (("name", name), ("name_normalized", name_norm)):
        for where in ("e.country = $country", "e.country IS NULL"):
            rec = session.run(
                f"MATCH (e:Entity) WHERE e.{field} = $v AND {where} RETURN e.id AS id LIMIT 1",
                v=value, country=country).single()
            if rec:
                return rec["id"]
    return None


def _upsert_entity_by_name(name: str, entity_type: str = "company",
                            cik: str | None = None,
                            source_id: str | None = None,
                            former_names: list[str] | None = None,
                            lei: str | None = None,
                            country: str | None = None,
                            jurisdiction_code: str | None = None,
                            headquarters: dict | None = None,
                            website: str | None = None,
                            credibility_score: int = 98) -> str:
    """Find or create an Entity node matched by CIK, exact name, or normalized name.

    A name says which company only together with where it is registered
    (`_resolve_by_name`): a row that states a ``country`` takes the node of
    that name there, else one of that name without a country (which learns
    it), else a new node — never the namesake abroad. Lavoro's "Agrointegral
    Andina S.A.S." in Colombia and in Ecuador; and, since 2026-10-25 for
    every dated row, Chubb's "Chubb Company Services Limited" (England),
    "Chubb Services Limited" (Cayman) and "Chubb Services Corporation"
    (Illinois), one name key once "Company", "Services" and the suffix are
    dropped, which had become one node with three parents at 100 %.

    ``source_id`` (the calling scraper's Source node — SEC EDGAR or
    OpenCorporates) is stamped so the entity's own provenance shows in the node
    panel. On an existing node it only fills a missing value, so a register or
    Wikidata source already recorded isn't overwritten.

    ``former_names`` (SEC EDGAR ``formerNames`` — prior names of the *same* CIK,
    as ``app.former_names`` entries ``"NAME|YYYY-MM-DD"``; bare names are taken
    too) are folded into the entity's ``aliases`` + ``search_text`` so the old
    name is searchable (e.g. "Facebook" → Meta Platforms), and kept with their
    dates in ``former_names`` for the timeline's "Renamed from". Merged, never
    clobbered.

    ``headquarters`` (``{address, city, country}``) is where the company is
    **run** — EDGAR's business address. Kept strictly apart from ``country``,
    which is where it is *registered*: a foreign filer's EDGAR address is often
    its US filing office, so the two answer different questions and the map draws
    one or the other. Only ever fills a blank."""
    name_norm = normalize_entity_name(name)
    cik = normalize_cik(cik)                   # the 10-digit form, as every CIK is stored and looked up
    with db.get_session() as session:
        entity_id = (resolve_entity_id(session, sec_cik=cik) if cik else None) \
            or _resolve_by_name(session, name, name_norm, country)
        if not entity_id and cik:
            # The prefix must end on a WORD boundary, and the candidate must not
            # already belong to a different filer.
            #
            # Without the boundary this matched any company whose name merely
            # starts with the same letters: scraping "Alphabet" resolved onto a
            # French company called "ALPHA" ("alphabet" STARTS WITH "alpha") and
            # stamped Alphabet Inc's CIK and its 13G holders — BlackRock, Vanguard,
            # Fidelity — onto it. The CIK guard is the second line of defence: a
            # node that already carries another filer's CIK is definitively not
            # this filer.
            rec = session.run(
                """
                MATCH (e:Entity)
                WHERE e.name_normalized IS NOT NULL
                  AND size(e.name_normalized) >= 4
                  AND $name_norm STARTS WITH (e.name_normalized + ' ')
                  AND (e.sec_cik IS NULL OR e.sec_cik = $cik)
                RETURN e.id AS id LIMIT 1
                """,
                name_norm=name_norm, cik=cik,
            ).single()
            entity_id = rec["id"] if rec else None

        if entity_id:
            # Only stamp the CIK onto the existing entity; preserve whatever
            # name and credibility the entity already has (Wikidata names are
            # human-readable; EDGAR registered names are all-caps legal strings).
            if former_names:
                # Fold former names into aliases + search_text, and their dated
                # entries into former_names (unions, no clobber).
                rec = session.run(
                    "MATCH (e:Entity {id: $id}) RETURN e.name AS name, "
                    "e.aliases AS aliases, e.description AS descr, "
                    "e.former_names AS former_names",
                    id=entity_id,
                ).single()
                cur_name = (rec["name"] if rec else None) or name
                merged   = _merge_aliases(rec["aliases"] if rec else None,
                                          former.names(former_names), cur_name)
                dated    = former.merge(rec["former_names"] if rec else None, former_names)
                session.run(
                    f"""
                    MATCH (e:Entity {{id: $id}})
                    SET e.sec_cik     = COALESCE($cik, e.sec_cik),
                        e.lei_id      = COALESCE($lei, e.lei_id),
                        e.source_id   = COALESCE(e.source_id, $source_id),
                        e.country     = COALESCE(e.country, $country),
                        e.jurisdiction_code = COALESCE(e.jurisdiction_code, $jurisdiction_code),
                        {_SEC_HQ_FILL},
                        e.website     = COALESCE(e.website, $website),
                        e.aliases     = $aliases,
                        e.former_names = $former_names,
                        e.search_text = $search_text
                    """,
                    id=entity_id, cik=cik, lei=lei, source_id=source_id, country=country,
                    jurisdiction_code=jurisdiction_code, website=website,
                    **_hq_params(headquarters),
                    aliases=merged, former_names=dated,
                    search_text=_search_text(cur_name, rec["descr"] if rec else None, merged),
                )
            else:
                session.run(
                    f"""
                    MATCH (e:Entity {{id: $id}})
                    SET e.sec_cik   = COALESCE($cik, e.sec_cik),
                        e.lei_id    = COALESCE($lei, e.lei_id),
                        e.source_id = COALESCE(e.source_id, $source_id),
                        e.country   = COALESCE(e.country, $country),
                        e.jurisdiction_code = COALESCE(e.jurisdiction_code, $jurisdiction_code),
                        {_SEC_HQ_FILL},
                        e.website     = COALESCE(e.website, $website)
                    """,
                    id=entity_id, cik=cik, lei=lei, source_id=source_id, country=country,
                    jurisdiction_code=jurisdiction_code, website=website,
                    **_hq_params(headquarters),
                )
            return _record_touched_entity(entity_id)

        # keyed by the LEI or the CIK it arrives with (app.node_ids); a
        # name-only row (an Exhibit 21 subsidiary) gets a uuid
        entity_id = mint_node_id(lei=lei, sec_cik=cik)
        aliases = _merge_aliases([], former.names(former_names), name)
        session.run(
            f"""
            CREATE (e:Entity {{
                id: $id, name: $name, name_normalized: $name_norm,
                name_credibility: $cred, search_text: $search_text, aliases: $aliases,
                former_names: $former_names,
                type: $type, sec_cik: $cik, lei_id: $lei, source_id: $source_id,
                is_nominee: $is_nominee,
                country: $country, jurisdiction_code: $jurisdiction_code,
                founded: null, revenue: null,
                description: null, wikidata_id: null,
                {_SEC_HQ_CREATE},
                website: $website, created_at: $created_at
            }})
            """,
            id=entity_id, name=name, name_norm=name_norm, website=website,
            created_at=_now_iso(),
            cred=credibility_score, type=entity_type, cik=cik, lei=lei, source_id=source_id,
            country=country, jurisdiction_code=jurisdiction_code,
            **_hq_params(headquarters),
            search_text=_search_text(name, None, aliases), aliases=aliases,
            former_names=former.merge([], former_names),
            is_nominee=is_nominee_name(name),
        )
        return _record_touched_entity(entity_id)


def _upsert_person_by_name(full_name: str, source_id: str | None = None,
                           sec_cik: str | None = None) -> str:
    """
    Find or create a Person node matched by SEC CIK, then full_name.

    ``sec_cik`` is the SEC-assigned Central Index Key of a filer — one per
    person, stable across every Form 3/4/13D/G they file however their name is
    spelled. It is matched FIRST because it is a hard identifier: it collapses
    "Timothy D Cook" and "Cook Timothy D" to one node, and it is person-unique,
    so it never conflates a father and son who share a name and a company (which
    name matching alone would). Stored fill-if-missing on whichever node wins,
    so a name-matched node gains the CIK for next time.

    SEC EDGAR investor filings use LAST FIRST word order, while Form 3/4
    executive filings use FIRST LAST order; the reversed two-word form is tried
    before creating a new node.
    """
    sec_cik = normalize_cik(sec_cik)           # the 10-digit form, as every CIK is stored and looked up
    parts = full_name.strip().split()
    reversed_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else None

    first_name, last_name = parse_full_name(full_name)
    with db.get_session() as session:
        # 0. Hard id: the SEC CIK. A filer is the same person across every
        #    spelling, and never confusable with a same-named relative.
        if sec_cik:
            rec = session.run(
                "MATCH (p:Person {sec_cik: $cik}) RETURN p.id AS id LIMIT 1",
                cik=sec_cik).single()
            if rec:
                return _record_touched(rec["id"])

        # 1. Exact name — but a CONFLICTING CIK means a different person (a
        #    father and son share a name; their CIKs differ), so skip a
        #    name match whose node already has a different CIK. Stamp the CIK
        #    when the matched node had none.
        rec = session.run(
            "MATCH (p:Person {full_name: $name}) RETURN p.id AS id, p.sec_cik AS cik LIMIT 1",
            name=full_name,
        ).single()
        if rec and not _cik_conflict(rec.get("cik"), sec_cik):
            _stamp_person_cik(session, rec["id"], sec_cik)
            return _record_touched(rec["id"])

        # 2. Reversed two-word form — catches "Brin Sergey" when "Sergey Brin"
        #    already exists (or vice-versa), same CIK-conflict guard.
        if reversed_name:
            rec = session.run(
                "MATCH (p:Person {full_name: $name}) RETURN p.id AS id, p.sec_cik AS cik LIMIT 1",
                name=reversed_name,
            ).single()
            if rec and not _cik_conflict(rec.get("cik"), sec_cik):
                _stamp_person_cik(session, rec["id"], sec_cik)
                return _record_touched(rec["id"])

        person_id = mint_node_id(sec_cik=sec_cik)                # `sec:{cik}`, a uuid without one
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, description: null,
                wikidata_id: null, sec_cik: $cik, source_id: $source_id,
                aliases: [], search_text: $search_text,
                created_at: $created_at, last_scraped_at: $created_at
            })
            """,
            id=person_id, first=first_name, last=last_name, full=full_name,
            cik=sec_cik, created_at=_now_iso(),
            search_text=_person_search_text(full_name, None),
            source_id=source_id,
        )
        return _record_touched(person_id)


def _cik_conflict(existing: str | None, incoming: str | None) -> bool:
    """True when both CIKs are present and differ — a same-name node that is a
    DIFFERENT filer (a father/son), which a name match must not fold together."""
    return bool(existing and incoming and existing != incoming)


def _stamp_person_cik(session, person_id: str, sec_cik: str | None) -> None:
    """A person matched by name: stamp when they were last written from a
    source, and fill their SEC CIK when they had none yet — COALESCE, so a
    CIK already recorded is never overwritten."""
    session.run("MATCH (p:Person {id: $id}) SET p.sec_cik = COALESCE(p.sec_cik, $cik), "
                "p.last_scraped_at = $now",
                id=person_id, cik=normalize_cik(sec_cik), now=_now_iso())
