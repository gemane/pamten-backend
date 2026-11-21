"""The companies registered at one address — a letterbox's network, read off
the matching key every importer already stores (2026-11-10).

A registered office is often not where a company is run but an agent's door:
on dev, 99 companies give CSC's 251 Little Falls Drive in Wilmington, 79 The
Corporation Trust Company's 1209 Orange St, 52 Barclays' 1 Churchill Place.
Who else sits behind the same door is the question a letterbox raises, and
the answer is already in the database: GLEIF, the Companies House importers
and the bulk import write `address_normalized` (`app.addresses`, the
registered kind's key) on every company they create, indexed NOTUNIQUE.

So the address is derived here, at read time, not stored as a vertex of its
own: a stored Address node would be a copy of that key needing a sync pass
after every import, delta and merge, and a company whose registered office
moves would sit at the old door until the pass ran. Read from the key, the
group is always the one the companies themselves state.

The key is exact — `normalize_address` only lowercases and strips
punctuation — so the same building under two spellings is two groups ("1209
Orange St" and "1209 Orange Street"; "c/o Walkers Corporate Limited, 190
Elgin Avenue" and "190 Elgin Avenue"). Merging those would need an address
parser; a hand-written normalisation pass was declined (2026-09-17, see the
geocoding notes), so the split stays visible rather than guessed away.
"""
from __future__ import annotations

from app.addresses import REGISTERED, field
from app.database import public_props
from app.scraper.mapper import normalize_address

#: the stored key and readable address of the registered office
KEY = field(REGISTERED, "key")
FULL = field(REGISTERED, "full")

#: a formation agent's door can hold tens of thousands of UK companies; the
#: list is paged, the count is always the true one
PAGE_MAX = 1_000
#: the rows the PROFILE carries: every profile of a company at a busy door
#: would otherwise ship 900 neighbours; the client pages the rest from
#: GET /search/address, and a graph of 50 is already a crowd
PROFILE_ROWS = 50


def _n(result) -> int:
    rec = result.single()
    return (rec.get("n") if rec else 0) or 0


def _visible_count(session, key: str, hidden: set, exclude: str | None) -> int:
    """Companies at `key`, less the suppressed ones and `exclude` (the company
    whose profile asks). Two indexed counts, never a scan of the group."""
    total = _n(session.run(f"MATCH (e:Entity {{{KEY}: $k}}) RETURN count(e) AS n", k=key))
    drop = [i for i in hidden if i != exclude] + ([exclude] if exclude else [])
    if drop and total:
        gone = _n(session.run(
            f"MATCH (e:Entity {{{KEY}: $k}}) WHERE e.id IN $ids RETURN count(e) AS n",
            k=key, ids=drop))
        total -= gone
    return max(total, 0)


def _page(session, key: str, hidden: set, exclude: str | None, limit: int, offset: int) -> list[dict]:
    """One page of the companies at `key`, by name, without `exclude` and the
    suppressed ones. Filtered in Python after a page with headroom, as the
    profile's other sections drop suppressed nodes; the set is a handful of ids."""
    skip = set(hidden)
    if exclude:
        skip.add(exclude)
    rows = session.run(
        f"MATCH (e:Entity {{{KEY}: $k}}) RETURN e ORDER BY e.name, e.id "
        f"SKIP {int(offset)} LIMIT {int(limit) + len(skip)}", k=key)
    # as every public read of a node: without the properties kept for matching only
    out = [public_props(dict(r["e"])) for r in rows]
    return [e for e in out if e.get("id") not in skip][:limit]


def co_registered(session, entity: dict, hidden: set, limit: int) -> dict | None:
    """The profile's `registered_address` section: the company's registered
    office and the others registered at it — `{key, address, count,
    entities}` — or None when the company states no registered office. `count`
    is the true number of OTHER companies there; `entities` the first `limit`
    of them by name, at most PROFILE_ROWS."""
    key = entity.get(KEY)
    if not key:
        return None
    eid = entity.get("id")
    return {
        "key": key,
        "address": entity.get(FULL),
        "count": _visible_count(session, key, hidden, eid),
        "entities": _page(session, key, hidden, eid, min(limit, PROFILE_ROWS), 0),
    }


def companies_at(session, key: str, hidden: set, limit: int, offset: int = 0) -> dict | None:
    """Every company registered at one address, paged — what the client
    opens when the reader follows the address itself. The key is normalised
    again, so a readable address works as well as a stored key. None when no
    company is registered there."""
    k = normalize_address(key)
    if not k:
        return None
    count = _visible_count(session, k, hidden, None)
    if not count:
        return None
    entities = _page(session, k, hidden, None, limit, offset)
    # The readable form: the companies' own, the most common spelling first
    # (two sources may write the same key in different case or punctuation).
    spellings: dict[str, int] = {}
    for e in entities:
        if e.get(FULL):
            spellings[e[FULL]] = spellings.get(e[FULL], 0) + 1
    address = max(spellings, key=lambda s: (spellings[s], s)) if spellings else None
    return {"key": k, "address": address, "count": count, "offset": offset, "entities": entities}
