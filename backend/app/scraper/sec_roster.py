"""The directors and officers an issuer's own proxy names, whose seat the
insider window misses (findings item 115).

`sec_edgar.fetch_executives` lists an insider from a Form 3/4 filed inside
`FORM4_WINDOW_DAYS`, on the assumption that every sitting director files at
least once a year (the annual grant). A board that gets no stock grants does
not: Berkshire Hathaway's window held 2 of its 13 directors, Tesla's 3. The
proxy's ownership table names the whole board and the executive officers as
of its record date, and EDGAR lists every reporting owner an issuer ever had
(`cgi-bin/own-disp?action=getissuer`), with the owner's CIK — but also the
people long gone, since leaving a board needs no filing. Neither alone is a
roster; together they are:

    a person the proxy names  +  the same person among the issuer's reporting
    owners, as a director who is no officer  →  a Director seat.

The seat's start is the person's own newest Form 3 about the issuer (their
submissions, Form 3s only); without one (a seat older than electronic filing)
it has no start, and the proxy is its source. A person in the owner list the
proxy does not name gets nothing: that is how the departed stay out.

Only where silence is the rule. At a board that grants shares every year a
director who is in the proxy and not in the window has usually LEFT — at the
very meeting the proxy was written for: Pool's proxy of 2026 names Carlos
Sabater, whose last grant is of May 2025 while every other director filed one
in May 2026 (13 of the 33 people a first version found on dev were such). A
seat is given

    - on a quiet board — the window lacks at least as many of the proxy's
      outside directors as it holds (Berkshire 8 lacking, 1 held; Tesla 5 and
      3) — or
    - to a director who has filed nothing since their Form 3 about the issuer,
      about any company: no grants to file for (a shareholder's nominee — Eve's
      Luis Carlos Affonso, LifeStance's Katherine Wood).

Outside directors only. The table also lists the named executive officers of
the last fiscal year, and those include people who have left — Molson Coors'
2026 proxy lists Gavin Hattersley, who retired as chief executive in 2025; a
departed officer files nothing more, so he looks exactly like a director
without grants. An officer who serves files for the yearly grant and is in the
window; a board is listed as it sits.
"""
import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.scraper.names import _NICKNAMES

log = logging.getLogger(__name__)

OWNERS_URL = "https://www.sec.gov/cgi-bin/own-disp"
#: proxy-named people looked up per issuer (a board and its officers: 10–25)
MAX_ROSTER_LOOKUPS = 30
#: a person's own Form 3s opened to find the one about the issuer — one per
#: company they ever joined
MAX_PERSON_FORM3 = 25

#: an insider's filings after the Form 3: transactions and the annual statement
_LATER_FORMS = frozenset({"4", "4/A", "5", "5/A"})

_SUFFIXES = {"jr": "jr", "junior": "jr", "sr": "sr", "senior": "sr", "ii": "ii", "iii": "iii", "iv": "iv"}
#: what a proxy appends to a name: footnote marks, degrees, a title after a comma
_MARKS = re.compile(r"[†‡§*¹²³⁴⁵⁶⁷⁸⁹⁰]+|\(\s*\d+\s*\)")
_DEGREES = {"phd", "md", "cpa", "esq", "dr", "mr", "mrs", "ms", "hon"}


def parse_owners(html: str) -> list[dict]:
    """EDGAR's "Ownership Reports from" page of an issuer → its reporting
    owners: [{"name", "cik", "type"}] — the table headed "Owner | Filings |
    Transaction Date | Type of Owner". The date column is not read: it is
    neither the first nor the newest filing (Warren E. Buffett's reads
    2022-06-14)."""
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        head = rows[0].get_text(" ") if rows else ""
        if "Type of Owner" not in head or "Owner" not in head or "Filings" not in head:
            continue
        out = []
        for row in rows[1:]:
            cells = [re.sub(r"\s+", " ", td.get_text(" ")).strip() for td in row.find_all("td")]
            if len(cells) < 4 or not re.fullmatch(r"\d{1,10}", cells[1]):
                continue
            out.append({"name": cells[0], "cik": cells[1].zfill(10), "type": cells[3]})
        return out
    return []


def issuer_owners(cik: str) -> list[dict]:
    """Every reporting owner EDGAR lists for the issuer (one request; the
    owner table is not paged — JPMorgan's 95 owners come on the first page)."""
    from app.scraper import sec_edgar
    return parse_owners(sec_edgar._get_text(OWNERS_URL, {"action": "getissuer", "CIK": sec_edgar._cik10(cik)}))


def _words(text: str) -> list[str]:
    return [w.strip(".,").lower() for w in text.split() if w.strip(".,")]


def person_parts(name: str) -> dict | None:
    """A name as a proxy writes it → {"given": [...], "surname", "suffix",
    "initials"}: "Thomas S. Murphy, Jr." → given [thomas, s], surname murphy,
    suffix jr; "JB Straubel" → initials True (two capitals for two given
    names); "David R. Little, Chairman of the Board" → the title is cut. None
    for a single word."""
    text = _MARKS.sub(" ", name or "")
    head, *tail = [p.strip() for p in text.split(",")]
    suffix = None
    for part in tail:                         # ", Jr." is a suffix; ", Chairman …" a title
        w = _words(part)
        if len(w) == 1 and w[0] in _SUFFIXES:
            suffix = _SUFFIXES[w[0]]
    raw = [w for w in head.split() if w.strip(".,")]
    words = [w for w in _words(head) if w not in _DEGREES]
    if words and words[-1] in _SUFFIXES:
        suffix = _SUFFIXES[words.pop()]
    if len(words) < 2:
        return None
    first_raw = next((w.strip(".,") for w in raw if w.strip(".,").lower() == words[0]), "")
    return {"given": words[:-1], "surname": words[-1], "suffix": suffix,
            "initials": len(first_raw) == 2 and first_raw.isupper()}


def _owner_bag(name: str) -> tuple[list[str], str | None]:
    """An owner's name as EDGAR writes it ("MURPHY THOMAS S JR", "Buffett
    Susan A.", "KEOUGH DONALD R /NY") → its words and its suffix. The order is
    not relied on: EDGAR's is surname first, but not always."""
    words = [w for w in _words(re.sub(r"/[A-Za-z]+/?", " ", name or "")) if w not in _DEGREES]
    suffix = next((_SUFFIXES[w] for w in words if w in _SUFFIXES), None)
    return [w for w in words if w not in _SUFFIXES], suffix


def _first_name_matches(person: dict, rest: list[str]) -> bool:
    """The proxy's first name against an owner's given words: the same name, a
    nickname of it ("Joe" ~ "Joseph"), or initials for the given names in
    their order ("JB" ~ "Jeffrey B"). Never a shared first letter alone:
    "John" is not "James"."""
    first = person["given"][0]
    if person["initials"]:
        return len(rest) >= 2 and rest[0][0] == first[0] and rest[1][0] == first[1]
    return any(w == first or _NICKNAMES.get(first) == w or _NICKNAMES.get(w) == first for w in rest)


def match_owner(name: str, owners: list[dict]) -> dict | None:
    """The one reporting owner a proxy's name stands for, or None — strictly:
    the surname and the first name (`_first_name_matches`). Where father and
    son are both owners (MURPHY THOMAS S and MURPHY THOMAS S JR) only a suffix
    the proxy states decides — "Thomas S. Murphy, Jr." — and a proxy that
    states none names neither; a middle initial tells two of a name apart
    ("Susan A. Buffett" beside BUFFETT SUSAN T). More than one left → none: a
    seat is not given on a guess."""
    person = person_parts(name)
    if not person:
        return None
    found = []
    for owner in owners:
        bag, suffix = _owner_bag(owner["name"])
        if person["surname"] not in bag:
            continue
        rest = [w for w in bag if w != person["surname"]]
        if _first_name_matches(person, rest):
            found.append((owner, rest, suffix))
    if len(found) > 1 and len({f[2] for f in found}) > 1:
        # father and son are both owners: only a suffix the proxy states decides
        found = [f for f in found if f[2] == person["suffix"]] if person["suffix"] else []
    middle = [w[0] for w in person["given"][1:]]
    if len(found) > 1 and middle and not person["initials"]:
        found = [f for f in found if len(f[1]) > 1 and f[1][1][0] == middle[0]]
    return found[0][0] if len(found) == 1 else None


def has_seat(name: str, executives: list[dict]) -> bool:
    """Whether the insider window already lists the person — generously, the
    surname and one shared given-name initial: a wrong yes only leaves a
    lookup out, a wrong no would look the same person up again."""
    person = person_parts(name)
    if not person:
        return True
    for ex in executives:
        words = [w for w in _words(ex.get("name") or "") if w not in _SUFFIXES]
        if person["surname"] in words and {w[0] for w in words if w != person["surname"]} & \
                {g[0] for g in person["given"]}:
            return True
    return False


def seated_directors(people: list[str], executives: list[dict]) -> int:
    """How many of the proxy's people the window lists as a sitting director
    (role "Director", not ended) — what a quiet board is measured against."""
    directors = [e for e in executives if e.get("role") == "Director" and not e.get("former") and not e.get("until")]
    return sum(1 for p in people if has_seat(p, directors))


def roster_people(html: str) -> list[str]:
    """The people the proxy's ownership tables name — directors, nominees and
    executive officers, and any person among the 5 % holders — in table
    order. Companies are left out."""
    from app.scraper.mapper import has_entity_suffix
    from app.scraper.proxy_statement import read_holder_rows
    return [r["name"] for r in read_holder_rows(html)
            if not has_entity_suffix(r["name"]) and person_parts(r["name"])]


def is_outside_director(owner_type: str) -> bool:
    """EDGAR's "Type of Owner" names a director who is no officer: "director",
    "director, 10 percent owner" — not "director, officer: Vice Chairman",
    not "10 percent owner"."""
    kinds = [k.strip().lower() for k in (owner_type or "").split(",")]
    return "director" in kinds and not any(k.startswith("officer") for k in kinds)


def own_form3(person_cik: str, issuer_cik: str) -> dict | None:
    """The person's newest Form 3 about the issuer, from their own
    submissions: the parsed record with its link and date, or None. A Form 3
    is filed once per company a person joins, so the newest MAX_PERSON_FORM3
    reach back decades — where their Form 4s about other companies would bury
    it (Kenneth Chenault's Berkshire Form 3 of 2020 lies behind his Airbnb
    filings). ``silent_since``: the person filed no Form 4 or 5 after it,
    about any company."""
    from app.scraper import sec_edgar
    try:
        recent = (sec_edgar._submissions(person_cik).get("filings") or {}).get("recent") or {}
    except httpx.HTTPError:
        return None
    issuer = sec_edgar._cik_int(issuer_cik)
    opened = 0
    for form, acc, doc, filed in zip(recent.get("form") or [], recent.get("accessionNumber") or [],
                                     recent.get("primaryDocument") or [], recent.get("filingDate") or []):
        if form not in ("3", "3/A"):
            continue
        if opened >= MAX_PERSON_FORM3:
            break
        doc = doc.split("/")[-1] if doc else ""
        if not doc:
            continue
        opened += 1
        try:
            xml = sec_edgar._get_text(f"{sec_edgar.ARCHIVES_URL}/{sec_edgar._cik_int(person_cik)}/"
                                      f"{acc.replace('-', '')}/{doc}")
        except httpx.HTTPError:
            continue
        record = sec_edgar._parse_form34_xml(xml)
        if not record or not record.get("issuer_cik") or sec_edgar._cik_int(record["issuer_cik"]) != issuer:
            continue
        record["source_url"] = sec_edgar._filing_index_url(issuer_cik, acc) or None
        record["source_date"] = filed
        # nothing filed since, about any company: a director with no grants
        record["silent_since"] = not any(
            f in _LATER_FORMS and d > filed
            for f, d in zip(recent.get("form") or [], recent.get("filingDate") or []))
        return record
    return None


def roster_insiders(cik: str, executives: list[dict], submissions: dict | None = None) -> list[dict]:
    """The proxy-named outside directors `executives` (the insider window's
    list) lacks — on a quiet board, or silent since their own Form 3 (the
    module's head) — as records of the same shape: name, role "Director",
    person_cik, source_url, source_date, since; with no holding: a Form 3's
    count is as old as the seat. The person's newest Form 3 about the issuer
    decides where there is one (JB Straubel: Tesla's chief technology officer
    in 2010, a director since 2023), else the owner list's type. [] when the
    issuer has no ownership table within `proxy_statement.TABLE_MAX_AGE_DAYS`,
    or when EDGAR cannot be read (the insiders section is marked failed, as
    for the window)."""
    from app.scraper import sec_edgar
    from app.scraper.proxy_statement import latest_table_filing
    try:
        subs = submissions if submissions is not None else sec_edgar._submissions(cik)
        filing = latest_table_filing(subs)
        if not filing:
            return []
        people = roster_people(sec_edgar._get_text(filing["url"]))
        missing = [p for p in people if not has_seat(p, executives)]
        if not missing:
            return []
        owners = issuer_owners(cik)
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: roster of CIK=%s not read: %s", cik, exc)
        sec_edgar._section_failed(sec_edgar.SECTION_INSIDERS, exc)
        return []
    listed = {e.get("person_cik") for e in executives if e.get("person_cik")}
    found: list[dict] = []
    for name in missing[:MAX_ROSTER_LOOKUPS]:
        owner = match_owner(name, owners)
        if not owner or owner["cik"] in listed:
            continue
        listed.add(owner["cik"])
        form3 = own_form3(owner["cik"], cik)
        # a joint Form 3 is parsed for its first reporting owner — the fund a
        # director sits for (Amberjack Capital Partners on Innovex's): it dates
        # the seat, and the owner list says whose it is
        own = bool(form3) and form3.get("person_cik") == owner["cik"]
        if own and (form3.get("former") or form3.get("role") != "Director"):
            continue                      # an officer then, or gone: not a seat the proxy's row proves
        if not own and not is_outside_director(owner["type"]):
            continue                      # a 5 % holder or an officer
        record = {"name": sec_edgar._normalize_sec_name(" ".join(_owner_bag(owner["name"])[0])),
                  "role": "Director", "title": "", "person_cik": owner["cik"], "former": False,
                  "issuer_cik": sec_edgar._cik10(cik), "document_type": None, "period_of_report": None,
                  "source_url": filing["url"], "source_date": filing["date"], "since": None}
        if own:
            record.update(form3)
        if form3:
            record.update({"since": form3.get("period_of_report"), "source_url": form3.get("source_url"),
                           "source_date": form3.get("source_date"), "document_type": form3.get("document_type"),
                           "period_of_report": form3.get("period_of_report")})
        # a seat, not a holding: a Form 3's count is as old as the seat
        record.update({"until": None, "shares_owned": None, "share_class": None, "holdings": {}, "blocks": []})
        record["roster"] = filing["url"]      # the proxy that names the person now
        record["silent_since"] = bool(form3 and form3.get("silent_since"))
        found.append(record)
    # Silence must be the rule, or the person's own: where the board files
    # every year, a director the window lacks has left (see the module's head).
    quiet_board = bool(found) and len(found) >= seated_directors(people, executives)
    out = [r for r in found if quiet_board or r["silent_since"]]
    for r in out:
        log.info("SEC EDGAR: %r (%s) is named in the %s of %s and is a director among the issuer's "
                 "reporting owners — seat%s (%s)", r["name"], r["person_cik"], filing["form"], filing["date"],
                 f" since {r['since']}" if r.get("since") else "",
                 "a board that files little" if quiet_board else "nothing filed since the Form 3")
    for r in found:
        if r not in out:
            log.info("SEC EDGAR: %r (%s) is named in the %s of %s but the board files yearly and they "
                     "did not — no seat", r["name"], r["person_cik"], filing["form"], filing["date"])
    return out
