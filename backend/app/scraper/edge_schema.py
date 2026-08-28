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
``tests/scraper/test_edge_schema.py`` fails on any writer that invents a
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
    "voting_shares",
    "stale",
    "shortcut",
    "also_ultimate",
    "ultimate_since",
    "ultimate_until",
    "value_usd",
    "file_date",
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
)

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
