"""One fact, one field: `nationalities`, never a `nationality` beside it.

A scalar beside its list is one fact stored twice, and two writers each fill
the half they know (on 2026-10-25 every dev person carried both, 204 of them
an empty list beside the scalar). The rule: `nationalities` is the field, the
scalar is gone, and every person writer folds a single value in through
`merge_nationalities`.
"""
from __future__ import annotations


def merge_nationalities(nationalities: list[str] | None, nationality: str | None) -> list[str]:
    """The list with the single value folded in (first when the list was empty,
    appended when it is new), blanks dropped, order kept."""
    out: list[str] = []
    for v in [*(nationalities or []), nationality]:
        v = (v or "").strip() if isinstance(v, str) else v
        if v and v not in out:
            out.append(v)
    return out
