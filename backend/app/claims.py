"""Per-source assertions behind an edge.

An `OWNS` edge answers "who owns this, and how much" with a single value, which
is what the graph traversals need. But several sources routinely assert the same
relationship with different numbers — GLEIF and Companies House will disagree
about a stake, and both are right about what their register says. The edge can
only hold one answer, so the others used to be lost: the second writer simply
overwrote the first, and the source attribution shown in the UI was reconstructed
by guessing from which identifier fields happened to be populated.

A `Claim` records what one source said about one relationship. The edge stays as
it was — the fast, single, current-best answer — and the claims sit beside it as
the evidence:

    (:Entity)-[:OWNS {stake_percent: 60}]->(:Entity)     <- traversals read this
    (:Claim {kind:'owns', from_id, to_id, stake_percent: 60, source_id:'gleif'})
    (:Claim {kind:'owns', from_id, to_id, stake_percent: 75, source_id:'ch-psc'})

Claims are keyed on `claim_key` — a digest of (kind, from_id, to_id, source_id) —
with a UNIQUE index, so a source re-asserting the same relationship updates its
own claim rather than accumulating a new row on every import. That is what makes
re-imports safe here even though the edges themselves still need a dedup pass.

Which claim wins is decided by `best_claim`: highest credibility, then most
recent source_date. Writers apply that to the edge in the same call that records
the claim, so the edge and the evidence cannot drift apart.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

# Kinds of relationship a claim can be about. Each maps to an edge type.
KIND_OWNS = "owns"
KIND_ROLE = "role"
KIND_SUCCESSION = "succession"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim_key(kind: str, from_id: str, to_id: str, source_id: str) -> str:
    """Stable identity for "what this source says about this relationship".

    Digested rather than concatenated because the parts are registry data of
    unbounded length and the result is a UNIQUE index key. Collisions are not a
    practical concern at sha1's width for this cardinality.

    Each part is length-prefixed rather than simply joined by a separator. With a
    plain `a|b|c`, an id containing the separator makes the encoding ambiguous —
    ("A|B", "C") and ("A", "B|C") produce the same string, so two genuinely
    different claims would collide on the UNIQUE index and silently overwrite
    each other. Ids come from external registers (`lei:…`, `gb-coh:…`, BODS
    statement ids); we do not get to assume which characters they avoid.
    """
    parts = (kind, from_id, to_id, source_id)
    raw = "|".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def claim_props(
    *,
    kind: str,
    from_id: str,
    to_id: str,
    source_id: str,
    stake_percent: float | None = None,
    voting_power_pct: float | None = None,
    ownership_type: str | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source_url: str | None = None,
    source_date: str | None = None,
    credibility_score: int = 80,
    share_class: str | None = None,
    shares: int | None = None,
    shares_outstanding: int | None = None,
    voting_shares: int | None = None,
    filing_type: str | None = None,
) -> dict:
    """The property bag for one claim, ready to UPSERT on `claim_key`.

    `first_seen_at` is deliberately absent: it must survive later updates, so the
    writers set it with COALESCE against the stored value rather than passing it
    in here, where it would overwrite on every re-import.
    """
    return {
        "claim_key": claim_key(kind, from_id, to_id, source_id),
        "kind": kind,
        "from_id": from_id,
        "to_id": to_id,
        "source_id": source_id,
        "stake_percent": stake_percent,
        "voting_power_pct": voting_power_pct,
        "ownership_type": ownership_type,
        "role": role,
        "since": since,
        "until": until,
        "source_url": source_url,
        "source_date": source_date,
        "credibility_score": credibility_score,
        # The counts behind the percentages — a claim that records 8.05% but
        # not the 159,121,937 shares it came from cannot be rechecked, which
        # was the whole argument for storing counts on the edge.
        "share_class": share_class,
        "shares": shares,
        "shares_outstanding": shares_outstanding,
        "voting_shares": voting_shares,
        # The record KIND behind the assertion — the Sources panel shows it as
        # "SEC EDGAR · 13F", which tells a reader whose rulebook to read.
        "filing_type": filing_type,
        "last_seen_at": now_iso(),
    }


def record_claim(**kwargs) -> None:
    """Write one claim, for the incremental scrapers (bulk imports batch instead).

    Best-effort: a scrape that succeeded must not be reported as failed because
    the evidence row could not be written. The edge is still there, and the next
    run re-asserts the claim — losing provenance is bad, losing the fact is worse.

    Kept out of the module's import-time dependencies: `run_sql` is imported here
    so `claims` stays importable by pure-logic tests without a database layer.
    """
    from app.db.arcadedb import run_sql

    props = claim_props(**kwargs)
    if not props["source_id"]:
        return
    sets = ", ".join(f"{name} = :{name}" for name in props)
    try:
        run_sql(
            f"UPDATE Claim SET {sets}, first_seen_at = COALESCE(first_seen_at, :last_seen_at) "
            f"UPSERT WHERE claim_key = :claim_key",
            props,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not record %s claim %s->%s from %s: %s",
            props["kind"], props["from_id"], props["to_id"], props["source_id"], exc)


def claims_for(from_id: str | None = None, to_id: str | None = None,
               kind: str | None = None) -> list[dict]:
    """Every recorded assertion about a relationship, most credible first."""
    from app.db.arcadedb import run_sql

    where, params = [], {}
    for field, value in (("from_id", from_id), ("to_id", to_id), ("kind", kind)):
        if value is not None:
            where.append(f"{field} = :{field}")
            params[field] = value
    if not where:
        return []
    try:
        rows = run_sql(f"SELECT FROM Claim WHERE {' AND '.join(where)}", params)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("could not read claims: %s", exc)
        return []
    cleaned = [{k: v for k, v in row.items() if not k.startswith("@")} for row in rows]
    return sorted(cleaned, key=_rank, reverse=True)


def _rank(claim: dict) -> tuple:
    """Sort key: most credible first, then most recently published.

    `source_date` is an ISO-8601 string, so lexicographic order is chronological.
    A missing date sorts oldest rather than crashing the comparison — an undated
    claim should lose a tie, not win it by accident.
    """
    return (
        int(claim.get("credibility_score") or 0),
        str(claim.get("source_date") or ""),
    )


def best_claim(claims: list[dict]) -> dict | None:
    """The claim whose values the edge should carry.

    Highest credibility wins, ties broken by the most recent source_date. Note
    this deliberately does *not* prefer the claim with a stake percentage over
    one without: a more credible source saying "owns, amount undisclosed" is a
    better description of what is known than a less credible source's number.
    """
    if not claims:
        return None
    return max(claims, key=_rank)


def edge_values_from(claims: list[dict]) -> dict:
    """The subset of the winning claim that belongs on the edge."""
    winner = best_claim(claims)
    if not winner:
        return {}
    return {
        "stake_percent": winner.get("stake_percent"),
        "voting_power_pct": winner.get("voting_power_pct"),
        "ownership_type": winner.get("ownership_type"),
        "since": winner.get("since"),
        "until": winner.get("until"),
        "source_id": winner.get("source_id"),
        "source_url": winner.get("source_url"),
        "source_date": winner.get("source_date"),
        "credibility_score": winner.get("credibility_score"),
    }


def migrate_claims(dead_id: str, keep_id: str) -> int:
    """Re-point the claims of a merged-away node at its survivor.

    A claim's key is a hash of (kind | from | to | source), so this cannot be a
    simple UPDATE: rewriting an endpoint changes the key. Each claim is re-keyed
    against the survivor and UPSERTed — an existing claim the survivor already
    holds for the same (kind, pair, source) wins, because it describes the same
    assertion — and the old rows are deleted.

    Without this, every merge orphaned the dead node's claims: the surviving
    edges existed, `claims_for()` found nothing for them, and the merged
    company showed as uncorroborated however many sources had asserted it.
    """
    from app.db.arcadedb import run_sql

    moved = 0
    for end in ("from_id", "to_id"):
        rows = run_sql(f"SELECT FROM Claim WHERE {end} = :d", {"d": dead_id})
        for r in rows:
            props = {k: r.get(k) for k in (
                "kind", "from_id", "to_id", "source_id", "stake_percent",
                "voting_power_pct", "ownership_type", "role", "since", "until",
                "source_url", "source_date", "credibility_score", "last_seen_at",
            )}
            props[end] = keep_id
            props["claim_key"] = claim_key(props["kind"], props["from_id"],
                                           props["to_id"], props["source_id"])
            sets = ", ".join(f"{k} = :{k}" for k in props)
            run_sql(f"UPDATE Claim SET {sets} UPSERT WHERE claim_key = :claim_key",
                    props)
            moved += 1
        run_sql(f"DELETE FROM Claim WHERE {end} = :d", {"d": dead_id})
    return moved
