"""
Shared graph-write instrumentation for scrapers — the first slice of the scraper
write layer (Phase 1 of the multi-scraper refactor).

Right now it holds the **touched-node collectors + the post-scrape auto-dedup
framework** every scraper reuses: a scrape entry point wrapped in `@with_autodedup`
collects the persons/entities it creates or updates (recorded via `record_touched` /
`record_touched_entity` from inside the upsert helpers) and, on completion, runs the
scoped high-confidence auto-merge over just those — never a full-DB scan.

The entity/person/owns/role upsert helpers themselves will move here next, once
they're generalized off their per-scraper hardcoded credibility.
"""
import contextvars
import functools
import logging
from datetime import datetime, timezone

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
        return result
    return wrapper
