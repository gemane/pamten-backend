"""A 13D filing group is its own vertex type — `VotingGroup` — since
2026-10-26, with `MEMBER_OF` for its parties and its OWNS edge to the company
it holds. Until then a group was an Entity of type `voting_group`, which every
query that assumes an entity is a legal person had to fence off: eight did
(`anchors.not_a_voting_group`, now gone), the rest forgot — the 38 groups on
dev were the only entities without a nominee flag, were counted in the entity
totals and offered to every heal that walks the entities.

The heal here converts a database written before: each old group becomes a
VotingGroup under the same id, its `RELATED_TO {group_member}` edges become
`MEMBER_OF`, its OWNS edge is recreated from the new vertex with every schema
property, and the old Entity is deleted. Claims and stake history are keyed by
id and need nothing. Idempotent: a second run finds no old group.
"""
import logging

from app.db.arcadedb import run_command, run_query, run_sql
from app.db.anchors import label_or_entity

log = logging.getLogger(__name__)

#: What an old group carries, copied as it stands (`type` stays
#: `voting_group`: the API's node kind, on the new vertex too)
_NODE_FIELDS = ("id", "name", "name_normalized", "search_text", "description", "type",
                "source_id", "created_at", "last_scraped_at", "member_keys")


def heal_voting_group_type(apply: bool = False) -> dict:
    """Move every Entity of type `voting_group` to the VotingGroup vertex
    type (see the module docstring). Dry run unless `apply`: counts the
    groups, their memberships and holdings, names the groups."""
    from app.scraper.edge_schema import OWNS_PROPS, edge_return_clause
    from app.scraper.maintenance import _carry_edge
    groups = run_sql(f"SELECT {', '.join(_NODE_FIELDS)} FROM Entity WHERE type = 'voting_group'")
    out: dict = {"applied": bool(apply), "groups": 0, "members": 0, "holdings": 0, "names": []}
    for g in groups or []:
        gid = g["id"]
        members = run_query(
            "MATCH (m)-[r:RELATED_TO]->(g:Entity {id: $gid}) WHERE r.relation = 'group_member' "
            "RETURN m.id AS mid, labels(m)[0] AS mlabel, r.source_id AS source_id, "
            "r.last_scraped_at AS last_scraped_at", {"gid": gid})
        holdings = run_query(
            f"MATCH (g:Entity {{id: $gid}})-[r:OWNS]->(t:Entity) "
            f"RETURN t.id AS tid, {edge_return_clause('r', OWNS_PROPS)}", {"gid": gid})
        out["groups"] += 1
        out["members"] += len(members)
        out["holdings"] += len(holdings)
        out["names"].append(f"{g.get('name')} ({len(members)} parties, {len(holdings)} holdings)")
        if not apply:
            continue
        props = {k: g.get(k) for k in _NODE_FIELDS}
        run_command("CREATE (g:VotingGroup {" + ", ".join(f"{k}: ${k}" for k in _NODE_FIELDS) + "})", props)
        for m in members:
            mlabel = label_or_entity(m.get("mlabel"))
            run_command(
                f"MATCH (m:{mlabel} {{id: $mid}}), (g:VotingGroup {{id: $gid}}) "
                "CREATE (m)-[:MEMBER_OF {source_id: $sid, last_scraped_at: $t}]->(g)",
                {"mid": m["mid"], "gid": gid, "sid": m.get("source_id"), "t": m.get("last_scraped_at")})
        for h in holdings:
            _carry_edge("OWNS", OWNS_PROPS, "(a:VotingGroup {id: $k})", "(t:Entity {id: $tid})",
                        {"k": gid, "tid": h["tid"]}, h)
        # the old node goes with its edges: the memberships and holdings now
        # hang on the new vertex, the claims never hung on the node
        run_command("MATCH (g:Entity {id: $gid}) DETACH DELETE g", {"gid": gid})
    log.info("voting group type heal: %s", {k: v for k, v in out.items() if k != "names"})
    return out
