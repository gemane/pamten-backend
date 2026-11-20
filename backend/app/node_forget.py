"""
What a deleted node leaves behind, removed with it (findings item 129).

`DETACH DELETE` takes a node and its edges. Everything else that names the node
does so by its **id**, in a row no edge holds — the claims the sources made
about it, its stake history, its industry codes, the forwarding addresses of
the ids it had before, the merge log (which keeps both names in plain text),
the flags raised on it with their free-text notes, the pins — and, outside the
database, its portrait or logo in the object store. Until this module the two
delete routes left all of it: a deleted person was still described, row by
row, in a database that no longer showed them.

**Suppressions are kept, on purpose.** A delete does not last — the next import
writes a record with a register id again — and the suppression is the only
thing that keeps it from showing. Deleting it with the node would un-hide the
record on its return.

Every column read here is indexed (`app.db.schema`), so each statement is a
lookup, not a scan, at the size of a full import too.
"""
import logging

log = logging.getLogger(__name__)

#: rows that name a node by id and go with it: (type, the columns that hold the id)
ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Claim",        ("from_id", "to_id")),
    ("StakeHistory", ("from_id", "to_id")),
    ("IndustryCode", ("entity_id",)),
    ("Flag",         ("node_id", "from_id", "to_id")),
    ("Pin",          ("from_id", "to_id")),
    ("MergeLog",     ("keep_id", "dup_id")),
)

#: what the media store calls the two kinds
_MEDIA_KIND = {"Entity": "entity", "Person": "person"}


def _sql(command: str, params: dict | None = None) -> list[dict]:
    from app.db.arcadedb import run_sql
    return run_sql(command, params or {}) or []


def _count(rows) -> int:
    """The row count an ArcadeDB DELETE / UPDATE reports (`[{"count": n}]`)."""
    return sum(int(r.get("count") or 0) for r in rows or [] if isinstance(r, dict))


def earlier_ids(node_id: str) -> list[str]:
    """The ids that forward to this node: the losers of the merges it survived
    and the id it had before it learned a register's. The merge log still
    names a merge by them (it is written before a later re-keying)."""
    return sorted({r["old_id"] for r in _sql("SELECT old_id FROM MergedId WHERE new_id = :id", {"id": node_id})
                   if r.get("old_id")})


def forget_node(label: str, node_id: str) -> dict:
    """Remove every stored row that names ``node_id`` — call it after the node
    itself is deleted. Returns what went, by type. Safe to repeat: a second
    call finds nothing."""
    from app import media, merge_undo
    from app.merged_ids import invalidate_forwarding_cache

    if not node_id:
        return {}
    ids = [node_id, *earlier_ids(node_id)]
    removed: dict[str, int] = {}
    for table, cols in ROWS:
        n = 0
        for col in cols:
            for i in ids:
                n += _count(_sql(f"DELETE FROM {table} WHERE {col} = :id", {"id": i}))
        removed[table] = n
    # the undo's stored copies of a merge this node was part of
    removed["MergeSnapshot"] = sum(merge_undo.forget(i) for i in ids)
    # the run log keeps its line (what was scraped, when), not the link to a node that is gone
    removed["ScrapeRun"] = _count(_sql("UPDATE ScrapeRun REMOVE entity_id WHERE entity_id = :id", {"id": node_id}))
    # last: the forwarding addresses, which the lines above read
    removed["MergedId"] = (_count(_sql("DELETE FROM MergedId WHERE new_id = :id", {"id": node_id}))
                           + _count(_sql("DELETE FROM MergedId WHERE old_id = :id", {"id": node_id})))
    invalidate_forwarding_cache()
    kind = _MEDIA_KIND.get(label)
    if kind:
        for i in ids:
            try:
                media.forget(kind, i)
            except Exception as exc:  # noqa: BLE001 - the rows are gone; a store outage must not undo that
                log.warning("stored image of %s %s not removed: %s", kind, i, exc)
    return removed
