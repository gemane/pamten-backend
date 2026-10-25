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

from app.db.arcadedb import run_sql, run_sqlscript

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


def _oldest_claim_dates(ids: list[str]) -> dict[str, str]:
    """{node id: oldest claim first_seen_at} over both ends of the claims that
    name these ids — two indexed grouped reads per page, never a scan."""
    oldest: dict[str, str] = {}
    for col in ("to_id", "from_id"):
        rows = run_sql(f"SELECT {col} AS i, min(first_seen_at) AS t FROM Claim "
                       f"WHERE {col} IN :ids AND first_seen_at IS NOT NULL GROUP BY {col}",
                       {"ids": ids})
        for r in rows:
            i, t = r.get("i"), r.get("t")
            if i and t and (i not in oldest or str(t) < oldest[i]):
                oldest[i] = str(t)
    return oldest


def _oldest_edge_date(rid: str) -> str | None:
    """The oldest `last_scraped_at` of any edge touching the node, in or out,
    of any type — one read per node, and only for a node nothing else dated."""
    rows = run_sql(f"SELECT min(last_scraped_at) AS t FROM (SELECT expand(bothE()) FROM {rid})")
    t = (rows or [{}])[0].get("t")
    return str(t) if t else None


def backfill(apply: bool = True, labels: tuple[str, ...] = ("Entity", "Person", "VotingGroup")) -> dict:
    """Give every node without `created_at` the oldest date anything recorded
    about it. Walks the id index (app.db.paging). Returns per-label counts:
    scanned, missing, from_claims, from_scrape, from_edges, unknown, written."""
    from app.db import paging
    out: dict = {"applied": bool(apply)}
    for label in labels:
        c = {"scanned": 0, "missing": 0, "from_claims": 0, "from_scrape": 0, "from_edges": 0,
             "unknown": 0, "written": 0}
        for page in paging.iter_id_pages(label, columns="id, @rid AS rid, created_at, last_scraped_at"):
            c["scanned"] += len(page)
            missing = [r for r in page if not r.get("created_at") and r.get("rid")]
            if not missing:
                continue
            c["missing"] += len(missing)
            claims = _oldest_claim_dates([r["id"] for r in missing])
            values: list[tuple[str, str]] = []
            for r in missing:
                when = claims.get(r["id"])
                if when:
                    c["from_claims"] += 1
                elif r.get("last_scraped_at"):
                    when = str(r["last_scraped_at"])
                    c["from_scrape"] += 1
                else:
                    when = _oldest_edge_date(str(r["rid"]))
                    if not when:
                        c["unknown"] += 1
                        continue
                    c["from_edges"] += 1
                values.append((str(r["rid"]), when))
            if not apply:
                continue
            for i in range(0, len(values), _WRITE_CHUNK):
                chunk = values[i:i + _WRITE_CHUNK]
                stmts = "\n".join(f"UPDATE {rid} SET created_at = :v{k};" for k, (rid, _) in enumerate(chunk))
                run_sqlscript(stmts, {f"v{k}": when for k, (_, when) in enumerate(chunk)})
                c["written"] += len(chunk)
        out[label] = c
    log.info("created_at backfill: %s", out)
    return out
