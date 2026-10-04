"""
Repairs for data written before the time-travel fixes (2026-10).

Each heal corrects what one fixed bug left in the graph, and is safe to run
again: it only changes rows that still have the old shape. They walk the
CLAIMS of the source concerned — every scraped edge has one, and the Claim
type is small and keyed — and fix each pair through labelled, index-backed
lookups, never a scan of the OWNS edge type, which would not finish under the
DB proxy's 60 s timeout on a full database.

The bulk sources (GLEIF, UK PSC) are not healed here: the rebuild re-imports
them with the fixed code.
"""
from __future__ import annotations

from app.db.anchors import node_label
from app.db.arcadedb import run_command, run_query, run_sql


def _source_id(name: str) -> str | None:
    rows = run_query("MATCH (s:Source {name: $n}) RETURN s.id AS id", {"n": name})
    return rows[0]["id"] if rows else None


def _claims(source_id: str, kind: str) -> list[dict]:
    return run_sql("SELECT from_id, to_id, role, filing_type, since, since_basis, until, "
                   "source_date FROM Claim WHERE source_id = :s AND kind = :k",
                   {"s": source_id, "k": kind})


def heal_sec_dates(dry_run: bool = False) -> dict:
    """What the SEC writer got wrong about time, before Time travel 2/6:

    - ``amendment``: a 13D/G amendment's file date stored as a STATED start —
      becomes a lower bound (``since_basis = 'amendment'``). Recognised by a
      ``/A`` filing type and a ``since`` equal to the edge's ``source_date``
      (the filing it came from).
    - ``form4``: a Form 3/4's date as the start of an insider's holding —
      the start is removed (the holding is as of that report, not since).
    - ``ended_before_start``: an edge or a seat closed with an end BEFORE its
      start — an old exit or 8-K departure applied to a newer period — is
      reopened; the departure belonged to an earlier period.

    The claims get the same repair as the edges, so the Sources panel agrees.
    """
    sec = _source_id("SEC EDGAR")
    counts = {"amendment": 0, "form4": 0, "ended_before_start": 0, "pairs": 0}
    if not sec:
        return counts
    for c in _claims(sec, "owns"):
        a, b = c.get("from_id"), c.get("to_id")
        if not a or not b:
            continue
        counts["pairs"] += 1
        label = node_label(a, query=run_query) or "Entity"
        match = f"MATCH (x:{label} {{id: $a}})-[r:OWNS]->(y:Entity {{id: $b}}) WHERE r.source_id = $s"
        ft = (c.get("filing_type") or "").upper()
        if ft.endswith("/A"):
            n = run_query(f"{match} AND r.since_basis IS NULL AND r.since = r.source_date "
                          "RETURN count(r) AS n", {"a": a, "b": b, "s": sec})[0]["n"]
            if n and not dry_run:
                run_command(f"{match} AND r.since_basis IS NULL AND r.since = r.source_date "
                            "SET r.since_basis = 'amendment'", {"a": a, "b": b, "s": sec})
            counts["amendment"] += n
        elif ft in ("FORM 4", "4", "3", "FORM 3"):
            n = run_query(f"{match} AND r.since IS NOT NULL AND r.since = r.source_date "
                          "RETURN count(r) AS n", {"a": a, "b": b, "s": sec})[0]["n"]
            if n and not dry_run:
                run_command(f"{match} AND r.since IS NOT NULL AND r.since = r.source_date "
                            "SET r.since = null", {"a": a, "b": b, "s": sec})
            counts["form4"] += n
        n = run_query(f"{match} AND r.until IS NOT NULL AND r.since IS NOT NULL AND r.until < r.since "
                      "RETURN count(r) AS n", {"a": a, "b": b, "s": sec})[0]["n"]
        if n and not dry_run:
            run_command(f"{match} AND r.until IS NOT NULL AND r.since IS NOT NULL AND r.until < r.since "
                        "SET r.until = null", {"a": a, "b": b, "s": sec})
        counts["ended_before_start"] += n
    for c in _claims(sec, "role"):
        a, b = c.get("from_id"), c.get("to_id")
        if not a or not b:
            continue
        match = ("MATCH (x:Person {id: $a})-[r:HAS_ROLE]->(y:Entity {id: $b}) "
                 "WHERE r.until IS NOT NULL AND r.since IS NOT NULL AND r.until < r.since")
        n = run_query(f"{match} RETURN count(r) AS n", {"a": a, "b": b})[0]["n"]
        if n and not dry_run:
            run_command(f"{match} SET r.until = null", {"a": a, "b": b})
        counts["ended_before_start"] += n
    if not dry_run:
        # the claims: the same two repairs, keyed by the claim's own fields
        run_sql("UPDATE Claim SET since_basis = 'amendment' WHERE source_id = :s AND kind = 'owns' "
                "AND since_basis IS NULL AND since IS NOT NULL AND since = source_date "
                "AND filing_type LIKE '%/A'", {"s": sec})
        run_sql("UPDATE Claim SET since = null WHERE source_id = :s AND kind = 'owns' "
                "AND since IS NOT NULL AND since = source_date "
                "AND (filing_type = 'Form 4' OR filing_type = '4' OR filing_type = '3')", {"s": sec})
    return counts
