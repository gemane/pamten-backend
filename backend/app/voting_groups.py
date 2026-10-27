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


log = logging.getLogger(__name__)

#: What an old group carries, copied as it stands (`type` stays
#: `voting_group`: the API's node kind, on the new vertex too)
_NODE_FIELDS = ("id", "name", "name_normalized", "search_text", "description", "type",
                "source_id", "created_at", "last_scraped_at", "member_keys")
