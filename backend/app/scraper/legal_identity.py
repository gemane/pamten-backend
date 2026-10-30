"""Is a GLEIF record and an SEC filer one legal entity? (findings item 4)

One company can end up as two nodes that share no hard id: GLEIF's `lei:X`
without a CIK, EDGAR's `sec:Y` without an LEI. GLEIF has no CIK field, EDGAR's
`lei` field is empty for most operating companies, and many filers state their
LEI in no filing at all (`sec_lei`): NYT, Tencent and Domino's Pizza, Inc. were
two nodes each after the 2026-10-29b rebuild. A name alone is not enough to
join them — Domino's Pizza, Inc. and its subsidiary Domino's Pizza LLC share
one name key and one street — so `same_legal_entity` asks four things:

1. **The same legal name, legal form kept** (`sec_ex21.legal_name_key`): any
   of GLEIF's names (the legal name, its other and transliterated names)
   against EDGAR's current name or the former name in force on the record's
   reference day. "Domino's Pizza LLC" is not "DOMINOS PIZZA INC".
2. **The same place of registration**: GLEIF's jurisdiction ("US-DE", "KY")
   against EDGAR's state of incorporation (`sec_edgar.incorporation_place`).
   GLEIF giving only "US" where EDGAR says New York is no contradiction, but
   no confirmation either: the record is weak (3).
3. **A weak record needs one more match, at its own date.** Weak: only a
   country where EDGAR names a state, a LAPSED or RETIRED registration (no
   longer maintained), or ENTITY_SUPPLIED_ONLY data. Its reference day ``D``
   is its last update when it is no longer maintained, today otherwise. Then
   either the headquarters at ``D`` — GLEIF's street and city against the
   business address in the header of the filer's filing closest to ``D``
   (today's address when no filing lies within a year, said in the reason) —
   or a subsidiary at ``D``: a GLEIF child in force then that the filer also
   held then (an OWNS edge open on ``D``, or the Exhibit 21 of the fiscal year
   containing ``D``). NYT's lapsed 2019 record says 620 Eighth Avenue, and so
   does NYT's 2021 filing header.
4. **Veto**: never a record GLEIF lists as a child (direct or ultimate) of a
   record that passes 1 and 2 against the filer, while that link was in force
   on ``D`` or is today. Domino's Pizza LLC, a child of the Inc. since
   1999-10-27.

The verdict is (JOIN, evidence) or (REFUSE, reason). The rule runs where a pair
can meet: the GLEIF pointer writer before it mints a node
(`register_pointer.write_from_gleif`), the SEC writer's name match
(`graph_writer._not_this_filer`) and the entity dedup's SEC↔LEI bridge
(`maintenance._bridge_sec_lei`), so the write order no longer decides.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
import time
from functools import wraps

from app import addresses
from app import former_names as former

log = logging.getLogger(__name__)

JOIN, REFUSE = "join", "refuse"

#: A registration GLEIF no longer maintains: its data is a snapshot of its last update.
_UNMAINTAINED = frozenset({"LAPSED", "RETIRED"})
_SELF_REPORTED = "ENTITY_SUPPLIED_ONLY"
#: How far from D a filing header may lie and still say where the filer was then.
_HEADER_WINDOW = timedelta(days=365)
GLEIF_API = "https://api.gleif.org/api/v1/lei-records/{lei}/{rel}"


@dataclass(frozen=True)
class Filer:
    """What EDGAR says of a filer, from one submissions request."""
    cik: str
    name: str
    former_names: tuple[str, ...] = ()       # app.former_names entries "NAME|YYYY-MM-DD"
    home: str | None = None                  # incorporation_place: "US-NY", "KY"
    street: str | None = None                # today's business address
    city: str | None = None


@dataclass
class Evidence:
    """Where the rule reads what the node does not hold. Live by default;
    a test passes its own."""
    address_at: object = None                # (cik, day) -> (street, city, note) | None
    gleif_children: object = None            # (lei) -> [{"name", "place", "periods", "status"}]
    gleif_parents: object = None             # (lei) -> [{"lei", "names", "place", "periods", "status"}]
    held_at: object = None                   # (cik, day) -> [(name, place)]
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.address_at = self.address_at or filing_address_at
        self.gleif_children = self.gleif_children or gleif_children
        self.gleif_parents = self.gleif_parents or gleif_parents
        self.held_at = self.held_at or filer_held_at


def _today() -> str:
    return date.today().isoformat()


def _country(place: str | None) -> str | None:
    return place.split("-")[0] if place else None


#: what the rule reads of a GLEIF node
IDENTITY_FIELDS = ("name", "other_names", "lei_id", "jurisdiction_code", "country",
                   "gleif_registration_status", "validation_sources", "gleif_last_update",
                   *(addresses.field(kind, part) for kind in (addresses.HEADQUARTERS, addresses.REGISTERED)
                     for part in ("street", "city")))


def gleif_place(node: dict) -> str | None:
    """GLEIF's place of registration on a node: the subdivision where it gives
    one ("US-DE"), else the country ("US", "KY")."""
    return node.get("jurisdiction_code") or node.get("country") or None


def reference_day(node: dict, today: str | None = None) -> str:
    """D: the day the record's data was last true by GLEIF — its last update
    when the registration is no longer maintained, today otherwise."""
    today = today or _today()
    status = (node.get("gleif_registration_status") or "").upper()
    stamp = (node.get("gleif_last_update") or "")[:10]
    return stamp if status in _UNMAINTAINED and stamp else today


def names_at(filer: Filer, day: str) -> list[str]:
    """EDGAR's current name and the former name in force on ``day``: the
    first former name whose last day is on or after it."""
    out = [filer.name]
    dated = sorted((until, name) for name, until in map(former.parse, filer.former_names) if until)
    hit = next((name for until, name in dated if until >= day), None)
    if hit:
        out.append(hit)
    return out


def _same_name(gleif_names: list[str], filer_names: list[str]) -> bool:
    from app.scraper.sec_ex21 import legal_name_key
    keys = {legal_name_key(n) for n in filer_names if n} - {""}
    return any(legal_name_key(n) in keys for n in gleif_names if n)


def _gleif_names(node: dict) -> list[str]:
    return [n for n in [node.get("name"), *(node.get("other_names") or [])] if n]


def _place_verdict(place: str | None, home: str | None) -> tuple[bool | None, str]:
    """(True: the same place, None: no contradiction but no confirmation,
    False: another place; the words for the reason)."""
    if not place or not home:
        return None, "place not stated"
    if place == home:
        return True, f"registered {place}"
    if _country(place) != _country(home):
        return False, f"registered {place}, filer {home}"
    if "-" in place and "-" in home:
        return False, f"registered {place}, filer {home}"
    return None, f"registered {place}, filer {home}"      # "US" against "US-NY"


def _in_force(periods: list[dict] | None, status: str | None, day: str) -> bool:
    """A GLEIF relationship in force on ``day``: its RELATIONSHIP_PERIOD began
    by then and had not ended; without one, its status says (ACTIVE)."""
    spans = [p for p in periods or [] if p.get("type") == "RELATIONSHIP_PERIOD"]
    if not spans:
        return (status or "").upper() == "ACTIVE"
    for p in spans:
        start, end = (p.get("startDate") or "")[:10], (p.get("endDate") or "")[:10]
        if (not start or start <= day) and (not end or end >= day):
            return True
    return False


_STREET_WORDS = {"avenue": "ave", "street": "st", "drive": "dr", "road": "rd", "boulevard": "blvd",
                 "place": "pl", "suite": "ste", "floor": "fl", "north": "n", "south": "s",
                 "east": "e", "west": "w", "lane": "ln", "court": "ct", "parkway": "pkwy"}


def _address_key(street: str | None, city: str | None) -> str | None:
    """Street and city without the postcode (Domino's: 48108 and 48105), each
    street word spelled one way."""
    from app.scraper.mapper import normalize_address
    if not street or not city:
        return None
    words = (normalize_address(f"{street} {city}") or "").split()
    return " ".join(_STREET_WORDS.get(w, w) for w in words) or None


_CODE = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$")


def _as_code(place: str | None) -> str | None:
    """A place as a jurisdiction code: "US-DE" stays, Exhibit 21's
    "Delaware" becomes "US-DE", "Japan" "JP"; None when unmappable."""
    if not place or _CODE.match(place):
        return place or None
    from app.scraper.sec_ex21 import jurisdiction_country, jurisdiction_subdivision
    return jurisdiction_subdivision(place) or jurisdiction_country(place)


def _place_ok(place: str | None, other: str | None) -> bool:
    """Two places that do not contradict each other (either unknown, one the
    other's country, or equal)."""
    place, other = _as_code(place), _as_code(other)
    if not place or not other:
        return True
    if _country(place) != _country(other):
        return False
    return "-" not in place or "-" not in other or place == other


def same_legal_entity(node: dict, filer: Filer, *, today: str | None = None,
                      evidence: Evidence | None = None) -> tuple[str, str]:
    """(JOIN, evidence) when the GLEIF record on ``node`` and the EDGAR filer
    are one legal entity, else (REFUSE, reason). See the module docstring."""
    from app.scraper.sec_ex21 import legal_name_key
    evidence = evidence or Evidence()
    today = today or _today()
    day = reference_day(node, today)
    gnames = _gleif_names(node)
    # 1. the same legal name, legal form kept
    if not _same_name(gnames, names_at(filer, day)):
        return REFUSE, f"legal name differs ({node.get('name')!r} / {filer.name!r})"
    # 2. the same place of registration
    place = gleif_place(node)
    same_place, place_words = _place_verdict(place, filer.home)
    if same_place is False:
        return REFUSE, place_words
    # 4. veto: a child of a record that is the filer
    lei = node.get("lei_id")
    if lei:
        for parent in evidence.gleif_parents(lei) or []:
            if not (_same_name(parent.get("names") or [], names_at(filer, day))
                    and _place_verdict(parent.get("place"), filer.home)[0] is not False):
                continue
            if _in_force(parent.get("periods"), parent.get("status"), day) \
                    or _in_force(parent.get("periods"), parent.get("status"), today):
                start = next((p.get("startDate", "")[:10] for p in parent.get("periods") or []
                              if p.get("type") == "RELATIONSHIP_PERIOD"), "")
                return REFUSE, f"child of {parent.get('lei')}" + (f" since {start}" if start else "")
    status = (node.get("gleif_registration_status") or "").upper()
    weak = [why for why, hit in (
        ("place only " + (place or "unknown"), same_place is None),
        (f"registration {status}", status in _UNMAINTAINED),
        ("entity-supplied data", (node.get("validation_sources") or "").upper() == _SELF_REPORTED),
    ) if hit]
    basis = "name+place" if same_place else "name+country"
    if not weak:
        return JOIN, basis
    # 3. a weak record: the headquarters, or a subsidiary, at D
    hq, reg = addresses.read(node, addresses.HEADQUARTERS), addresses.read(node, addresses.REGISTERED)
    mine = _address_key(hq.get("street") or reg.get("street"), hq.get("city") or reg.get("city"))
    if mine:
        got = evidence.address_at(filer.cik, day)
        if got and _address_key(got[0], got[1]) == mine:
            note = f" ({got[2]})" if got[2] else ""
            return JOIN, f"{basis}+address@{day}{note}"
    if lei:
        held = [(legal_name_key(n), p) for n, p in evidence.held_at(filer.cik, day) or [] if n]
        if held:
            for child in evidence.gleif_children(lei) or []:
                if not _in_force(child.get("periods"), child.get("status"), day):
                    continue
                key = legal_name_key(child.get("name") or "")
                if key and any(key == k and _place_ok(child.get("place"), p) for k, p in held):
                    return JOIN, f"{basis}+subsidiary@{day} ({child.get('name')})"
    return REFUSE, f"weak record ({', '.join(weak)}) and no address or subsidiary at {day} to confirm it"


# ── live evidence ───────────────────────────────────────────────────────────

#: How long an answer of EDGAR's or GLEIF's is reused. A rebuild asks for one
#: filer many times; a server running for weeks must still see a rename.
CACHE_SECONDS = 24 * 3600
_CACHES: list[dict] = []


def _cached(fn):
    """`fn`'s answers per arguments, for `CACHE_SECONDS`."""
    store: dict = {}
    _CACHES.append(store)

    @wraps(fn)
    def wrapper(*args):
        hit = store.get(args)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = fn(*args)
        store[args] = (time.monotonic(), value)
        return value
    return wrapper


def clear_caches() -> None:
    """Forget every cached answer (tests; a manual re-check)."""
    for store in _CACHES:
        store.clear()


@_cached
def filer_facts(cik: str) -> Filer | None:
    """EDGAR's facts on a filer for the rule, or None when EDGAR cannot say."""
    from app.scraper.sec_edgar import _submissions, former_name_entries, incorporation_place
    try:
        sub = _submissions(cik)
    except Exception as exc:  # noqa: BLE001 - EDGAR unreachable: no verdict
        log.warning("legal identity: submissions for CIK %s unavailable: %s", cik, exc)
        return None
    name = ((sub or {}).get("name") or "").strip()
    if not name:
        return None
    business = (sub.get("addresses") or {}).get("business") or {}
    street = " ".join(p for p in ((business.get("street1") or "").strip(),
                                  (business.get("street2") or "").strip()) if p) or None
    return Filer(cik=cik, name=name, former_names=tuple(former_name_entries(sub) or ()),
                 home=incorporation_place(sub), street=street,
                 city=(business.get("city") or "").strip() or None)


_HEADER_BLOCK = re.compile(r"<(ISSUER|FILER|SUBJECT-COMPANY|FILED-BY)>(.*?)</\1>", re.S)


def header_address(sgml: str, cik: str) -> tuple[str, str] | None:
    """(street, city) of ``cik``'s BUSINESS-ADDRESS in a filing's SGML header."""
    want = str(int(cik))
    for _, block in _HEADER_BLOCK.findall(sgml or ""):
        got = re.search(r"<CIK>(\d+)", block)
        if not got or str(int(got.group(1))) != want:
            continue
        addr = re.search(r"<BUSINESS-ADDRESS>(.*?)</BUSINESS-ADDRESS>", block, re.S)
        if not addr:
            return None
        part = dict(re.findall(r"<([A-Z0-9-]+)>([^\n<]*)", addr.group(1)))
        street = " ".join(p.strip() for p in (part.get("STREET1"), part.get("STREET2")) if p and p.strip())
        city = (part.get("CITY") or "").strip()
        return (street, city) if street and city else None
    return None


@_cached
def filing_address_at(cik: str, day: str) -> tuple[str, str, str | None] | None:
    """(street, city, note) of the filer's business address as the header of
    its filing closest to ``day`` states it; today's address, noted, when no
    filing lies within a year of it."""
    from app.scraper.sec_edgar import _cik10, _get, _get_text, _submissions
    try:
        sub = _submissions(cik)
        rows = list(zip((sub.get("filings") or {}).get("recent", {}).get("accessionNumber", []),
                        (sub.get("filings") or {}).get("recent", {}).get("filingDate", [])))
        for page in (sub.get("filings") or {}).get("files") or []:
            if (page.get("filingFrom") or "") <= day <= (page.get("filingTo") or ""):
                older = _get(f"https://data.sec.gov/submissions/{page['name']}")
                rows += list(zip(older.get("accessionNumber", []), older.get("filingDate", [])))
    except Exception as exc:  # noqa: BLE001
        log.info("legal identity: filings of CIK %s unreadable: %s", cik, exc)
        return None
    target = date.fromisoformat(day)
    near = sorted(((abs(date.fromisoformat(d) - target), a) for a, d in rows if d), key=lambda r: r[0])
    if near and near[0][0] <= _HEADER_WINDOW:
        accession = near[0][1]
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
               f"{accession.replace('-', '')}/{accession}.hdr.sgml")
        try:
            got = header_address(_get_text(url) or "", cik)
        except Exception as exc:  # noqa: BLE001
            log.info("legal identity: header %s unreadable: %s", url, exc)
            got = None
        if got:
            return got[0], got[1], None
    facts = filer_facts(_cik10(cik))
    if facts and facts.street and facts.city:
        return facts.street, facts.city, "today's address: no filing within a year"
    return None


def _gleif_get(lei: str, rel: str) -> dict | None:
    import httpx

    from app.scraper.gleif_incremental import _HEADERS
    r = httpx.get(GLEIF_API.format(lei=lei, rel=rel), headers=_HEADERS, timeout=30,
                  follow_redirects=True, params={"page[size]": 200})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _record_names(attributes: dict) -> list[str]:
    entity = attributes.get("entity") or {}
    names = [(entity.get("legalName") or {}).get("name")]
    names += [n.get("name") for n in (entity.get("otherNames") or []) + (entity.get("transliteratedOtherNames") or [])]
    return [n for n in names if n]


def _record_place(attributes: dict) -> str | None:
    entity = attributes.get("entity") or {}
    return entity.get("jurisdiction") or (entity.get("legalAddress") or {}).get("country")


@_cached
def gleif_parents(lei: str) -> list[dict]:
    """The record's direct and ultimate parents with their relationship
    periods. A failure to ask is no parent (logged): the veto then cannot
    fire, and the rest of the rule still has to agree."""
    out = []
    for rel, rec_rel in (("direct-parent-relationship", "direct-parent"),
                         ("ultimate-parent-relationship", "ultimate-parent")):
        try:
            got = _gleif_get(lei, rel)
            if not got or not got.get("data"):
                continue
            r = got["data"]["attributes"]["relationship"]
            parent_lei = (r.get("endNode") or {}).get("id")
            rec = _gleif_get(lei, rec_rel)
            attrs = ((rec or {}).get("data") or {}).get("attributes") or {}
            out.append({"lei": parent_lei, "names": _record_names(attrs), "place": _record_place(attrs),
                        "periods": r.get("periods") or [], "status": r.get("status")})
        except Exception as exc:  # noqa: BLE001
            log.warning("legal identity: GLEIF %s of %s unreadable: %s", rel, lei, exc)
    return out


@_cached
def gleif_children(lei: str) -> list[dict]:
    """The record's direct and ultimate children, each with its name, place
    and relationship periods (two requests per kind)."""
    out, seen = [], set()
    for rel, recs in (("direct-child-relationships", "direct-children"),
                      ("ultimate-child-relationships", "ultimate-children")):
        try:
            rels = _gleif_get(lei, rel) or {}
            periods = {((x["attributes"]["relationship"].get("startNode") or {}).get("id")):
                       (x["attributes"]["relationship"].get("periods") or [],
                        x["attributes"]["relationship"].get("status"))
                       for x in rels.get("data") or []}
            for x in (_gleif_get(lei, recs) or {}).get("data") or []:
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                p, status = periods.get(x["id"], ([], None))
                attrs = x.get("attributes") or {}
                out.append({"lei": x["id"], "name": (_record_names(attrs) or [None])[0],
                            "place": _record_place(attrs), "periods": p, "status": status})
        except Exception as exc:  # noqa: BLE001
            log.warning("legal identity: GLEIF %s of %s unreadable: %s", rel, lei, exc)
    return out


def filer_held_at(cik: str, day: str) -> list[tuple[str, str | None]]:
    """(name, place) of what the filer held on ``day``: its OWNS edges open
    then (dates compared in Python — Cypher coerces ISO strings), and the
    Exhibit 21 of the fiscal year containing ``day``."""
    from app.database import db
    from app.scraper.sec_edgar import _cik10
    out: list[tuple[str, str | None]] = []
    with db.get_session() as session:
        rows = session.run(
            "MATCH (f:Entity {sec_cik: $cik})-[r:OWNS]->(c:Entity) "
            "RETURN c.name AS name, c.jurisdiction_code AS jc, c.country AS country, "
            "r.since AS since, r.until AS until", cik=_cik10(cik))
        for r in rows:
            since, until = str(r.get("since") or "")[:10], str(r.get("until") or "")[:10]
            if since and since <= day and (not until or until >= day):
                out.append((r["name"], r.get("jc") or r.get("country")))
    try:
        from app.scraper.sec_ex21 import _parse_first, annual_filings, exhibit_candidates
        for form, accession, filed, period in reversed(annual_filings(cik, include_older=True)):
            if (period or filed) >= day:
                got = _parse_first(exhibit_candidates(cik, form, accession, filed, period), cik)
                out += [(s["name"], s.get("jurisdiction")) for s in (got[0] if got else [])]
                break
    except Exception as exc:  # noqa: BLE001
        log.info("legal identity: Exhibit 21 of CIK %s at %s unreadable: %s", cik, day, exc)
    return out
