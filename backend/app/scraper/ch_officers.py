"""Companies House officers: the people who hold a seat at a UK company now.

The PSC snapshot names who CONTROLS a UK company; who RUNS it — directors,
secretaries, LLP members — is a separate register, read here from the
Companies House REST API (no free bulk file):

    GET https://api.company-information.service.gov.uk/company/{number}/officers
        ?items_per_page=100&start_index=N          (HTTP basic: the API key, no password)

    items[].name                  "SURNAME, Forename Middle" or "SURNAME, Forename, Title"  → Person
    items[].officer_role          director / secretary / llp-member / …  → HAS_ROLE.role
    items[].appointed_on          → HAS_ROLE.since
    items[].resigned_on           → ends a seat this source holds open
    items[].date_of_birth         {month, year} — never a day  → Person.birth_date "YYYY-MM"
    items[].nationality           "British" / "British,American"  → Person.nationalities ["GB", "US"]
    items[].links.officer.appointments  "/officers/{id}/appointments"  → Person.id "choff:{id}"

    GET …/officers/{id}/appointments?items_per_page=1
    items[0].name_elements        {title, forename, other_forenames, surname} — asked only for a
                                  name with a title part (`fetch_name_elements`, findings item 120)

What is taken, and what is not (user, 2026-11-13):

- **Current appointments only.** Of 9,111 appointments at dev's 280 UK
  companies 7,871 had ended (Barclays Investment Solutions: 301 listed, 6
  serving). A resigned officer is read only to END a seat a previous run
  wrote, on the day the register states; history builds from the first run.
- **People only.** A corporate officer (a company-secretary firm, a nominee
  company: 462 of the 9,111) is a company, and a seat runs from a person.
- **Name, role, the dates, month and year of birth, nationality** — the birth
  month is what keeps two people of one name apart; the nationality is stored
  as the other sources store it (ISO-2 where the demonym is recognised, the
  register's words otherwise). Not the correspondence address or the country
  of residence: nothing here needs them.

The officer id is Companies House's own key for a person across companies
(1,442 of dev's 6,226 officers sit at more than one of its companies), so
the node is `choff:{id}` — the register's key, as `chpsc:` is for a PSC.

Limit: 600 requests per five minutes for the key; the headers say what is
left, and the reader waits for the window when it is used up.
"""
import contextlib
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

API = "https://api.company-information.service.gov.uk"
PAGE_SIZE = 100
#: the register's public page for a company's officers — a seat's `source_url`
OFFICERS_PAGE = "https://find-and-update.company-information.service.gov.uk/company/{number}/officers"
#: requests kept in hand before the reader waits for the next window
RATE_RESERVE = 5

#: `officer_role` of a natural person → the seat's label. A role not listed is
#: written as the register words it ("cic-manager" → "Cic Manager"); a
#: `corporate-…` role is a company and is not written.
ROLES = {
    "director": "Director",
    "nominee-director": "Nominee Director",
    "secretary": "Company Secretary",
    "nominee-secretary": "Nominee Company Secretary",
    "llp-member": "LLP Member",
    "llp-designated-member": "LLP Designated Member",
}

_OFFICER_ID = re.compile(r"^/officers/([^/]+)/appointments$")
_TITLE = re.compile(r"^(mr|mrs|ms|miss|dr|sir|dame|lord|lady|prof|professor|the hon)\.?\s+", re.IGNORECASE)


class OfficersError(Exception):
    """The register could not be read for a company (not a 404)."""


def is_corporate(item: dict) -> bool:
    """A company holding the seat: its role says so, or it states a register."""
    return (item.get("officer_role") or "").startswith("corporate-") or bool(item.get("identification"))


def role_label(officer_role: str | None) -> str | None:
    """The seat's label for a person's `officer_role`; None for no role."""
    role = (officer_role or "").strip().lower()
    if not role:
        return None
    return ROLES.get(role) or role.replace("-", " ").title()


def officer_id(item: dict) -> str | None:
    """Companies House's id for the officer, from the appointments link."""
    link = (((item.get("links") or {}).get("officer") or {}).get("appointments") or "").strip()
    m = _OFFICER_ID.match(link)
    return m.group(1) if m else None


def _name_case(part: str) -> str:
    """An upper-case surname in name case: "O'NEILL-SMITH" → "O'Neill-Smith".
    A part already in mixed case is the register's own spelling and is kept."""
    if part != part.upper():
        return part
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), part)


def has_title_part(raw: str | None) -> bool:
    """The register wrote something after the forenames: "MERRICK, Julie, Dr".
    524 of the 8,746 names at dev's UK companies (findings item 120) — a title
    mostly (Mr 186, Dr 119, Sir 98), but the field is free text: "Managing
    Director", "CVO CBE", once more forenames ("John Anton")."""
    return (raw or "").count(",") >= 2


def officer_name(raw: str | None, elements: dict | None = None) -> tuple[str, str, str] | None:
    """"SURNAME, Forename Middle" → (first names, last name, full name); a name
    without a comma is one word order already. None for an empty name.

    ``elements``: the register's own split of the name (`fetch_name_elements`)
    — taken where it has a surname. Without it, what follows a second comma is
    the title field and is left out: "MERRICK, Julie, Dr" was read "Julie, Dr
    Merrick" (findings item 120)."""
    surname = ((elements or {}).get("surname") or "").strip()
    if surname:
        first = " ".join(p.strip() for p in (elements.get("forename"), elements.get("other_forenames"))
                         if p and p.strip())
        first, last = re.sub(r"\s+", " ", first), _name_case(re.sub(r"\s+", " ", surname))
        return first, last, f"{first} {last}".strip()
    text = re.sub(r"\s+", " ", raw or "").strip()
    if not text:
        return None
    if "," not in text:
        parts = text.split(" ")
        first, last = " ".join(parts[:-1]), _name_case(parts[-1])
    else:
        last, first, *_title = text.split(",")
        first, last = _TITLE.sub("", first.strip()), _name_case(last.strip())
    first = first.strip()
    if not last:
        return None
    return first, last, f"{first} {last}".strip()


def birth_month(item: dict) -> str | None:
    """"YYYY-MM" from the register's month and year; None without both."""
    dob = item.get("date_of_birth") or {}
    try:
        year, month = int(dob["year"]), int(dob["month"])
    except (KeyError, TypeError, ValueError):
        return None
    return f"{year:04d}-{month:02d}" if 1 <= month <= 12 and 1800 <= year <= 2100 else None


def nationalities(item: dict) -> list[str]:
    """The register's nationality as the graph's list: each demonym as an ISO-2
    code where it is recognised, verbatim otherwise — the PSC mapping. A person
    of two nationalities is one comma-joined text here ("British,American":
    33 of dev's 1,105 serving officers), so it is split first; "GB" is not
    written twice for "British,English"."""
    from app.scraper.companies_house_psc import _nationality
    out: list[str] = []
    for part in (item.get("nationality") or "").split(","):
        value = _nationality(part)
        if value and value not in out:
            out.append(value)
    return out


def _date(value: str | None) -> str | None:
    return value[:10] if value and re.match(r"^\d{4}-\d{2}-\d{2}", value) else None


def read_officer(item: dict) -> dict | None:
    """One appointment as the writer takes it: {"officer_id", "first_name",
    "last_name", "full_name", "birth_date", "nationalities", "role", "since",
    "until"} — None for
    a company, and for a record with no officer id, name or role."""
    if is_corporate(item):
        return None
    oid, role = officer_id(item), role_label(item.get("officer_role"))
    name = officer_name(item.get("name"), item.get("name_elements"))
    if not (oid and name and role):
        return None
    first, last, full = name
    return {"officer_id": oid, "first_name": first, "last_name": last, "full_name": full,
            "birth_date": birth_month(item), "nationalities": nationalities(item), "role": role,
            "since": _date(item.get("appointed_on")), "until": _date(item.get("resigned_on"))}


def _wait_for_window(response: httpx.Response, sleep=time.sleep) -> None:
    """Wait for the next five-minute window when this one is used up."""
    try:
        remain = int(response.headers.get("x-ratelimit-remain", RATE_RESERVE + 1))
        reset = int(response.headers.get("x-ratelimit-reset", 0))
    except ValueError:
        return
    if remain <= RATE_RESERVE or response.status_code == 429:
        pause = min(max(reset - time.time(), 1), 305)
        log.info("Companies House: request window used up, waiting %.0f s", pause)
        sleep(pause)


def fetch_officers(client: httpx.Client, number: str, sleep=time.sleep) -> list[dict] | None:
    """Every appointment the register lists for a company, as the API gives
    them; None when it knows no such company (404). One request per 100."""
    items: list[dict] = []
    start = 0
    while True:
        for attempt in (1, 2):
            response = client.get(f"{API}/company/{number}/officers",
                                  params={"items_per_page": PAGE_SIZE, "start_index": start})
            _wait_for_window(response, sleep)
            if response.status_code != 429:
                break
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise OfficersError(f"HTTP {response.status_code} for company {number}")
        data = response.json()
        page = data.get("items") or []
        items += page
        start += PAGE_SIZE
        if not page or start >= int(data.get("total_results") or 0):
            return items


def fetch_name_elements(client: httpx.Client, oid: str, sleep=time.sleep) -> dict | None:
    """The register's own split of an officer's name — {"title", "forename",
    "other_forenames", "surname"} — from their appointments page (one request,
    one item). The officers list gives the name as one text, and what follows
    its second comma is a free-text title field (findings item 120). A failed
    request is tried once more (user, 2026-11-14); None when both fail or the
    page has no split — the caller then leaves the title part out."""
    for attempt in (1, 2):
        try:
            response = client.get(f"{API}/officers/{oid}/appointments", params={"items_per_page": 1})
        except httpx.HTTPError as exc:
            log.warning("Companies House: name of officer %s not read (attempt %d): %s", oid, attempt, exc)
            continue
        _wait_for_window(response, sleep)
        if response.status_code == 200:
            try:
                items = response.json().get("items") or []
            except ValueError:
                items = []
            elements = (items[0].get("name_elements") if items else None) or {}
            return elements if (elements.get("surname") or "").strip() else None
        if response.status_code == 404:
            return None                      # the register has no such page: asking again changes nothing
        log.warning("Companies House: name of officer %s not read (attempt %d): HTTP %s",
                    oid, attempt, response.status_code)
    return None


def add_name_elements(client: httpx.Client, items: list[dict], known: dict, sleep=time.sleep) -> int:
    """Give every serving person whose list name has a title part the
    register's own split (`item["name_elements"]`), asked once per officer id
    for the run (``known``). Returns how many were asked for. Resigned and
    corporate officers are not: nobody writes their names."""
    asked = 0
    for item in items:
        if is_corporate(item) or item.get("resigned_on") or not has_title_part(item.get("name")):
            continue
        oid = officer_id(item)
        if not oid:
            continue
        if oid not in known:
            known[oid] = fetch_name_elements(client, oid, sleep)
            asked += 1
        if known[oid]:
            item["name_elements"] = known[oid]
    return asked


def client(api_key: str) -> httpx.Client:
    """The API client: the key as the basic-auth user, no password."""
    return httpx.Client(auth=(api_key, ""), timeout=30, headers={"Accept": "application/json"})


# ── the writes ────────────────────────────────────────────────────────────────

SOURCE_KEY = "ch_officers"


def person_id(oid: str) -> str:
    return f"choff:{oid}"


def _upsert_officer(session, rec: dict, source_id: str, now: str) -> str:
    """The officer's Person node: found by the register's id — or where a merge
    moved it (`MergedId`) — else created. A birth month and the nationalities
    fill an empty field and never replace what another source stated; a person
    without a nationality gets no field, never an empty list.

    The name is the register's while the node is still the register's own —
    its id the officer's, its source this one: a re-run corrects it (56 people
    on dev were written "Hidehiko, Mr. Noda", findings item 120). A node a
    merge moved the officer to keeps the name its merge chose."""
    from app.merged_ids import resolve_current_id
    from app.scraper.graph_writer import _person_search_text
    pid = person_id(rec["officer_id"])
    if not session.run("MATCH (p:Person {id: $id}) RETURN p.id AS id", id=pid).single():
        survivor = resolve_current_id(session, pid)
        if survivor:
            pid = survivor
        else:
            session.run(
                """
                CREATE (p:Person {
                    id: $id, first_name: $first, last_name: $last, full_name: $full,
                    birth_date: $birth, nationalities: $nats, source_id: $source_id, aliases: [],
                    search_text: $search_text, created_at: $now, last_scraped_at: $now
                })
                """,
                id=pid, first=rec["first_name"], last=rec["last_name"], full=rec["full_name"],
                birth=rec["birth_date"], nats=rec.get("nationalities") or None,
                source_id=source_id, now=now,
                search_text=_person_search_text(rec["full_name"], None))
            return pid
    elif rec.get("full_name"):
        session.run("MATCH (p:Person {id: $id}) WHERE p.source_id = $source_id AND p.full_name <> $full "
                    "SET p.first_name = $first, p.last_name = $last, p.full_name = $full, "
                    "p.search_text = $search_text",
                    id=pid, source_id=source_id, first=rec["first_name"], last=rec["last_name"],
                    full=rec["full_name"], search_text=_person_search_text(rec["full_name"], None))
    session.run("MATCH (p:Person {id: $id}) SET p.last_scraped_at = $now, "
                "p.birth_date = COALESCE(p.birth_date, $birth), "
                "p.nationalities = CASE WHEN size(COALESCE(p.nationalities, [])) > 0 "
                "THEN p.nationalities ELSE $nats END",
                id=pid, now=now, birth=rec["birth_date"], nats=rec.get("nationalities") or None)
    return pid


def write_company_officers(entity_id: str, number: str, items: list[dict], source_id: str,
                           credibility: int) -> dict:
    """Write a company's serving officers and end the seats of those who left.

    A serving person → the Person (`choff:{id}`) and a HAS_ROLE seat dated by
    `appointed_on`. A resigned one → nothing new: `resigned_on` ends the seat
    this source holds open for that person in that role, and only that.
    Returns {"written", "ended", "corporate", "resigned", "unreadable"}."""
    from datetime import datetime, timezone

    from app.database import db
    from app.merged_ids import resolve_current_id
    from app.roles import canonical_role
    from app.scraper.edge_schema import READ_FIELD
    from app.scraper.graph_writer import _upsert_role
    out = {"written": 0, "ended": 0, "corporate": 0, "resigned": 0, "unreadable": 0}
    url = OFFICERS_PAGE.format(number=number)
    now = datetime.now(timezone.utc).isoformat()
    serving, left = [], []
    for item in items:
        if is_corporate(item):
            out["corporate"] += 1
            continue
        rec = read_officer(item)
        if rec is None:
            out["unreadable"] += 1
        elif rec["until"]:
            out["resigned"] += 1
            left.append(rec)
        else:
            serving.append(rec)
    with db.get_session() as session:
        held = {(r["pid"], canonical_role(r["role"])) for r in session.run(
            "MATCH (p:Person)-[r:HAS_ROLE]->(e:Entity {id: $eid}) WHERE r.source_id = $sid AND r.until IS NULL "
            "RETURN p.id AS pid, r.role AS role", eid=entity_id, sid=source_id)}
        people = [(_upsert_officer(session, rec, source_id, now), rec) for rec in serving]
        ended = []
        for rec in left if held else []:
            pid = person_id(rec["officer_id"])
            if (pid, canonical_role(rec["role"])) not in held:
                pid = resolve_current_id(session, pid) or pid
            if (pid, canonical_role(rec["role"])) in held:
                ended.append((pid, rec))
    for pid, rec in people:
        _upsert_role(pid, entity_id, rec["role"], source_id, since=rec["since"], source_url=url,
                     credibility_score=credibility, read_from=READ_FIELD)
        out["written"] += 1
    for pid, rec in ended:
        _upsert_role(pid, entity_id, rec["role"], source_id, since=rec["since"], until=rec["until"],
                     source_url=url, credibility_score=credibility, read_from=READ_FIELD)
        out["ended"] += 1
    return out


def companies_in_graph(only: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """The UK companies the graph holds a Companies House number for:
    [{"id", "name", "number"}], by number; `only` narrows to those numbers."""
    from app.db.arcadedb import run_query
    rows = run_query("MATCH (e:Entity) WHERE e.companies_house_id IS NOT NULL "
                     "RETURN e.id AS id, e.name AS name, e.companies_house_id AS number") or []
    wanted = {n.strip().upper() for n in only or []}
    out = sorted(({"id": r["id"], "name": r.get("name"), "number": str(r["number"]).strip().upper()}
                  for r in rows if r.get("number")), key=lambda r: r["number"])
    if wanted:
        out = [r for r in out if r["number"] in wanted]
    return out[:limit] if limit else out


def run_ch_officers(only: list[str] | None = None, limit: int | None = None, dry_run: bool = False) -> dict:
    """Read the serving officers of every UK company in the graph and write
    them (`write_company_officers`). A manual run: no schedule calls it.

    `dry_run` reads and counts, writing nothing. Returns {"status", "companies",
    "read", "unknown", "failed", "written", "ended", "corporate", "resigned",
    "unreadable", "names_asked"}; status `no_api_key` without COMPANIES_HOUSE_API_KEY, `disabled`
    when the source is switched off in the catalogue."""
    from app.config import settings
    from app.scraper.graph_writer import _ensure_source
    from app.scraper.run_log import record_run
    from app.scraper.sources import KNOWN_SOURCES, get_source_enabled
    if not settings.COMPANIES_HOUSE_API_KEY:
        return {"status": "no_api_key"}
    meta = KNOWN_SOURCES[SOURCE_KEY]
    # the catalogue toggle (Scraper panel); a dry run does not ask — the
    # question creates the catalogue's Source nodes, and a dry run writes nothing
    if not dry_run and not get_source_enabled(SOURCE_KEY):
        return {"status": "disabled"}
    companies = companies_in_graph(only, limit)
    out = {"status": "dry-run" if dry_run else "ok", "companies": len(companies), "read": 0, "unknown": 0,
           "failed": 0, "written": 0, "ended": 0, "corporate": 0, "resigned": 0, "unreadable": 0,
           "names_asked": 0}
    # a dry run writes nothing — not a run-log row either
    logged = contextlib.nullcontext({}) if dry_run else \
        record_run("ch-officers", "all" if not only else ",".join(sorted(only)))
    names: dict = {}                    # officer id → the register's split of the name, asked once
    with logged as run, client(settings.COMPANIES_HOUSE_API_KEY) as http:
        source_id = None if dry_run else _ensure_source(meta["label"], meta["url"], meta["credibility"])
        for company in companies:
            try:
                items = fetch_officers(http, company["number"])
            except (OfficersError, httpx.HTTPError) as exc:
                out["failed"] += 1
                log.warning("Companies House: officers of %s (%s) not read: %s",
                            company["name"], company["number"], exc)
                continue
            if items is None:
                out["unknown"] += 1
                continue
            out["read"] += 1
            if dry_run:
                recs = [read_officer(i) for i in items if not is_corporate(i)]
                counts = {"corporate": sum(1 for i in items if is_corporate(i)),
                          "unreadable": sum(1 for r in recs if r is None),
                          "resigned": sum(1 for r in recs if r and r["until"]),
                          "written": sum(1 for r in recs if r and not r["until"]), "ended": 0}
            else:
                try:
                    out["names_asked"] += add_name_elements(http, items, names)
                except Exception as exc:  # noqa: BLE001 — a name's title part is not worth a company
                    log.warning("Companies House: name parts at %s not read: %s", company["number"], exc)
                counts = write_company_officers(company["id"], company["number"], items, source_id,
                                                meta["credibility"])
            for key, n in counts.items():
                out[key] += n
        run["total"] = out["written"]
        run["note"] = (f"{out['read']} of {out['companies']} companies, {out['written']} seats, "
                       f"{out['ended']} ended, {out['failed']} failed")
    return out
