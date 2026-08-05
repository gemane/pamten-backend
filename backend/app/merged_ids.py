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
