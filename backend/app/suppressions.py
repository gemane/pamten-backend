"""
Suppression overlay — a moderator's decision that a scraped edge is wrong and
should not be shown, kept separate from the scraped data so it survives
re-scrapes (Phase-B resolution of a verification flag). Enforced at **read time**:
the read endpoints load the small suppression set and drop matching edges, so a
suppressed relationship never surfaces even if a later import recreates it.

A suppression is keyed by the edge's natural key — the same (from_id, to_id
[, role]) the flags and the importer use.
"""


def load_keys(session) -> set[tuple]:
    """All active suppressions as a set of (target_kind, from_id, to_id, role)
    tuples (role is '' for OWNS). The set is small — a moderator-curated list —
    so one scan per request is cheap."""
    keys: set[tuple] = set()
    for rec in session.run(
        "MATCH (s:Suppression) RETURN s.target_kind AS tk, s.from_id AS f, "
        "s.to_id AS t, s.role AS role"
    ):
        keys.add((rec.get("tk"), rec.get("f"), rec.get("t"), rec.get("role") or ""))
    return keys


def is_suppressed(keys: set[tuple], target_kind: str, from_id, to_id, role: str = "") -> bool:
    return (target_kind, from_id, to_id, role or "") in keys


def load_suppressed_nodes(session) -> set:
    """ids of nodes (Entity/Person) a moderator has suppressed. Such a node is
    hidden everywhere at read time — from search, its own profile, and as a
    related node on other profiles — without being deleted, so un-suppress
    restores it."""
    ids: set = set()
    for rec in session.run(
        "MATCH (s:Suppression) WHERE s.target_kind = 'entity' OR s.target_kind = 'person' "
        "RETURN s.node_id AS nid"
    ):
        nid = rec.get("nid")
        if nid:
            ids.add(nid)
    return ids
