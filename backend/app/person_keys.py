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

- `p:<16 hex>` — a keyed hash (HMAC-SHA-256 under `PERSON_POSTCODE_HASH_KEY`,
  cut to 64 bits) of the postcode of a PSC record's correspondence address.
  The readable postcode is never stored: the graph holds no address of a
  person, and the hash can be compared and not read. Two entries of one name
  and birth month at one postcode are one person — measured on the full
  register: 1.6 million such merges, at least 175 expected wrong; against the
  identity-verification day, pairs it links differ in 0.9 %, under the 1.8 %
  floor of people who are certainly one. Without the key setting no hash is
  made. A postcode more than 1,000 people give is an agent's
  (`uk_mass_person_postcodes.txt`, 574 of them, holding 2.0 of the 13.9
  million entries with a postcode): no key is kept for it. Decided by the
  user on 2026-11-21, with the lawyer's view still to be asked.

A merged person carries the keys of every entry it was made of.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import pathlib
import re

VERIFIED = "v"
POSTCODE = "p"
EVIDENCE = {VERIFIED: "the same identity-verification day",
            POSTCODE: "the same correspondence postcode"}

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def verification_key(value: str | None) -> str | None:
    """The key for an identity-verification timestamp, or None when the
    register states none ("9999-12-31", its open end, is no day)."""
    day = (value or "")[:10]
    return f"{VERIFIED}:{day}" if _DAY.match(day) and not day.startswith("9999") else None


@functools.lru_cache(maxsize=1)
def mass_person_postcodes() -> frozenset:
    """The UK postcodes more than 1,000 people of significant control give as
    their correspondence address, without spaces, upper case."""
    path = pathlib.Path(__file__).parent / "scraper" / "data" / "uk_mass_person_postcodes.txt"
    return frozenset(line.strip() for line in path.read_text().splitlines()
                     if line.strip() and not line.startswith("#"))


def postcode_key(postcode: str | None, secret: str | None) -> str | None:
    """The key for a correspondence postcode: a keyed hash, so that the same
    postcode gives the same token and the token gives no postcode back. None
    without a postcode, without the secret (the feature is then off), or for
    an agent's postcode."""
    code = re.sub(r"[^0-9A-Z]", "", (postcode or "").upper())
    if len(code) < 4 or not secret or code in mass_person_postcodes():
        return None
    digest = hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()
    return f"{POSTCODE}:{digest[:16]}"


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
