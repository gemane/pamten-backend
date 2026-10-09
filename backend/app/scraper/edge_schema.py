"""
The one place that knows what sits on an edge.

Eight bugs in two days shared a single shape: a property added to one code path
and not its siblings. The worst offenders were the merge paths, which RECREATE
an edge from a hand-written property list — four such blocks existed, the
newest docstring knew about three of them, and the lists were between 6 and 18
properties long against a real universe of 25. Every gap was silent: the merge
succeeded, the edge existed, the data was simply gone.

The obvious generic fix — copy ``properties(r)`` server-side — is unavailable:
prod ArcadeDB silently no-ops cross-edge property reads (see the arcadedb
gotchas memory; it shipped broken twice before being understood). So the
mechanism here is the opposite: the property list is **data**, and the Cypher
fragments are **derived** from it with bound ``$params``, which is the one
write shape proven reliable on prod.

Adding a property to an edge now means adding it to the tuple below — once.
The merge paths carry it automatically, and the parity test in
``tests/scraper/test_writer_parity.py`` fails on any writer that invents a
property this module does not know.
"""

#: Every property any writer puts on an OWNS edge. Audited 2026-08-29 across
#: all 13 writers (runner ×2, bulk_import, companies_house_psc,
#: ch_psc_incremental ×2, gleif_incremental ×2, maintenance ×3, persons,
#: federation, relationships). Order is cosmetic; membership is the contract.
OWNS_PROPS: tuple = (
    "stake_percent",
    "voting_power_pct",
    "ownership_type",
    "since",
    "until",
    "until_reason",
    "source_id",
    "credibility_score",
    "source_url",
    "source_date",
    "last_scraped_at",
    "interest_types",
    "direct_or_indirect",
    "psc_self_link",
    "share_class",
    "shares",
    "shares_outstanding",
    # The filing date `shares_outstanding` comes from, when it is NEWER than
    # the holder's own filing: the stake was re-divided by the issuer's latest
    # stated total (sec_edgar._restate_against_newest_denominator). Unset when
    # the total is the holder's own filing's.
    "denominator_date",
    # The day the filing states the position as of — a 13D/G cover's "date of
    # event". The count and the percentage are true AS OF this day; the filing
    # date (source_date) is when it was said, often weeks later.
    "event_date",
    "voting_shares",
    "stale",
    "shortcut",
    "also_ultimate",
    "ultimate_since",
    "ultimate_until",
    "value_usd",
    "file_date",
    # Which KIND of record asserted this — "13G/A", "13F", "RR", "PSC". The
    # source names the register; this names the rulebook the fact lives under,
    # which is what a reader needs to judge it (a 13G is a >5% event filing,
    # a 13F a quarterly snapshot). Unset where there is no filing (Wikidata).
    "filing_type",
    # How `since` is known, when it is not the start of the holding itself:
    # "first_listed" = the oldest annual subsidiary list (Exhibit 21/8.1) in an
    # unbroken run naming it — a LOWER bound ("owned since at least");
    # "newly_listed" = the same, and the list for the year before does not name
    # it, nor any older one ("first listed 2025"). Unset when `since` is the
    # stated start. `since_source_url` is that filing.
    "since_basis",
    "since_source_url",
    # GLEIF's own archive refutes a start before this date: the child was still
    # the top of a tree then (gleif_rr_history). The stated start stays in the
    # source's claim; the edge and every merge ignore anything earlier.
    "since_not_before",
    # How the edge's PLACE in the tree is known when a source showed it by
    # layout rather than stating it: "ex21_indent" (the filer indented the
    # subsidiary under another), "ex21_heading" (it sits under a "Subsidiaries
    # of X" heading), "ex21_column" (a "Direct controlling entity" column names
    # its holder). Unset for GLEIF's stated direct/ultimate markers — which
    # is how the GLEIF delta tells its own markers from an inferred one.
    "structure_basis",
    # How reliably the answer was READ from its document — not how credible the
    # source is (that is `credibility_score`: who speaks) but how much of the
    # value is ours. One of `READ_GRADES`, from best to worst: "field" (a named
    # field in XML/JSON — GLEIF, PSC, 13F, a structured 13D/G), "table" (a cell
    # under a header the filer wrote — most Exhibit 21s), "form" (a numbered
    # item of a regulator's form read off its text rendering — a pre-2024 13D/G
    # cover page), "layout" (inferred from how the page is laid out: an
    # indented parent, a heading, a header carried onto the next page, a
    # headerless table), "prose" (items written as text lines — a subsidiary
    # list of "Name (Jurisdiction)" paragraphs), "narrative" (a fact picked out
    # of running text — an 8-K's departure sentence). An edge is as good as its
    # weakest value (`weakest_reading`). Unset where nobody read anything: a
    # manual entry, an edge from before the grade existed.
    "read_from",
)

ROLE_PROPS: tuple = (
    "role",
    "since",
    "until",
    "source_id",
    "credibility_score",
    "source_url",
    "source_date",
    "last_scraped_at",
    "read_from",
)

#: `read_from` values, best first. The order is the rank: a grade further
#: right is a weaker reading. `form` sits above `layout` because a form's
#: labels are the regulator's — numbered, fixed, not the filer's to vary — so
#: only the number is read, while a layout reading recovers a relationship the
#: filer expressed through spacing alone. The three text grades differ in how
#: much text the pattern has to understand: a line shaped like a list item,
#: against a whole sentence.
READ_FIELD, READ_TABLE, READ_FORM = "field", "table", "form"
READ_LAYOUT, READ_PROSE, READ_NARRATIVE = "layout", "prose", "narrative"
READ_GRADES: tuple = (READ_FIELD, READ_TABLE, READ_FORM, READ_LAYOUT, READ_PROSE, READ_NARRATIVE)


def read_rank(grade: str | None) -> int:
    """Higher is better; an unset grade ranks below every real one — an edge
    that says how it was read beats one that cannot say, all else equal."""
    return len(READ_GRADES) - READ_GRADES.index(grade) if grade in READ_GRADES else 0


def weakest_reading(*grades: str | None) -> str | None:
    """The grade of a value built from several readings: the weakest of them.
    An Exhibit 21 row read from a table whose parent came from indentation is
    a `layout` edge — the stake is only as placed as the parent is. Unset
    grades are ignored; all unset gives None."""
    known = [g for g in grades if g in READ_GRADES]
    return max(known, key=READ_GRADES.index) if known else None

RELATED_TO_PROPS: tuple = (
    "relation",
    "source_id",
    "last_scraped_at",
)


def edge_return_clause(var: str, props: tuple) -> str:
    """``r.x AS x, r.y AS y, …`` — reads every schema property off an edge."""
    return ", ".join(f"{var}.{p} AS {p}" for p in props)


def edge_create_clause(props: tuple) -> str:
    """``x: $x, y: $y, …`` — writes every schema property from bound params.

    Bound ``$params`` only, never interpolated values: that is the write shape
    proven reliable on prod ArcadeDB, and it also means a property whose value
    is None is written as null rather than omitted — so a recreated edge has
    the same key set however sparse the original was.
    """
    return ", ".join(f"{p}: ${p}" for p in props)


def edge_params(record, props: tuple) -> dict:
    """The bound-parameter dict for a record read via `edge_return_clause`."""
    return {p: record.get(p) for p in props}


def owns_props(**kwargs) -> dict:
    """The full property bag for one OWNS edge — every schema field, None where
    the caller did not say.

    Strict on purpose: an unknown keyword raises, so a new property must be
    added to ``OWNS_PROPS`` before any writer can pass it. That inverts the
    failure mode — instead of a writer silently carrying a field the schema
    (and therefore the merges) does not know, the write fails loudly at the
    first call.

    Writers keep their own update semantics (matching keys, COALESCE
    direction, credibility defaults differ per source, deliberately); what
    they share is this vocabulary.
    """
    unknown = set(kwargs) - set(OWNS_PROPS)
    if unknown:
        raise TypeError(
            f"owns_props: {sorted(unknown)} not in OWNS_PROPS — "
            f"add the property to the schema first, so the merge paths and "
            f"parity tests know it exists")
    return {p: kwargs.get(p) for p in OWNS_PROPS}
