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
import uuid
from datetime import datetime, timezone

from app.claims import KIND_OWNS, KIND_ROLE, KIND_SUCCESSION, record_claim
from app.database import db
from app.entity_resolution import resolve_entity_id
from app.scraper.edge_schema import OWNS_PROPS, edge_create_clause, owns_props
from app.scraper.mapper import is_nominee_name, normalize_entity_name, parse_full_name
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
    """Get or create a Source node by name, return its id. One helper for every
    source (Wikidata / SEC / OpenCorporates / GLEIF / UK PSC) — pass its constants;
    Wikidata is the lone ``knowledge_base``, the rest are ``register``."""
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id", name=name,
        ).single()
        if rec:
            return rec["id"]

        source_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (s:Source {
                id: $id, name: $name, url: $url,
                credibility_score: $score, type: $type
            })
            """,
            id=source_id, name=name, url=url, score=credibility, type=type_,
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
                 owner_label: str = "Entity", credibility_score: int = 80):
    """Create an active OWNS edge if one doesn't already exist, and record this
    source's claim behind it.

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
    create_clause = edge_create_clause(OWNS_PROPS)
    record_claim(
        kind=KIND_OWNS, from_id=owner_id, to_id=owned_id, source_id=source_id,
        source_url=source_url, source_date=source_date,
        credibility_score=credibility_score,
    )
    with db.get_session() as session:
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
                         credibility_score=credibility_score,
                         source_url=source_url, source_date=source_date,
                         last_scraped_at=now, stale=False),
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


def _upsert_role(person_id: str, entity_id: str, role: str, source_id: str,
                 since: str | None = None, until: str | None = None,
                 source_url: str | None = None, credibility_score: int = 80):
    """Create a HAS_ROLE edge if one doesn't already exist.

    Matched on role, and on `since` **only when the incoming assertion has one**.
    A dated tenure is its own edge — someone can be CEO twice — but an *undated*
    assertion of a role that is already recorded says nothing new, and adding it
    is how a person came to be listed twice on the same board: the company scrape
    knew Larry Page joined Alphabet's board in 1998, and the person scrape, which
    gets no dates from the reverse lookup, added a second edge beside it.
    """
    record_claim(kind=KIND_ROLE, from_id=person_id, to_id=entity_id, source_id=source_id,
                 role=role, since=since, until=until, source_url=source_url,
                 credibility_score=credibility_score)
    now = _now_iso()
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.role = $role
              AND ($since IS NULL OR r.since = $since)
            RETURN r LIMIT 1
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
            since=since,
        ).single()
        if exists:
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role
                  AND ($since IS NULL OR r.since = $since)
                SET r.last_scraped_at = $now,
                    r.source_url = COALESCE(r.source_url, $surl)
                """,
                pid=person_id, eid=entity_id, role=role, since=since, now=now,
                surl=source_url,
            )
            return

        if since:
            # We now know when a role we already had started. That is the same
            # role learning its date, not a second appointment — so fill the
            # blank rather than creating an edge beside it. (The reverse order is
            # handled above: an undated assertion matches a dated edge.)
            undated = session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.since IS NULL
                RETURN r LIMIT 1
                """,
                pid=person_id, eid=entity_id, role=role,
            ).single()
            if undated:
                session.run(
                    """
                    MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                    WHERE r.role = $role AND r.since IS NULL
                    SET r.since = $since, r.source_date = $since,
                        r.last_scraped_at = $now,
                        r.source_url = COALESCE($surl, r.source_url)
                    """,
                    pid=person_id, eid=entity_id, role=role, since=since, now=now,
                    surl=source_url,
                )
                return

        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $since, last_scraped_at: $now
            }]->(e)
            """,
            pid=person_id,
            eid=entity_id,
            role=role,
            since=since,
            until=until,
            sid=source_id,
            score=credibility_score,
            surl=source_url, now=now,
        )


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


def _hq_params(headquarters: dict | None) -> dict:
    """Query parameters for a headquarters, or three Nones. Every write is a
    COALESCE, so a missing address leaves whatever is already there."""
    hq = headquarters or {}
    return {"hq_address":  hq.get("address") or None,
            "hq_city":     hq.get("city") or None,
            "hq_country":  hq.get("country") or None,
            # The parts, so the geocoder never has to re-parse the string.
            "hq_street":   hq.get("street") or None,
            "hq_postcode": hq.get("postcode") or None}


def _upsert_entity_by_name(name: str, entity_type: str = "company",
                            cik: str | None = None,
                            source_id: str | None = None,
                            former_names: list[str] | None = None,
                            lei: str | None = None,
                            country: str | None = None,
                            headquarters: dict | None = None,
                            credibility_score: int = 98) -> str:
    """Find or create an Entity node matched by CIK, exact name, or normalized name.

    ``source_id`` (the calling scraper's Source node — SEC EDGAR or
    OpenCorporates) is stamped so the entity's own provenance shows in the node
    panel. On an existing node it only fills a missing value, so a register or
    Wikidata source already recorded isn't overwritten.

    ``former_names`` (SEC EDGAR ``formerNames`` — prior names of the *same* CIK)
    are folded into the entity's ``aliases`` + ``search_text`` so the old name is
    searchable (e.g. "Facebook" → Meta Platforms). Merged, never clobbered.

    ``headquarters`` (``{address, city, country}``) is where the company is
    **run** — EDGAR's business address. Kept strictly apart from ``country``,
    which is where it is *registered*: a foreign filer's EDGAR address is often
    its US filing office, so the two answer different questions and the map draws
    one or the other. Only ever fills a blank."""
    name_norm = normalize_entity_name(name)
    with db.get_session() as session:
        # Indexed lookups first (an OR full-scans the Entity type on ArcadeDB).
        entity_id = resolve_entity_id(
            session, sec_cik=cik, name=name, name_normalized=name_norm,
        )
        # Fuzzy CIK fallback: an EDGAR filer whose stored normalized name is a
        # prefix of this one. This can't use an index (variable-length prefix of
        # the *parameter*), so only run it as a last resort when a CIK is known
        # and the indexed lookups missed.
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
                # Fold former names into aliases + search_text (union, no clobber).
                rec = session.run(
                    "MATCH (e:Entity {id: $id}) RETURN e.name AS name, "
                    "e.aliases AS aliases, e.description AS descr",
                    id=entity_id,
                ).single()
                cur_name = (rec["name"] if rec else None) or name
                merged   = _merge_aliases(rec["aliases"] if rec else None, former_names, cur_name)
                session.run(
                    """
                    MATCH (e:Entity {id: $id})
                    SET e.sec_cik     = COALESCE($cik, e.sec_cik),
                        e.lei_id      = COALESCE($lei, e.lei_id),
                        e.source_id   = COALESCE(e.source_id, $source_id),
                        e.country     = COALESCE(e.country, $country),
                        e.hq_address  = COALESCE(e.hq_address, $hq_address),
                        e.hq_city     = COALESCE(e.hq_city, $hq_city),
                        e.hq_country  = COALESCE(e.hq_country, $hq_country),
                        e.hq_street   = COALESCE(e.hq_street, $hq_street),
                        e.hq_postcode = COALESCE(e.hq_postcode, $hq_postcode),
                        e.aliases     = $aliases,
                        e.search_text = $search_text
                    """,
                    id=entity_id, cik=cik, lei=lei, source_id=source_id, country=country,
                    **_hq_params(headquarters),
                    aliases=merged,
                    search_text=_search_text(cur_name, rec["descr"] if rec else None, merged),
                )
            else:
                session.run(
                    """
                    MATCH (e:Entity {id: $id})
                    SET e.sec_cik   = COALESCE($cik, e.sec_cik),
                        e.lei_id    = COALESCE($lei, e.lei_id),
                        e.source_id = COALESCE(e.source_id, $source_id),
                        e.country   = COALESCE(e.country, $country),
                        e.hq_address = COALESCE(e.hq_address, $hq_address),
                        e.hq_city    = COALESCE(e.hq_city, $hq_city),
                        e.hq_country = COALESCE(e.hq_country, $hq_country),
                        e.hq_street   = COALESCE(e.hq_street, $hq_street),
                        e.hq_postcode = COALESCE(e.hq_postcode, $hq_postcode)
                    """,
                    id=entity_id, cik=cik, lei=lei, source_id=source_id, country=country,
                    **_hq_params(headquarters),
                )
            return _record_touched_entity(entity_id)

        entity_id = str(uuid.uuid4())
        aliases = _merge_aliases([], former_names, name)
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, name_normalized: $name_norm,
                name_credibility: $cred, search_text: $search_text, aliases: $aliases,
                type: $type, sec_cik: $cik, lei_id: $lei, verified: false, source_id: $source_id,
                is_nominee: $is_nominee,
                country: $country, founded: null, revenue: null,
                description: null, wikidata_id: null,
                hq_address: $hq_address, hq_city: $hq_city, hq_country: $hq_country,
                hq_street: $hq_street, hq_postcode: $hq_postcode
            })
            """,
            id=entity_id, name=name, name_norm=name_norm,
            cred=credibility_score, type=entity_type, cik=cik, lei=lei, source_id=source_id,
            country=country, **_hq_params(headquarters),
            search_text=_search_text(name, None, aliases), aliases=aliases,
            is_nominee=is_nominee_name(name),
        )
        return _record_touched_entity(entity_id)


def _upsert_person_by_name(full_name: str, source_id: str | None = None) -> str:
    """
    Find or create a Person node matched by full_name.

    SEC EDGAR investor filings use LAST FIRST word order, while Form 3/4
    executive filings use FIRST LAST order. For two-word names this causes
    duplicate nodes (e.g. "Brin Sergey" and "Sergey Brin").  We resolve
    this by also trying the reversed form before creating a new node, and
    storing whichever form already exists if found.
    """
    parts = full_name.strip().split()
    reversed_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else None

    first_name, last_name = parse_full_name(full_name)
    with db.get_session() as session:
        # 1. Exact match
        rec = session.run(
            "MATCH (p:Person {full_name: $name}) RETURN p.id AS id LIMIT 1",
            name=full_name,
        ).single()
        if rec:
            return _record_touched(rec["id"])

        # 2. Reversed two-word form — catches "Brin Sergey" when "Sergey Brin"
        #    already exists (or vice-versa)
        if reversed_name:
            rec = session.run(
                "MATCH (p:Person {full_name: $name}) RETURN p.id AS id LIMIT 1",
                name=reversed_name,
            ).single()
            if rec:
                return _record_touched(rec["id"])

        person_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, nationality: '', description: '',
                wikidata_id: null, verified: false, source_id: $source_id,
                alias: [], nationalities: [], search_text: $search_text
            })
            """,
            id=person_id, first=first_name, last=last_name, full=full_name,
            search_text=_person_search_text(full_name, None),
            source_id=source_id,
        )
        return _record_touched(person_id)
