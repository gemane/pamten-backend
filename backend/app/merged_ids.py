"""
Redirects for ids that a merge folded away.

Merging two duplicate nodes deletes one of them, and its id is not private
bookkeeping: it appears in shared links, in a mobile client's cache, and in
**federation peers' copies of our data** — a peer that pulled the losing id and
pulls again would otherwise find nothing and recreate the duplicate we just
merged. So every merge leaves a forwarding address.

Stored as its own ``MergedId`` vertex rather than a list property on the
survivor: resolving `old_id` has to be an indexed equality lookup. A
``also_known_ids CONTAINS $id`` predicate cannot use an index and would scan the
whole Entity type — 4.2M rows on the dev database — on every miss.

Chains are collapsed at write time (A→B, then B→C rewrites A→C) so a lookup is
always one hop. `resolve_current_id` still follows a short chain defensively, in
case a row was written by an older version or by hand.
"""
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# A chain longer than this means the data is malformed (or cyclic); stop rather
# than loop forever.
_MAX_HOPS = 5


# Forwarding map for the IMPORTERS: a merged-away id, resolved to its final
# survivor. Cached briefly because a bulk import addresses nodes by
# `lei:{LEI}` millions of times and no merges happen mid-import (auto-dedup
# runs after); a 60-second lag on a fresh merge is invisible next to that.
# "at" None = empty/invalidated. Never 0.0: monotonic() is uptime, so on a
# fresh container `now - 0.0 < TTL` marks an empty cache fresh and
# invalidation is a no-op for the first minute of process life.
_FWD_CACHE: dict = {"at": None, "map": {}}
_FWD_TTL = 60.0


def _forwarding_map() -> dict:
    """Every merged-away id → its FINAL survivor (chains pre-resolved).

    Loaded once per TTL from the whole MergedId table (small: one row per
    historical merge, not per node). Chains are collapsed here so a lookup is
    a single dict hit — the same terminal `resolve_current_id` reaches by
    hopping, but importers can't afford a per-node query."""
    import time
    from app.db.arcadedb import run_sql
    now = time.monotonic()
    if _FWD_CACHE["at"] is not None and now - _FWD_CACHE["at"] < _FWD_TTL:
        return _FWD_CACHE["map"]
    direct: dict = {}
    try:
        for r in run_sql("SELECT old_id, new_id FROM MergedId"):
            d = dict(r)
            if d.get("old_id") and d.get("new_id"):
                direct[d["old_id"]] = d["new_id"]
    except Exception:  # noqa: BLE001 - fail open: no forwarding is safe, and
        direct = {}   # a cached empty map avoids hammering a flaky DB per node
    # Collapse chains to the terminal survivor (guarded against cycles).
    final: dict = {}
    for old in direct:
        seen = {old}
        cur = old
        for _ in range(_MAX_HOPS):
            nxt = direct.get(cur)
            if not nxt or nxt in seen:
                break
            cur = nxt
            seen.add(cur)
        if cur != old:
            final[old] = cur
    _FWD_CACHE["at"] = now
    _FWD_CACHE["map"] = final
    return final


def canonical_id(node_id: str | None) -> str | None:
    """The id a node lives under now: the survivor if ``node_id`` was merged
    away, else ``node_id`` unchanged. For importers that address nodes by a
    derived id (`lei:{LEI}`) and must not resurrect what a merge folded away."""
    if not node_id:
        return node_id
    return _forwarding_map().get(node_id, node_id)


def invalidate_forwarding_cache() -> None:
    """Drop the cache — call right after a merge so an importer in the same
    process sees it immediately (the TTL handles cross-process staleness)."""
    _FWD_CACHE["at"] = None


# One definition of the write, used by both entry points below. The person merge
# runs inside a session; the entity merges in scraper/maintenance.py run through
# the module-level run_command helper instead.
_REPOINT_CHAIN = "MATCH (m:MergedId {new_id: $old}) SET m.new_id = $new, m.at = $now"
_UPSERT_REDIRECT = """
    MERGE (m:MergedId {old_id: $old})
    SET m.new_id = $new, m.kind = $kind, m.at = $now,
        m.id = COALESCE(m.id, $row_id)
"""


def _params(old_id: str, new_id: str, kind: str) -> dict:
    return {
        "old": old_id, "new": new_id, "kind": kind,
        "now": datetime.now(timezone.utc).isoformat(),
        "row_id": str(uuid.uuid4()),
    }


def record_merge(session, old_id: str, new_id: str, kind: str = "Entity") -> None:
    """Leave a forwarding address from ``old_id`` to ``new_id``.

    Also re-points any existing redirect that led to ``old_id``, so a node merged
    twice still resolves in a single hop.
    """
    if not old_id or not new_id or old_id == new_id:
        return
    p = _params(old_id, new_id, kind)
    # Anything that pointed at the node we just merged away now points onward.
    session.run(_REPOINT_CHAIN, old=p["old"], new=p["new"], now=p["now"])
    session.run(_UPSERT_REDIRECT, **p)
    invalidate_forwarding_cache()


def record_merge_sql(old_id: str, new_id: str, kind: str = "Entity") -> None:
    """``record_merge`` for callers outside a session (scraper/maintenance).

    Best-effort: the nodes are already merged by the time this runs, so a failure
    to write the forwarding address must not fail the merge itself — it degrades
    to the old behaviour (a dead id) rather than leaving the graph half-merged.
    """
    if not old_id or not new_id or old_id == new_id:
        return
    from app.db.arcadedb import run_command

    p = _params(old_id, new_id, kind)
    try:
        run_command(_REPOINT_CHAIN, {"old": p["old"], "new": p["new"], "now": p["now"]})
        run_command(_UPSERT_REDIRECT, p)
        invalidate_forwarding_cache()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record merge redirect %s -> %s: %s", old_id, new_id, exc)


def resolve_current_id(session, old_id: str) -> str | None:
    """The id a merged-away ``old_id`` now lives under, or None if it wasn't merged.

    Callers use this only after a direct lookup misses — an id that still exists
    must never be redirected.
    """
    if not old_id:
        return None
    seen = {old_id}
    current = old_id
    for _ in range(_MAX_HOPS):
        rec = session.run(
            "MATCH (m:MergedId {old_id: $id}) RETURN m.new_id AS new_id LIMIT 1",
            id=current,
        ).single()
        if not rec or not rec["new_id"]:
            break
        current = rec["new_id"]
        if current in seen:            # cycle — bail out rather than spin
            log.warning("merged-id cycle detected at %s", current)
            return None
        seen.add(current)
    return current if current != old_id else None
