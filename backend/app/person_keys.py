"""Match keys: facts a register states about a person that say "the same one"
when two entries share them — kept for the person dedupe and for nothing else
(findings item 124).

`Person.match_keys` is a list of short tokens, `<kind>:<value>`. A token is
never shown, searched, exported or served: it exists so that two entries of
one common name and one birth month, which the month alone must not merge, can
be recognised as one person.

- `v:<YYYY-MM-DD>` — the day Companies House verified the person's identity
  (`identity_verification_details.identity_verified_on` of a PSC record;
  1,140,028 of the 14 million entries carried one in October 2026, and it is
  becoming mandatory). The SAME day on two entries of one name and birth
  month is one verification of one person. A DIFFERENT day proves nothing —
  a person is verified again when an agent files for them: the days differ in
  4.0 % of the groups of a name only one person bears — so it is evidence
  when it agrees and no veto when it does not.

A merged person carries the keys of every entry it was made of.
"""
from __future__ import annotations

import re

VERIFIED = "v"
EVIDENCE = {VERIFIED: "the same identity-verification day"}

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def verification_key(value: str | None) -> str | None:
    """The key for an identity-verification timestamp, or None when the
    register states none ("9999-12-31", its open end, is no day)."""
    day = (value or "")[:10]
    return f"{VERIFIED}:{day}" if _DAY.match(day) and not day.startswith("9999") else None


def union(*lists) -> list[str] | None:
    """Every key of the given lists, once, in a stable order — None for none
    (a person without keys has no field, never an empty list)."""
    out = sorted({k for keys in lists for k in (keys or []) if k})
    return out or None


def shared_evidence(a, b) -> str | None:
    """What two persons' keys say when they share one, in words for the merge
    log; None when they share none."""
    kinds = {k.split(":", 1)[0] for k in set(a or []) & set(b or [])}
    return next((EVIDENCE[kind] for kind in sorted(kinds) if kind in EVIDENCE), None)
