"""One active OWNS edge per (owner, owned) pair, whichever sources assert it.

A pair used to get one edge PER SOURCE: each writer looked only for the edge it
had written itself (SEC by ``source_id``, GLEIF by its direct/indirect marker,
UK PSC by its ``psc_self_link``), so the second source to state a relationship
drew a second edge beside the first. News Corp's timeline listed Dow Jones twice
(SEC's "since 2013 or earlier" and GLEIF's 2024), and the duplicate-edge cleanup
after every GLEIF import deleted the SEC edge, date and all, for the next SEC
scrape to draw again.

Now every writer takes over the pair's active edge, whoever drew it, and this
module is the rule for how two assertions share that edge:

* **The answer** — stake, type, share counts, source, link, credibility, filing
  type, end date (``ANSWER_FIELDS``) — belongs to ONE source and moves as a unit,
  so an edge never cites one source with another's link or number. The source
  that ranks higher (``answer_rank``: official tier, then a stated stake, then
  credibility) holds it; on a tie the source already holding it keeps it, so
  two sources re-asserting the same pair on alternate nights cannot flip the
  edge back and forth. Every source's own answer is kept in its Claim
  regardless.

  Among official sources a stated stake ranks first, because a subsidiary list
  is silent, not "undisclosed": SEC's Exhibit 21 (credibility 98) names a
  company without a number, and letting it displace the UK register's 75%
  (credibility 97) would make the edge know less than the evidence behind it.
  The tier comes before the stake so a community source's figure never
  displaces a register's "owns, amount not stated" (``app.claims.best_claim``
  ranks claims the same way).

* **The start date** is combined, not taken from the answer's source: the
  earliest date any source gives (``combine_since``). A lower bound
  (``since_basis = "first_listed"``: the oldest Exhibit 21 naming the
  subsidiary) keeps its label, so the edge reads "since 2013 or earlier"; a
  stated start on or before it replaces it.

* **Structure** one source contributes — GLEIF's direct/indirect marker and
  ultimate-parent fields, the PSC appointment link — stays on the edge whoever
  holds the answer (``STRUCTURAL_FIELDS``).
"""
from __future__ import annotations

#: One source's answer about the pair. Moved together, never mixed across sources.
ANSWER_FIELDS: tuple = (
    "stake_percent", "voting_power_pct", "ownership_type",
    "share_class", "shares", "shares_outstanding", "voting_shares", "value_usd",
    "until", "until_reason",
    "source_id", "credibility_score", "source_url", "source_date", "file_date",
    "filing_type",
)

#: The start date and how it is known — combined across sources.
SINCE_FIELDS: tuple = ("since", "since_basis", "since_source_url")

#: What one source adds to the shared edge and no other source states: kept from
#: whichever edge has it.
STRUCTURAL_FIELDS: tuple = (
    "interest_types", "direct_or_indirect", "psc_self_link",
    "also_ultimate", "ultimate_since", "ultimate_until",
)

LOWER_BOUND = "first_listed"

#: The credibility floor of the official tier. GLEIF (92), UK PSC (97) and SEC
#: EDGAR (98) sit above it; Wikidata (80) and OpenCorporates (85) below. Tier by
#: score rather than by source name, so a new source lands in the right tier by
#: setting its credibility honestly instead of by editing lists.
OFFICIAL_TIER_MIN_CREDIBILITY = 90


def answer_rank(values: dict) -> tuple:
    """Which source's answer the shared edge carries: the official tier, then a
    stated stake, then credibility. Deliberately no date tie-break — see
    ``outranks``."""
    credibility = int(values.get("credibility_score") or 0)
    return (credibility >= OFFICIAL_TIER_MIN_CREDIBILITY,
            values.get("stake_percent") is not None,
            credibility)


def outranks(incoming: dict, current: dict) -> bool:
    """Whether ``incoming`` takes the answer from the edge holding ``current``.
    Strictly higher only: on a tie the incumbent keeps it."""
    return answer_rank(incoming) > answer_rank(current)


def combine_since(*candidates: dict | None) -> dict:
    """The combined start date: the earliest one any candidate gives.

    Each candidate is a dict with ``since`` / ``since_basis`` /
    ``since_source_url`` (missing keys are None). On the same day a stated start
    beats a lower bound — it says more. Returns all three fields, None when no
    candidate is dated.
    """
    dated = [c for c in candidates if c and c.get("since")]
    if not dated:
        return {"since": None, "since_basis": None, "since_source_url": None}
    best = min(dated, key=lambda c: (str(c["since"]), c.get("since_basis") is not None))
    return {"since": best["since"], "since_basis": best.get("since_basis"),
            "since_source_url": best.get("since_source_url")}


def fold(edges: list[dict], survivor: dict) -> dict:
    """The properties to set on ``survivor`` so it carries what ``edges`` (every
    active edge of one pair, the survivor included) said between them.

    The answer comes from the highest-ranked edge (the survivor on a tie); the
    start date is combined; structural fields the survivor lacks come from
    whichever other edge has them; freshness is the most recent confirmation.
    ``shortcut`` is left alone — it is a statement about the survivor's own
    position in a path, and copying it could hide a holding.
    """
    top = survivor
    for e in edges:
        if outranks(e, top):
            top = e
    out: dict = {}
    if top is not survivor:
        out.update({f: top.get(f) for f in ANSWER_FIELDS})
    since = combine_since(*edges)
    if any(since[f] != survivor.get(f) for f in SINCE_FIELDS):
        out.update(since)
    for f in STRUCTURAL_FIELDS:
        if survivor.get(f) is None:
            value = next((e.get(f) for e in edges if e.get(f) is not None), None)
            if value is not None:
                out[f] = value
    seen = [e.get("last_scraped_at") for e in edges if e.get("last_scraped_at")]
    if seen and max(seen) != survivor.get("last_scraped_at"):
        out["last_scraped_at"] = max(seen)
    if survivor.get("stale") and any(not e.get("stale") for e in edges):
        out["stale"] = False
    return out
