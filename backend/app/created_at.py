"""When a node entered the graph — `created_at` on every Entity and Person.

Every creation site stamps it (the scrape-path CREATEs, the manual endpoints,
the federation import, the bulk writer's UPSERT with COALESCE so a re-import
is not a birth) and a merge keeps the earlier of the two
(`maintenance._merge_entity_props`, `persons.merge_person_records`). Until
2026-10-24 no node carried a creation time at all — 0 of 6,138 on dev — so
"new this week" and any age-based reading had nothing to stand on.

`backfill` is the one-off for the nodes written before then. It is honest
about what it knows: the oldest claim that names the node (claims have had
`first_seen_at` since they exist), else the node's own `last_scraped_at`,
else the oldest edge touching it (a 13D group member hangs on a RELATED_TO
edge alone — no claim names it, and the node itself was never scraped; 19
dev nodes on 2026-10-25), else nothing — the day a bulk import ran is not
recorded anywhere, and a guessed date would be read as a fact.
"""
from __future__ import annotations

import logging


log = logging.getLogger(__name__)

_WRITE_CHUNK = 500


def earliest(*stamps: str | None) -> str | None:
    """The earlier of two (or more) ISO stamps, None when none is known — the
    rule a merge applies: the merged node entered the graph when the first of
    its parts did. Decided in Python on purpose: ArcadeDB's Cypher coerces a
    datetime-looking string to a temporal value when it returns or compares
    it (`$a < e.created_at` yields null, a RETURN gives '2026-03-01T00:00Z'
    for a stored '2026-03-01T00:00:00+00:00'), so the comparison must not
    live in a Cypher CASE. SQL returns the stored string as written."""
    known = [str(s) for s in stamps if s]
    return min(known) if known else None
