"""Which vertex type a node id lives in — so every Cypher anchor can name it.

An anchor without a label — ``(n {id})`` instead of ``(n:Entity {id})`` —
cannot use a per-type index: ArcadeDB has to scan EVERY vertex type for the id. On the dev graph that
is a few thousand rows and invisible; on the full import (14.2M entities + 13.8M
persons) the same match ran for more than 400 seconds while the labelled form
took 10–70 ms. The profile endpoint did exactly that in ``_voting_groups_of``
and answered HTTP 500 at the 60 s timeout for every company, on a database
whose direct queries were fine.

The rule, enforced by ``tests/test_cypher_anchors.py``: an anchor by id names
its label. When the caller does not know which — a manual OWNS write, where the
owner may be a person or a company — it asks here first: two indexed point
reads, Entity then Person, which is what the scan was for.
"""
from __future__ import annotations

from typing import Callable

NODE_LABELS = ("Entity", "Person")


def node_label(node_id: str, session=None, query: Callable | None = None) -> str | None:
    """The label — ``Entity`` or ``Person`` — holding ``node_id``, or None.

    Pass either an open ``session`` (``session.run(cypher, **params)``) or a
    ``query`` callable with the ``run_query(cypher, params)`` signature.
    """
    if not node_id:
        return None
    for label in NODE_LABELS:
        cypher = f"MATCH (n:{label} {{id: $id}}) RETURN n.id AS id LIMIT 1"
        if session is not None:
            hit = session.run(cypher, id=node_id).single()
        elif query is not None:
            rows = query(cypher, {"id": node_id})
            hit = rows[0] if rows else None
        else:
            raise TypeError("node_label needs a session or a query callable")
        if hit:
            return label
    return None


def label_or_entity(label: str | None) -> str:
    """Coerce a label from data (a snapshot's ``kind``, a ``labels(n)[0]``
    result) to one of the two vertex types; anything else is a company."""
    return label if label in NODE_LABELS else "Entity"
