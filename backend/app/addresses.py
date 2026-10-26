"""An entity's places: one registry for the fields every address kind is
stored in (2026-10-26).

A company has two places, and the difference matters: where it is
REGISTERED (often an agent's or an accountant's door — 24 dev companies share
one building in Wilmington) and where it is RUN, its headquarters. Each is
stored as the same shape under its own prefix: the readable address, its
parts (street, city, postcode, country), and the geocoded pin with its
precision. Until this module the pattern was written out by hand in six
places — the GLEIF, Companies House PSC and company-data importers, the SEC
writer, the geocoder's two passes and the heal that undid the UK importers'
copy of the registered office into the headquarters fields — and the schema
ledger listed the fields a seventh time. They read it here now; a third kind
of place is one `KINDS` entry.

Storage is unchanged: the names are the ones the database, the API and the
client have always used (`address`, `address_normalized`, `reg_*`, `hq_*`).
Two of them do not follow the prefix and are stated: the registered office's
readable `address` and its matching key `address_normalized` (the same-name
dedup's corroborator). The registered office has no country field of its own:
the entity's `country` IS where it is registered.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.scraper.mapper import normalize_address

REGISTERED = "registered"
HEADQUARTERS = "headquarters"

#: the parts an address is written in, as a structured geocode takes them
PARTS = ("street", "city", "postcode", "country")
#: what a kind stores beyond its parts
_EXTRA = ("full", "key", "lat", "lng", "precision")


@dataclass(frozen=True)
class Kind:
    prefix: str
    full: str                    # the readable address, for display and the free-text geocode
    key: str | None              # its matching key (normalize_address), when the kind keeps one
    country: str | None          # its own country field; None when the entity's `country` is it
    geocode_country: bool        # may the geocoder fall back to the country's centroid?


KINDS: dict[str, Kind] = {
    # NO country fallback for the registered pin: a centroid put 51 American
    # companies in a field in Kansas and 39 British ones in the Irish Sea, all
    # captioned as registered offices. No pin says "we do not know".
    REGISTERED: Kind(prefix="reg", full="address", key="address_normalized",
                     country=None, geocode_country=False),
    HEADQUARTERS: Kind(prefix="hq", full="hq_address", key=None,
                       country="hq_country", geocode_country=True),
}


def field(kind: str, part: str) -> str | None:
    """The stored field of one part of one kind, or None when the kind has
    no such field (the registered office's country, the headquarters' key)."""
    k = KINDS[kind]
    if part == "full":
        return k.full
    if part == "key":
        return k.key
    if part == "country":
        return k.country
    if part == "precision":
        return f"{k.prefix}_geo_precision"
    if part in ("street", "city", "postcode", "lat", "lng"):
        return f"{k.prefix}_{part}"
    raise KeyError(part)


def fields(kind: str) -> dict[str, str]:
    """{part: stored field} for every part the kind stores."""
    out = {}
    for part in PARTS + _EXTRA:
        f = field(kind, part)
        if f:
            out[part] = f
    return out


def all_fields() -> list[str]:
    """Every stored field of every kind, in registry order."""
    out: list[str] = []
    for kind in KINDS:
        out += [f for f in fields(kind).values() if f not in out]
    return out


def coordinate_fields() -> list[str]:
    """The pins' fields, which are numbers (DOUBLE); everything else is text."""
    return [field(kind, p) for kind in KINDS for p in ("lat", "lng")]


def parts(kind: str, *, street=None, city=None, postcode=None, country=None, full=None,
          empty: tuple[str, ...] = ()) -> dict:
    """The property bag one source writes for one place: the parts it states,
    the readable address, and its matching key when the kind keeps one (from
    `full`). A value the source does not state is left out, so a re-import
    never blanks what another source supplied — except the parts named in
    `empty`, which are written as None (a writer whose COALESCE or UPDATE
    expects every parameter). A country is dropped for the registered kind:
    the entity's `country` is where it is registered, set by the caller."""
    given = {"street": street, "city": city, "postcode": postcode, "country": country, "full": full}
    k = KINDS[kind]
    if k.key and (full or "key" in empty):
        given["key"] = normalize_address(full)
    out = {}
    for part, value in given.items():
        f = field(kind, part)
        if f is None:
            continue
        if value:
            out[f] = value
        elif part in empty:
            out[f] = None
    return out


def read(node: dict, kind: str) -> dict:
    """{part: value} of one place, read off a stored node."""
    return {part: node.get(f) for part, f in fields(kind).items()}


def fill_clause(var: str, kind: str, which: tuple[str, ...]) -> str:
    """`e.hq_city = COALESCE(e.hq_city, $hq_city), …` — a Cypher SET that only
    fills what is empty, for the named parts, the parameters named after the
    fields (what `parts(..., empty=which)` produces)."""
    fs = [field(kind, p) for p in which]
    return ",\n    ".join(f"{var}.{f} = COALESCE({var}.{f}, ${f})" for f in fs if f)


def create_map(kind: str, which: tuple[str, ...]) -> str:
    """`hq_city: $hq_city, …` — the same parts as a CREATE property map."""
    fs = [field(kind, p) for p in which]
    return ", ".join(f"{f}: ${f}" for f in fs if f)
