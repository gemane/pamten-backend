"""
On-demand ("instant"-source) scraping — enrich a company from the instant sources
(Wikidata, SEC EDGAR, OpenCorporates) when a user asks for it, but only when the data is
actually missing or stale, so a normal view never triggers redundant work.

Two pieces:
  * `decide_scrape` — a PURE freshness decision (absent / never-on-demand / stale / deepen
    / force / fresh). No DB, fully unit-testable.
  * `ensure_scrape` — the orchestrator: resolve the query to a DB entity (same ranking as
    /search), decide, and if needed run the enabled **instant** sources (never bulk/GLEIF),
    guarded by a per-target in-flight lock, then return the reloaded profile.
"""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.scraper.graph_writer import _with_autodedup
from app.scraper.mapper import normalize_entity_name

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeDecision:
    should_scrape: bool
    reason: str          # forced | absent | never_on_demand | stale | deepen | fresh | cooldown
    need_depth: int


def _parse_dt(value) -> datetime | None:
    """Parse an ISO timestamp (with a trailing Z or an offset) to an aware datetime."""
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def decide_scrape(entity: dict | None, *, requested_depth: int, force: bool,
                  now: datetime, ttl_days: int | None = None,
                  cooldown_hours: int | None = None) -> ScrapeDecision:
    """Should we scrape, and to what depth? Pure — `entity` is the resolved DB row (or
    None). Branch order: cooldown (force only) → force → absent → never-on-demand → stale →
    deepen → fresh."""
    ttl = settings.SCRAPER_ONDEMAND_TTL_DAYS if ttl_days is None else ttl_days
    cooldown = settings.SCRAPER_ONDEMAND_COOLDOWN_HOURS if cooldown_hours is None else cooldown_hours
    depth = int(requested_depth)
    last = _parse_dt(entity.get("last_scraped_at")) if entity else None
    within_cooldown = (last is not None and cooldown > 0
                       and (now - last) < timedelta(hours=cooldown))
    if force:
        # A forced "Refresh from sources" is still gated by the cooldown: a company scraped
        # within the last `cooldown` hours is served from the DB until the window passes, so
        # users can't hammer the external sources. (Non-force deepen isn't blocked — it never
        # reaches here — so the two-phase depth-1-then-depth-2 enrichment still completes.)
        if within_cooldown:
            return ScrapeDecision(False, "cooldown", depth)
        return ScrapeDecision(True, "forced", depth)
    if entity is None:
        return ScrapeDecision(True, "absent", depth)
    if not entity.get("on_demand_scraped"):
        return ScrapeDecision(True, "never_on_demand", depth)
    if last is None or (now - last) > timedelta(days=ttl):
        return ScrapeDecision(True, "stale", depth)
    if depth > int(entity.get("scrape_depth") or 0):
        return ScrapeDecision(True, "deepen", depth)
    return ScrapeDecision(False, "fresh", depth)


# ── Fruitless searches ────────────────────────────────────────────────────────
#
# The freshness gate protects the sources by looking at the *company*: when it was
# last scraped, how deep. A search that finds nothing has no company to hang that
# on, so nothing was recorded and every repeat of a hopeless query ran every
# source again. "Alphabet" in France has no answer, and asking twice in a row does
# not make one appear.
#
# So a miss is remembered too, under the same key that makes two searches the same
# search — normalised name plus country — and honoured for the same cooldown that
# caps a forced re-scrape.


def _miss_key(query: str, country: str | None) -> str:
    """What makes two searches the same search: the normalised name and the
    country. "Alphabet" in France and "Alphabet" in Germany are different
    questions and must not share an answer."""
    name = normalize_entity_name(query) or (query or "").strip().lower()
    return f"{name}|{(country or '').strip().upper()}"


def _recent_miss(key: str, now: datetime, hours: int) -> str | None:
    """When this search last came back empty, if that was recent enough to still
    count. An expired row is deleted on the way past — the cheapest prune there
    is, and it only ever touches a key someone is asking about."""
    if hours <= 0:
        return None
    from app.db.arcadedb import run_sql

    rows = run_sql("SELECT missed_at FROM ScrapeMiss WHERE key = :k", {"k": key})
    if not rows:
        return None
    at = _parse_dt(rows[0].get("missed_at"))
    if at and (now - at) < timedelta(hours=hours):
        return at.isoformat()
    run_sql("DELETE FROM ScrapeMiss WHERE key = :k", {"k": key})
    return None


def _record_miss(key: str, now: datetime) -> None:
    from app.db.arcadedb import run_sql

    run_sql("UPDATE ScrapeMiss SET key = :k, missed_at = :now UPSERT WHERE key = :k",
            {"k": key, "now": now.isoformat()})


def _clear_miss(key: str) -> None:
    """A search that found something cancels the memory of one that didn't."""
    from app.db.arcadedb import run_sql

    run_sql("DELETE FROM ScrapeMiss WHERE key = :k", {"k": key})


# Serialise scrapes of the same target so a double-click / concurrent request doesn't
# queue a second identical scrape. The 30-day freshness gate is the primary damper; this
# just stops overlap. Keyed by the normalized company name.
_inflight_lock = threading.Lock()
_inflight: set[str] = set()


@_with_autodedup
def _run_instant_sources(query: str, decision: ScrapeDecision, country: str | None = None) -> dict:
    """Run the enabled **instant** sources for `query`; returns
    {status, names_run, target_id}. Kind-driven: iterate the registry, keep only
    `kind=="instant" and enabled()` — this structurally excludes bulk sources (GLEIF) and
    any admin-disabled instant source, and auto-includes future instant sources. On a pure
    `deepen` pass, only depth-aware sources re-run (the rest ignore depth). One source
    failing never sinks the others.

    **Wrapped in `@_with_autodedup` so all sources share ONE dedup scope.** The person
    auto-merge only groups duplicates that were touched *together* in one scope (candidate
    expansion is exact-name only — persons.py `_candidate_persons`), so running Wikidata +
    SEC under a single scope is what merges cross-source pairs like Wikidata "Larry Page"
    ↔ SEC "Page Lawrence". Separate per-source scopes miss them. Mirrors `run_scrape_all`.
    The nested runners (each `@_with_autodedup`) become no-ops for dedup/freshness and feed
    the shared collector; the combined merge + the single freshness stamp run here."""
    from app.scraper.scraper_registry import registered
    from app.scraper.run_log import record_run

    names_run: list[str] = []
    target_id: str | None = None
    for spec in registered():
        if spec.kind != "instant" or not spec.enabled():
            continue
        if decision.reason == "deepen" and not spec.depth_aware:
            continue
        run_depth = decision.need_depth if spec.depth_aware else 0
        try:
            with record_run(spec.name, query) as run:
                res = spec.run(query, run_depth, country)
                if isinstance(res, dict):
                    run["total"] = res.get("total", 0) or 0
                    if res.get("entity_id"):
                        target_id = res["entity_id"]
            names_run.append(spec.name)
        except Exception as exc:  # noqa: BLE001 - one source failing mustn't sink the rest
            log.error("on-demand %s scrape failed for %r: %s", spec.name, query, exc)
    return {"status": "ok", "names_run": names_run, "target_id": target_id}


def ensure_scrape(query: str, depth: int = 1, force: bool = False,
                  country: str | None = None) -> dict:
    """Ensure `query`'s company is present + fresh, scraping the instant sources only when
    the freshness rule says so. Returns {scraped, reason, entity_id, depth_reached,
    sources_run, profile}. Idempotent and safe to call twice (phase-1 depth 1, then
    phase-2 depth 2 → `deepen` runs only depth-aware sources; a third call → `fresh`).

    A search that finds nothing is remembered for the same cooldown, keyed by name
    and country, and repeating it returns `reason: "recently_missed"` without
    touching a source. Otherwise nothing records the attempt — there is no company
    to stamp — and every repeat of a hopeless query runs every source again.

    `country` is the ISO-2 chosen in the search box, and narrows the whole operation:
    the DB lookup that decides freshness resolves within that country, the sources are
    told to reject a match found elsewhere, and the re-resolve afterwards is scoped the
    same way. Without it the German query "Alphabet" would be answered — from the DB or
    from Wikidata — with the company in Mountain View."""
    from app.routers.search import resolve_best_entity, get_full_profile

    depth = max(0, min(int(depth), 3))
    country = (country or "").strip().upper() or None
    entity = resolve_best_entity(query, country)
    decision = decide_scrape(entity, requested_depth=depth, force=force,
                             now=datetime.now(timezone.utc))

    def _profile(eid: str | None) -> dict | None:
        if not eid:
            return None
        try:
            return get_full_profile(eid)
        except Exception:  # noqa: BLE001 - 404 / suppressed → no profile
            return None

    def _served_from_db(reason: str) -> dict:
        eid = entity.get("id") if entity else None
        return {"scraped": False, "reason": reason, "entity_id": eid,
                "depth_reached": int((entity or {}).get("scrape_depth") or 0),
                "sources_run": [], "profile": _profile(eid)}

    if not decision.should_scrape:
        return _served_from_db(decision.reason)      # fresh — serve from DB
    if not settings.SCRAPER_ENABLED:
        return _served_from_db("disabled")           # master switch off — graceful

    now = datetime.now(timezone.utc)
    miss_key = _miss_key(query, country)
    missed_at = _recent_miss(miss_key, now, settings.SCRAPER_ONDEMAND_COOLDOWN_HOURS)
    if missed_at:
        # Asked and answered. Running every source again for a query that just came
        # back empty is exactly the hammering the cooldown exists to prevent — and
        # it is the same answer.
        out = _served_from_db("recently_missed")
        out["missed_at"] = missed_at
        return out

    # Same key as the miss memory: two searches are the same search when the name
    # and the country match. Two countries are two scrapes and must not block
    # each other.
    key = miss_key
    with _inflight_lock:
        if key in _inflight:
            return _served_from_db("in_progress")    # already scraping this target
        _inflight.add(key)
    try:
        ran = _run_instant_sources(query, decision, country)
        names_run, target_id = ran["names_run"], ran["target_id"]
    finally:
        with _inflight_lock:
            _inflight.discard(key)

    if not target_id:                                # re-resolve (node may be new/merged)
        again = resolve_best_entity(query, country)
        target_id = again.get("id") if again else None
    if target_id:
        _clear_miss(miss_key)
    else:
        _record_miss(miss_key, now)

    profile = _profile(target_id)
    depth_reached = decision.need_depth
    if profile and profile.get("entity"):
        depth_reached = int(profile["entity"].get("scrape_depth") or decision.need_depth)
    return {"scraped": True, "reason": decision.reason, "entity_id": target_id,
            "depth_reached": depth_reached, "sources_run": names_run, "profile": profile}
