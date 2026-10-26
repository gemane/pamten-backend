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

Which claim wins is decided by `best_claim`: the official tier, then a stated
stake, then credibility, then the most recent source_date — the same order in
which the incremental writers let one source's answer take over the pair's one
shared edge (`app.scraper.owns_merge`, which on a tie keeps the source already
holding it rather than the newest).
"""
from __future__ import annotations

import hashlib
import re

from app.roles import canonical_role
import logging
from datetime import datetime, timezone

# Kinds of relationship a claim can be about. Each maps to an edge type.
KIND_OWNS = "owns"
KIND_ROLE = "role"
KIND_SUCCESSION = "succession"

#: `filing_type` values of an owns claim read from an acquirer's XBRL
#: business-combination facts (app/scraper/sec_acquisitions.py).
ACQUISITION_FILING_TYPES = frozenset({"10-K XBRL", "10-Q XBRL"})
_ACQUISITION_KEY = "acquisition"

#: SEC EDGAR speaks about one pair through several filing families, and each
#: is its own statement: a Form 4 is the insider's holding of record, a
#: Schedule 13D/G the beneficial stake at or above 5 % (which the edge rule
#: prefers), a 13F the manager's quarterly table, an Exhibit 21 the issuer's
#: subsidiary list, the acquirer's XBRL the deal. One claim per (pair, source)
#: held whichever filing was read last, so the Sources panel showed one "SEC
#: EDGAR" line where the register had made two or three statements (2026-10-26;
#: the acquisition was keyed apart since 2026-10-12 for the same reason). A
#: family is read off the filing type's text alone — no source lookup in a key
#: — and anything that is not an SEC form (GLEIF's "RR", a PSC snapshot) has
#: none, so those keys never changed. A re-read of the same family still
#: updates its own claim: amendments do not pile up.
_SEC_FAMILIES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^(SC\s*)?13[DG]\b", re.I), "13dg"),         # 13D, 13D/A, 13G, 13G/A, SC 13D
    (re.compile(r"^13F\b", re.I), "13f"),
    (re.compile(r"^Form\s*[345]\b", re.I), "insider"),        # Form 3 / 4 / 5
    (re.compile(r"^EX-21\b", re.I), "ex21"),
    (re.compile(r"^EX-8\.1\b", re.I), "ex81"),
)


def filing_family(filing_type: str | None) -> str | None:
    """The SEC filing family a `filing_type` belongs to, or None for a type
    that is not an SEC form (every other source's)."""
    ft = (filing_type or "").strip()
    if not ft:
        return None
    if ft in ACQUISITION_FILING_TYPES:
        return _ACQUISITION_KEY
    for pattern, family in _SEC_FAMILIES:
        if pattern.match(ft):
            return family
    return None


def _key_discriminator(kind: str | None, role: str | None, filing_type: str | None) -> str | None:
    """The part of a claim's identity beyond (kind, pair, source): a role claim's
    canonical role, an owns claim's SEC filing family, else nothing. `claim_props`
    and `key_for` MUST agree on this, or a heal re-keys what a writer wrote."""
    if kind == KIND_ROLE and role:
        return canonical_role(role)
    if kind == KIND_OWNS:
        return filing_family(filing_type)
    return None


def owns_claim_key(from_id: str, to_id: str, source_id: str, filing_type: str | None = None) -> str:
    """The key of a source's owns claim for a pair — the one a writer gave it
    for that filing type (`claim_props`), for a caller that must find it."""
    return claim_key(KIND_OWNS, from_id, to_id, source_id,
                     role_key=_key_discriminator(KIND_OWNS, None, filing_type))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim_key(kind: str, from_id: str, to_id: str, source_id: str,
              role_key: str | None = None) -> str:
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
    parts = [kind, from_id, to_id, source_id]
    if role_key:
        # One claim row PER POSITION for role claims: without this, a source
        # asserting two roles for the same pair overwrote its own row and only
        # the last-written role survived — so per-role corroboration could
        # never be counted. Canonical, so "Director" and "Board Member" are
        # one position across sources. Owns/succession keys are unchanged.
        parts.append(role_key)
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
    denominator_date: str | None = None,
    event_date: str | None = None,
    voting_shares: int | None = None,
    filing_type: str | None = None,
    since_basis: str | None = None,
    since_source_url: str | None = None,
    structure_basis: str | None = None,
    read_from: str | None = None,
    validation_sources: str | None = None,
    validation_documents: str | None = None,
    until_source_url: str | None = None,
    until_read_from: str | None = None,
) -> dict:
    """The property bag for one claim, ready to UPSERT on `claim_key`.

    `first_seen_at` is deliberately absent: it must survive later updates, so the
    writers set it with COALESCE against the stored value rather than passing it
    in here, where it would overwrite on every re-import.
    """
    return {
        "claim_key": claim_key(kind, from_id, to_id, source_id,
                               role_key=_key_discriminator(kind, role, filing_type)),
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
        # where that total comes from when it is newer than the claim's filing
        "denominator_date": denominator_date,
        # the day the filing states the position as of (13D/G date of event)
        "event_date": event_date,
        "voting_shares": voting_shares,
        # The record KIND behind the assertion — the Sources panel shows it as
        # "SEC EDGAR · 13F", which tells a reader whose rulebook to read.
        "filing_type": filing_type,
        # How `since` is known when it is not the stated start: "first_listed" =
        # the oldest annual subsidiary list naming it, a lower bound — without
        # this a claim's since would read as the start of the holding.
        "since_basis": since_basis,
        "since_source_url": since_source_url,
        # The layout evidence behind an inferred tree position (see edge_schema).
        "structure_basis": structure_basis,
        # How far the register checked the record behind this claim (GLEIF's
        # ValidationSources / ValidationDocuments) — provenance, not credibility.
        "validation_sources": validation_sources,
        "validation_documents": validation_documents,
        # How reliably the values were read off the document (edge_schema
        # READ_GRADES: field > table > form > layout > prose > narrative) — the claim's own, so a
        # conflict can prefer the surer reading among equals.
        "read_from": read_from,
        # Where the END was read, when a seat's departure is stated by another
        # record than its appointment (an 8-K after a Form 4): the appointment
        # keeps `source_url`/`read_from`, the departure has its own (2026-10-25).
        "until_source_url": until_source_url,
        "until_read_from": until_read_from,
        "last_seen_at": now_iso(),
    }


# A start date read off the annual subsidiary lists (`since_basis` set — see
# `sec_ex21.earliest_listing`) is written by the history run, not by the scrape
# of the list itself, which states no start. A re-scrape rewrites the whole
# claim; without this it wrote "no start date" over the listing date — an
# Exhibit 21 re-read wiped 148 of News Corp's claims while their edges kept the
# date. So: a write that brings NO start leaves a listing date alone; one that
# brings a start replaces it as before.
#
# The order of the three assignments does not matter: when the date is kept
# `since_basis` is kept too, so each CASE sees the same stored value either way.
_KEEP = ":since IS NULL AND :since_basis IS NULL AND since_basis IS NOT NULL"
# A seat's END is kept the same way: a Form 4 re-read of a seat an 8-K closed
# brings no end, and wrote null over the departure — the claim said "open"
# while the edge stayed closed. A write with no end and the SAME start (or no
# start) leaves the end alone; a different start is a new spell (Steve Jobs's
# second CEO tenure) and replaces it. Role claims only: an ownership claim
# that re-asserts a holding without an end does mean it is held again.
_END_KEPT = (":until IS NULL AND :kind = 'role' AND until IS NOT NULL "
             "AND (:since IS NULL OR :since = since)")
_END_RULES = ("until", "until_source_url", "until_read_from")
_LISTING_DATE_KEPT = {
    "since": f"since = CASE WHEN {_KEEP} THEN since ELSE :since END",
    "since_source_url": f"since_source_url = CASE WHEN {_KEEP} THEN since_source_url ELSE :since_source_url END",
    "since_basis": f"since_basis = CASE WHEN {_KEEP} THEN since_basis ELSE :since_basis END",
    "until": f"until = CASE WHEN {_END_KEPT} THEN until ELSE :until END",
    "until_source_url": f"until_source_url = CASE WHEN {_END_KEPT} THEN until_source_url ELSE :until_source_url END",
    "until_read_from": f"until_read_from = CASE WHEN {_END_KEPT} THEN until_read_from ELSE :until_read_from END",
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
    from app.stake_history import deferred, record_rows, rows_for_batch

    props = claim_props(**kwargs)
    if not props["source_id"]:
        return
    # ArcadeDB applies the assignments in order, each seeing the ones before
    # it: the end rules read the STORED start, so they go first — after
    # `since = :since` the comparison would see the new start and keep an end
    # a new spell should replace.
    names = sorted(props, key=lambda n: 0 if n in _END_RULES else 1)
    sets = ", ".join(_LISTING_DATE_KEPT.get(name, f"{name} = :{name}") for name in names)
    try:
        # Read the stored stake BEFORE the upsert replaces it: a material change
        # keeps the overwritten statement (app.stake_history). Inside a runner's
        # `one_statement_per_run()` the rows wait for the run's last write.
        history = [] if props["kind"] != KIND_OWNS or deferred(props) else rows_for_batch([props])
        run_sql(
            f"UPDATE Claim SET {sets}, first_seen_at = COALESCE(first_seen_at, :last_seen_at) "
            f"UPSERT WHERE claim_key = :claim_key",
            props,
        )
        if history:
            record_rows(history)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not record %s claim %s->%s from %s: %s",
            props["kind"], props["from_id"], props["to_id"], props["source_id"], exc)


def close_role_claim(*, from_id: str, to_id: str, source_id: str, role: str, until: str,
                     until_source_url: str | None = None, until_read_from: str | None = None,
                     source_date: str | None = None, filing_type: str | None = None,
                     credibility_score: int = 80) -> None:
    """Record that `source_id` states the end of a seat. The source's own
    claim for the position, when it has one, is UPDATED — it keeps the
    appointment's citation (`source_url`, `read_from`, `filing_type`) and
    gains the departure's (`until`, `until_source_url`, `until_read_from`).
    Until 2026-10-25 the close went through `record_claim` and so overwrote
    the Form 4's citation with the 8-K's. A source that never asserted the
    seat (an 8-K closing a Wikidata-drawn seat) gets a claim stating only the
    end, cited by the closing record."""
    from app.db.arcadedb import run_sql
    key = claim_key(KIND_ROLE, from_id, to_id, source_id, role_key=canonical_role(role))
    try:
        rows = run_sql(
            "UPDATE Claim SET until = :until, until_source_url = :u, until_read_from = :r, "
            "last_seen_at = :now WHERE claim_key = :key",
            {"until": until, "u": until_source_url, "r": until_read_from, "now": now_iso(), "key": key})
    except Exception as exc:  # noqa: BLE001 - as record_claim: losing provenance beats failing the scrape
        logging.getLogger(__name__).warning("could not close role claim %s->%s from %s: %s",
                                            from_id, to_id, source_id, exc)
        return
    if int((rows or [{}])[0].get("count") or 0):
        return
    record_claim(kind=KIND_ROLE, from_id=from_id, to_id=to_id, source_id=source_id, role=role,
                 until=until, source_url=until_source_url, source_date=source_date,
                 credibility_score=credibility_score, read_from=until_read_from, filing_type=filing_type,
                 until_source_url=until_source_url, until_read_from=until_read_from)


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

    Ranked as `owns_merge.answer_rank` ranks a shared edge's answer — official
    tier, then a stated stake, then credibility, then the reading grade — with
    ties broken by the most recent source_date. So a community source's number
    never beats a register saying "owns, amount undisclosed"; among registers a
    stake beats a subsidiary list that states none (the UK PSC's 75% over SEC's
    Exhibit 21); and of two equally credible claims the one read from a field
    beats the one read off a page.
    """
    if not claims:
        return None
    from app.scraper.owns_merge import answer_rank
    return max(claims, key=lambda c: (answer_rank(c), str(c.get("source_date") or "")))


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
        "filing_type": winner.get("filing_type"),
        "read_from": winner.get("read_from"),
    }


def key_for(claim: dict, *, from_id: str | None = None, to_id: str | None = None) -> str:
    """The key a stored claim SHOULD have — what `claim_props` would give it.

    The one place a stored claim is re-keyed from: a ROLE claim's key includes
    its canonical role ("CEO" and "Director" on the same seat are two claims),
    and the merge re-keyed without it — collapsing a person's roles at a company
    into one claim, which the next scrape then doubled.
    """
    kind, role = claim.get("kind"), claim.get("role")
    # `or ""`: a row some old path wrote without a source still gets A key — a
    # merge must not fail on it
    return claim_key(kind or "", from_id or claim.get("from_id") or "", to_id or claim.get("to_id") or "",
                     claim.get("source_id") or "",
                     role_key=_key_discriminator(kind, role, claim.get("filing_type")))


def rekey_claim(claim: dict, *, from_id: str | None = None, to_id: str | None = None) -> str:
    """Give a stored claim the key its content calls for, optionally with a new
    endpoint. Returns "unchanged", "moved" or "folded".

    * **moved** — re-keyed in place: every field stays, `first_seen_at` too.
    * **folded** — a claim with that key already exists: they are the same
      assertion (same kind, pair, source, role), so one row remains. The one
      seen LAST is the source's current statement and is kept; `first_seen_at`
      becomes the earlier of the two, and a listing date (`since_basis`) the
      kept one lacks is carried over.
    """
    from app.db.arcadedb import run_sql

    old_key = claim["claim_key"]
    f, t = from_id or claim["from_id"], to_id or claim["to_id"]
    new_key = key_for(claim, from_id=f, to_id=t)
    if new_key == old_key and (f, t) == (claim["from_id"], claim["to_id"]):
        return "unchanged"
    twin = run_sql("SELECT FROM Claim WHERE claim_key = :k", {"k": new_key}) if new_key != old_key else []
    if not twin:
        run_sql("UPDATE Claim SET from_id = :f, to_id = :t, claim_key = :new WHERE claim_key = :old",
                {"f": f, "t": t, "new": new_key, "old": old_key})
        return "moved"

    twin = twin[0]
    seen = lambda c: str(c.get("last_seen_at") or "")                       # noqa: E731
    kept, other = (claim, twin) if seen(claim) > seen(twin) else (twin, claim)
    props = {k: v for k, v in kept.items() if not k.startswith("@")}
    props.update(from_id=f, to_id=t, claim_key=new_key)
    firsts = [c.get("first_seen_at") for c in (claim, twin) if c.get("first_seen_at")]
    props["first_seen_at"] = min(firsts) if firsts else None
    if not props.get("since") and other.get("since_basis"):
        props.update({k: other.get(k) for k in ("since", "since_basis", "since_source_url")})
    # the stale row first: the UNIQUE key is free for nobody else, but two rows
    # for one assertion must never be what a crash leaves behind twice over
    run_sql("DELETE FROM Claim WHERE claim_key = :old", {"old": old_key})
    sets = ", ".join(f"{k} = :{k}" for k in props)
    run_sql(f"UPDATE Claim SET {sets} WHERE claim_key = :claim_key", props)
    return "folded"


def migrate_claims(dead_id: str, keep_id: str) -> int:
    """Re-point the claims of a merged-away (or renamed) node at its survivor.

    A claim's key is a hash of (kind | from | to | source | role), so rewriting
    an endpoint changes the key: each claim is re-keyed (`rekey_claim`) — moved
    in place, or folded into the claim the survivor already holds for the same
    assertion.

    Without this, every merge orphaned the dead node's claims: the surviving
    edges existed, `claims_for()` found nothing for them, and the merged
    company showed as uncorroborated however many sources had asserted it.
    """
    from app.db.arcadedb import run_sql
    from app.stake_history import migrate as migrate_stake_history

    moved = 0
    for end in ("from_id", "to_id"):
        for r in run_sql(f"SELECT FROM Claim WHERE {end} = :d", {"d": dead_id}):
            rekey_claim(r, **{end: keep_id})
            moved += 1
    # The stake rows behind those claims follow them (they carry the ids, not
    # the key, so a plain re-point is enough), and so do the industry
    # statements (re-keyed: their key hashes the entity id).
    migrate_stake_history(dead_id, keep_id)
    from app.industry import migrate as migrate_industry
    migrate_industry(dead_id, keep_id)
    return moved


def heal_claim_keys(dry_run: bool = False, page: int = 2000) -> dict:
    """Re-key every claim whose key does not match its own content.

    Two paths left such claims behind (both fixed): a merge re-keyed ROLE
    claims without the role, and an id rename re-pointed claims without
    re-keying them at all. Either way the next scrape wrote a correctly keyed
    claim beside the stale one — the same assertion, stored twice.

    Walks the type through its UNIQUE `claim_key` index, two-sided range per
    page (`app.db.paging`: the only walk that survives a full-size database).
    Keys are sha1 hex, so the space ends below "g". A re-keyed claim may be
    met again further on; it is then "unchanged".
    """
    from app.db.arcadedb import run_sql

    out = {"claims": 0, "stale": 0, "moved": 0, "folded": 0}
    after = ""
    while True:
        rows = run_sql(f"SELECT FROM Claim WHERE claim_key > '{after}' AND claim_key < 'g' "
                       f"ORDER BY claim_key LIMIT {int(page)}")
        if not rows:
            return out
        for r in rows:
            after = max(after, r["claim_key"])
            out["claims"] += 1
            if key_for(r) == r["claim_key"]:
                continue
            out["stale"] += 1
            if dry_run:
                continue
            out[rekey_claim(r)] += 1
