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


class Guard:
    """Both suppression sets, loaded once for a request — what every public
    read asks before it shows a node or an edge (findings item 130).

    The filter used to be two loads and an `if` written out in each route that
    remembered it; the routes that did not (the plain record reads, the
    timeline, the stake series, the portrait) showed a suppressed person to
    anyone with the id. One object with two questions is harder to half-apply.
    """

    def __init__(self, nodes: set, keys: set[tuple]):
        self.nodes, self.keys = nodes, keys

    def node(self, node_id) -> bool:
        """Is this person / company suppressed?"""
        return bool(node_id) and node_id in self.nodes

    def edge(self, target_kind: str, from_id, to_id, role: str = "") -> bool:
        """Is this holding / seat hidden — itself suppressed, or either end?"""
        return (self.node(from_id) or self.node(to_id)
                or is_suppressed(self.keys, target_kind, from_id, to_id, role))

    def visible(self, rows, key="id") -> list:
        """The rows whose node is not suppressed (`key` names the id field)."""
        return [r for r in rows if not self.node(r.get(key))]


def guard(session) -> Guard:
    return Guard(load_suppressed_nodes(session), load_keys(session))


def node_hidden(node_id) -> bool:
    """One node, asked outside a session a caller already holds."""
    from app.database import db
    with db.get_session() as session:
        return node_id in load_suppressed_nodes(session)
