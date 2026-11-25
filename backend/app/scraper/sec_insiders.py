"""
SEC EDGAR — insiders: officers and directors from Form 3/4/5, their
departures from 8-K Item 5.02, and an insider's holding with the vehicles it
is held through.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
from datetime import datetime, timedelta
import logging
import xml.etree.ElementTree as ET

import httpx
from app.roles import canonical_role
from app.scraper.mapper import (has_entity_suffix)
from app.scraper import sec_13dg_text, sec_client, sec_lookup, sec_shares

log = logging.getLogger(__name__)
MAX_FORM4_FETCH  = 60     # max unique insiders listed from Form 3/4
#: Form 3/4 documents opened per scrape, at most: a runaway guard, never the
#: rule — the window is (below). At 300 it was a rule: TSMC filed 333 Form 3/4s
#: in eight months, and its seven directors, whose one filing is the Form 3 of
#: 2026-03-18, fell past it the day 30 new Form 4s came in (findings item 105).
MAX_FORM4_SCAN   = 1000
#: Insiders are read from the Form 3/4s of the last ~15 months, counted back
#: from the issuer's newest one: every sitting director files at least once a
#: year (the annual grant). The window replaced a cap of 50 documents, which a
#: few officers' repeat filings used up — Bank of America listed 8 of its 29
#: insiders, none of the nine directors of its 2026-05-06 grant (findings item 9).
FORM4_WINDOW_DAYS = 456
MAX_FORM3_SCAN   = 40     # Form 3s read past the insider cap, for seat start dates
MAX_PERSON_FORM4 = 20     # a candidate's own Form 4s opened to find one about the issuer
MAX_8K_FETCH     = 12     # newest 8-Ks with Item 5.02 read for departures


#: Form 4's officer-title box when the filer wrote the title in the remarks
#: box instead — EDGAR's own placeholder, not a position ("See Remarks",
#: "See remarks below").
_SEE_REMARKS = re.compile(r"^\s*see\s+remarks?\b", re.IGNORECASE)

#: The roles `_title_to_role` names on its own. Anything else it returns is the
#: title as written — fine for a title box, never for a remarks paragraph.
_NAMED_ROLES = frozenset({"CEO", "CFO", "COO", "CTO", "General Counsel", "Chairman", "President"})


#: A remarks box that is a sentence, not a title. Televisa's Form 4 for its
#: cable company's chief says "Mr. Valim is the Chief Executive Officer of
#: izzi, Grupo Televisa, S.A.B.'s cable company, and on that basis he may be
#: considered an officer of Grupo Televisa" — a CEO of a SUBSIDIARY, which a
#: substring match would have seated as Televisa's CEO (2026-10-25).
_SENTENCE = re.compile(r"\b(is|are|was|were|serves?|served|became|may|considered|also)\b|\. |^(mr|ms|mrs|dr)\b",
                       re.IGNORECASE)
_TITLE_MAX = 120


def _looks_like_title(text: str) -> bool:
    """Short and declarative — "Senior Vice President and Chief Financial
    Officer" — not a sentence about the filer."""
    text = (text or "").strip()
    return 0 < len(text) <= _TITLE_MAX and not _SENTENCE.search(text)


def _officer_role(title: str, remarks: str = "") -> str:
    """An officer's role from the title box — or, when the box says "See
    Remarks", from the remarks when they READ AS A TITLE and name a role we
    know ("Senior Vice President and Chief Financial Officer" → CFO), else
    plain "Officer". A remarks sentence is never mined for a role: it can
    describe a post at another company. The placeholder itself reached the
    graph as a role on five dev edges, in two spellings, until 2026-10-25."""
    if not _SEE_REMARKS.match(title):
        return _title_to_role(title)
    if not _looks_like_title(remarks):
        return "Officer"
    role = _title_to_role(remarks)
    return role if role in _NAMED_ROLES else "Officer"


def _title_to_role(title: str) -> str:
    """Map an officer title string to a canonical role."""
    t = title.lower()
    if "chief executive" in t or t == "ceo":
        return "CEO"
    if "chief financial" in t or t == "cfo":
        return "CFO"
    if "chief operating" in t or t == "coo":
        return "COO"
    if "chief technology" in t or "chief technical" in t or t == "cto":
        return "CTO"
    if "general counsel" in t or "chief legal" in t:
        return "General Counsel"
    if "chairman" in t:
        return "Chairman"
    if "president" in t and "vice" not in t:
        return "President"
    return title or "Officer"


# ── Executives from Form 3/4 (structured XML) ────────────────────────────────

#: The forms the insider window reads. Form 5 is the annual statement of the
#: transactions an insider did not have to report on a Form 4 (gifts, small
#: acquisitions) and has the same XML; a director whose newest filing about
#: the issuer is a Form 5 was not listed — WesBanco's Michael J Crawford
#: (Form 5 of 2026-02-10, isDirector) had no seat (findings item 119).
INSIDER_FORMS = ("3", "4", "5", "3/A", "4/A", "5/A")


def _parse_form34_xml(xml_text: str) -> dict | None:
    """
    Parse a Form 3 or Form 4 XML document.
    Returns {name, title, role} if the reporting person is an officer or director,
    None otherwise.

    Form 3/4 XML schema (key fields):
      issuer/issuerCik                                   — whose shares (verified by caller)
      reportingOwner/reportingOwnerId/rptOwnerName       — person's name
      reportingOwner/reportingOwnerRelationship/isOfficer   — "1" or "0"
      reportingOwner/reportingOwnerRelationship/isDirector  — "1" or "0"
      reportingOwner/reportingOwnerRelationship/officerTitle — e.g. "Chief Executive Officer",
          or "See Remarks" when the title is in the filing-level <remarks> box

    The result carries `issuer_cik` so the caller can check the form is about
    the company being scraped. A company's submissions feed also lists forms it
    FILED about other issuers — Embraer's Form 4s about Eve Holding put
    "EMBRAER S.A." into Embraer's own executive list as a "Director", because
    isDirector meant director OF EVE. Unlike the 13D cover page this is exact:
    the XML states the issuer's CIK outright.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    owner = root.find(".//reportingOwner")
    if owner is None:
        return None

    issuer_cik = (root.findtext(".//issuer/issuerCik") or "").strip() or None

    name_elem = owner.find(".//rptOwnerName")
    if name_elem is None or not (name_elem.text or "").strip():
        return None

    rel = owner.find("reportingOwnerRelationship")
    if rel is None:
        return None

    is_director   = _xml_flag(rel, "isDirector")
    is_officer    = _xml_flag(rel, "isOfficer")
    officer_title = (rel.findtext("officerTitle") or "").strip()
    other_text    = (rel.findtext("otherText")    or "").strip()
    remarks       = (root.findtext(".//remarks")  or "").strip()
    document_type = (root.findtext(".//documentType") or "").strip() or None
    # Form 3: the date the person BECAME an insider — the appointment date.
    # Form 4: the transaction date, which says nothing about tenure.
    period        = (root.findtext(".//periodOfReport") or "").strip() or None

    former_role = None
    if not (is_director or is_officer):
        # "Other" with a text like "Former Chief Executive Officer": the
        # insider's newest filing says the position has ENDED, and it is the
        # only structured statement of a departure EDGAR has. A pure investor
        # or 10% owner is still nothing to us.
        m = _FORMER_TEXT.search(other_text)
        if not m:
            return None
        former_role = _title_to_role(m.group("title").strip()) if m.group("title").strip() else "Officer"

    # 'LAST FIRST' is how EDGAR writes a human being. A company that files a
    # Form 4 (a director-company, a 10% owner) keeps its name as filed: turned
    # around, "EMBRAER S.A." became "S.a Embraer", lost its legal form, and the
    # runner minted a Person with Embraer's CIK and a seat (findings item 21).
    raw_name = name_elem.text.strip()
    name = raw_name if has_entity_suffix(raw_name) else sec_lookup._normalize_sec_name(raw_name)
    role = former_role or (_officer_role(officer_title, remarks) if is_officer else "Director")
    if is_officer and _SEE_REMARKS.match(officer_title) and _looks_like_title(remarks):
        officer_title = remarks                           # the title as the filer actually wrote it
    # The reporting owner's own CIK — a hard person key, one per filer across
    # every filing and spelling. Padded to 10 like every CIK we store.
    owner_cik_raw = (owner.findtext(".//rptOwnerCik") or "").strip()
    owner_cik = sec_client._cik_int(owner_cik_raw).zfill(10) if owner_cik_raw else None

    # What the filer holds after the reported transactions, per security —
    # the stake is computed per class once the issuer's counts are known.
    blocks = _form4_blocks(root)
    holdings = _form4_holdings(root)
    pick = stake_for(holdings, None)

    return {"name": name, "title": officer_title, "role": role,
            "shares_owned": pick["shares_owned"] if pick else None,
            "share_class": pick["share_class"] if pick else None,
            "holdings": holdings, "blocks": blocks, "issuer_cik": issuer_cik,
            "person_cik": owner_cik, "document_type": document_type,
            "period_of_report": period, "former": former_role is not None}


def _xml_flag(rel, tag: str) -> bool:
    """A Form 3/4 relationship flag. Older filings write `1`, newer ones
    `true` — Apple's 2026 Form 3 for its new CEO says `<isDirector>true`,
    and reading only `1` dropped him."""
    return (rel.findtext(tag) or "").strip().lower() in ("1", "true")


#: Item 5/6 "Other" text on a Form 3/4 that names a position as ended.
_FORMER_TEXT = re.compile(r"\b(?:former|retired|ex-)\s*(?P<title>[^;,.()]*)", re.IGNORECASE)


def fetch_executives(cik: str) -> list:
    """
    Extract executives and directors from Form 3/4 filings (insider ownership reports).
    These are machine-readable XML with explicit name and title fields —
    no HTML parsing or heuristics required.

    Strategy: read the most recent Form 3/4 for each unique insider (by filer CIK),
    over the filings of the last FORM4_WINDOW_DAYS (from the issuer's newest
    one), up to MAX_FORM4_FETCH insiders and MAX_FORM4_SCAN documents.

    EDGAR cross-indexes Form 3/4 filings under the issuer's CIK in Archives, even
    when the accession number starts with a filing agent's CIK. Always use the
    issuer CIK (the `cik` argument) to build the Archives URL.
    """
    log.info("SEC EDGAR: fetching Form 3/4 for CIK=%s", cik)
    try:
        submissions = sec_client._submissions(cik)
    except httpx.HTTPError as exc:
        log.error("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        sec_client._section_failed(sec_client.SECTION_INSIDERS, exc)
        return []

    recent       = submissions.get("filings", {}).get("recent", {})
    forms        = list(recent.get("form",           []))
    accessions   = list(recent.get("accessionNumber", []))
    primary_docs = list(recent.get("primaryDocument", []))
    filing_dates = list(recent.get("filingDate",      []))

    # Collect one filing per unique reporting person (newest first = most
    # current title), then keep reading the rarer Form 3s further back for
    # the date each listed person became an insider.
    executives: list[dict] = []
    by_person: dict[str, dict] = {}   # person CIK (or name) → their record
    issuer_cik_int = sec_client._cik_int(cik)
    scanned  = 0
    form3_scanned = 0
    window_start = _form34_window_start(forms, filing_dates)
    # The inline index holds the newest ~1,000 filings: a busy filer's ends
    # inside the window (JPMorgan's on 2025-11-10, the window from 2025-07-16),
    # and the window's oldest Form 3/4s are on the next page. Read on, so the
    # window is read whole; the pages are kept for the seat dating below.
    pages = _window_pages(submissions, filing_dates, window_start)
    for page in pages.values():
        for form, acc, doc, filed in zip(page.get("form") or [], page.get("accessionNumber") or [],
                                         page.get("primaryDocument") or [], page.get("filingDate") or []):
            if form in INSIDER_FORMS and filed and filed >= window_start:
                forms.append(form)
                accessions.append(acc)
                primary_docs.append(doc)
                filing_dates.append(filed)

    # Fetch Form 3/4 filings newest-first and dedupe by the *reporting person* —
    # NOT the accession/filer-CIK prefix, which collapses distinct insiders who
    # share a filing agent (that missed most of a company's insiders). New
    # insiders stop at the window's start, MAX_FORM4_FETCH or MAX_FORM4_SCAN; Form 3s (a few a year)
    # are still read past that, because a person's Form 3 is usually older
    # than their newest Form 4 and it is the one filing that dates the seat.
    for i, form in enumerate(forms):
        if form not in INSIDER_FORMS:
            continue
        is_form3 = form.startswith("3")
        filed = filing_dates[i] if i < len(filing_dates) else ""
        full = (len(executives) >= MAX_FORM4_FETCH or scanned >= MAX_FORM4_SCAN
                or bool(window_start and filed and filed < window_start))
        if full and (not is_form3 or form3_scanned >= MAX_FORM3_SCAN):
            continue
        # primaryDocument may be prefixed with an XSLT stylesheet dir
        # (e.g. "xslF345X06/form4.xml") which serves HTML — strip to the raw XML.
        doc = (primary_docs[i] if i < len(primary_docs) else "")
        doc = doc.split("/")[-1] if "/" in doc else doc
        acc = (accessions[i] if i < len(accessions) else "").replace("-", "")
        if not doc or not acc:
            continue

        # Archives path uses the issuer CIK (not the accession filer CIK).
        try:
            xml_text = sec_client._get_text(f"{sec_client.ARCHIVES_URL}/{issuer_cik_int}/{acc}/{doc}")
        except httpx.HTTPError as exc:
            log.debug("SEC EDGAR: Form 3/4 fetch failed: %s", exc)
            continue
        scanned += 1
        if is_form3:
            form3_scanned += 1

        result = _parse_form34_xml(xml_text)
        # A form about a different issuer is the company filing about a stake it
        # holds elsewhere — its reporting "owner" is usually the company itself,
        # and writing it here made Embraer a Director of Embraer.
        if result and result.get("issuer_cik") and \
                sec_client._cik_int(result["issuer_cik"]) != issuer_cik_int:
            log.info("SEC EDGAR: dropping Form 3/4 by %r — issuer CIK %s is not %s",
                     result["name"], result["issuer_cik"], cik)
            continue
        if not (result and result["name"]):
            continue
        key   = result.get("person_cik") or result["name"]
        known = by_person.get(key)
        if known is None:
            if full:
                continue        # an older Form 3 of someone we did not list
            result["source_url"]  = sec_client._filing_index_url(cik, accessions[i]) or None
            result["source_date"] = filing_dates[i] if i < len(filing_dates) else None
            # The newest filing IS the Form 3: the seat starts on its date. A
            # "Former …" filing instead closes the seat on its date.
            result["since"] = result["period_of_report"] if is_form3 and not result["former"] else None
            result["until"] = result["period_of_report"] if result["former"] else None
            executives.append(result)
            by_person[key] = result
            log.debug("SEC EDGAR: insider %s (%s)", result["name"], result["role"])
        elif is_form3 and not result["former"] and not known.get("since") \
                and canonical_role(result["role"]) == canonical_role(known["role"]):
            # Their Form 3, older than the Form 4 we listed them from, and for
            # the same seat: that is when it began. A Form 3 for a different
            # seat (VP then, CEO now) dates the wrong thing and is left alone.
            known["since"] = result["period_of_report"]
            known["since_url"] = sec_client._filing_index_url(cik, accessions[i]) or None

    # The inline index holds the newest ~1,000 filings — for Microsoft, back to
    # 2020 — so a long-serving officer's Form 3 is on an older page.
    # a former insider too: its seat is written closed on a fresh read and
    # dated like any other (findings item 74)
    if scanned >= MAX_FORM4_SCAN:
        log.warning("SEC EDGAR: CIK=%s files more than %d Form 3/4s in its insider window — "
                    "the oldest were not read", cik, MAX_FORM4_SCAN)
    if any(not e.get("since") for e in executives):
        _date_from_older_pages(cik, submissions, by_person, pages)
    _drop_issuer_event_dates(executives)
    log.info("SEC EDGAR: found %d executives from Form 3/4 for CIK=%s",
             len(executives), cik)
    return executives


def _window_pages(submissions: dict, filing_dates: list, window_start: str | None) -> dict:
    """The older submissions pages the insider window reaches into, by name:
    read while the filings so far end after the window's start, at most
    MAX_OLDER_PAGES (findings item 105). {} when the inline index covers it."""
    files = (submissions.get("filings", {}) or {}).get("files") or []
    pages: dict = {}
    oldest = filing_dates[-1] if filing_dates else ""
    for f in files[:MAX_OLDER_PAGES]:
        if not window_start or not oldest or oldest < window_start:
            break
        try:
            page = sec_client._get(f"{sec_client.SUBMISSIONS_URL}/{f['name']}")
        except Exception as exc:  # noqa: BLE001 - the window's tail is best-effort
            log.warning("SEC EDGAR: older submissions page %s failed: %s", f.get("name"), exc)
            break
        pages[f["name"]] = page
        dates = page.get("filingDate") or []
        oldest = dates[-1] if dates else ""
    return pages


def _form34_window_start(forms: list, filing_dates: list) -> str | None:
    """The first filing day inside the insider window: FORM4_WINDOW_DAYS before
    the issuer's newest Form 3/4 (ISO, comparable as text), or None when there
    is none or its date does not parse."""
    newest = next((d for f, d in zip(forms, filing_dates) if f in INSIDER_FORMS and d), None)
    try:
        return (datetime.fromisoformat(newest).date() - timedelta(days=FORM4_WINDOW_DAYS)).isoformat()
    except (TypeError, ValueError):
        return None


MAX_OLDER_PAGES = 3      # older submissions index pages read for Form 3s
MAX_OLDER_FORM3 = 60     # Form 3 documents opened across them


def _date_from_older_pages(cik: str, submissions: dict, by_person: dict,
                           loaded: dict | None = None) -> int:
    """Seat start dates for listed people whose Form 3 predates the inline index.

    EDGAR pages the rest of a company's filings as `filings.files` (Microsoft:
    two pages, 2008–2020 and 1994–2008, holding 58 Form 3s — among them Amy
    Hood's, CFO since 2013-05-08). Index pages are never cached, so this reads
    at most `MAX_OLDER_PAGES` of them and only while a listed person is still
    undated; the Form 3 documents are Archives files and cached forever. Same
    rules as the inline pass: same seat only, never a "Former …" filing as the
    start — a former insider's seat is dated too (findings item 74).
    Returns how many people were dated. Electronic Form 3s begin mid-2003;
    earlier seats are not in EDGAR at all. ``loaded``: pages the insider
    window already read (`_window_pages`), not asked for again.
    """
    files = (submissions.get("filings", {}) or {}).get("files") or []
    issuer_cik_int = sec_client._cik_int(cik)
    read = dated = 0
    for f in files[:MAX_OLDER_PAGES]:
        if not any(not e.get("since") for e in by_person.values()):
            break
        try:
            page = (loaded or {}).get(f["name"]) or sec_client._get(f"{sec_client.SUBMISSIONS_URL}/{f['name']}")
        except Exception as exc:  # noqa: BLE001 - dating is best-effort
            log.warning("SEC EDGAR: older submissions page %s failed: %s", f.get("name"), exc)
            break
        forms        = page.get("form",            [])
        accessions   = page.get("accessionNumber", [])
        primary_docs = page.get("primaryDocument", [])
        for i, form in enumerate(forms):
            if form not in ("3", "3/A"):
                continue
            if read >= MAX_OLDER_FORM3:
                break
            doc = (primary_docs[i] if i < len(primary_docs) else "").split("/")[-1]
            acc = (accessions[i] if i < len(accessions) else "").replace("-", "")
            if not doc or not acc:
                continue
            try:
                xml_text = sec_client._get_text(f"{sec_client.ARCHIVES_URL}/{issuer_cik_int}/{acc}/{doc}")
            except httpx.HTTPError as exc:
                log.debug("SEC EDGAR: Form 3 fetch failed: %s", exc)
                continue
            read += 1
            result = _parse_form34_xml(xml_text)
            if not result or result["former"] or not result.get("period_of_report"):
                continue
            if result.get("issuer_cik") and sec_client._cik_int(result["issuer_cik"]) != issuer_cik_int:
                continue
            known = by_person.get(result.get("person_cik") or result["name"])
            if known is None or known.get("since"):
                continue
            if canonical_role(result["role"]) != canonical_role(known["role"]):
                continue
            known["since"] = result["period_of_report"]
            known["since_url"] = sec_client._filing_index_url(cik, accessions[i]) or None
            dated += 1
    if read:
        log.info("SEC EDGAR: %d Form 3(s) read from older pages for CIK=%s, %d seat(s) dated",
                 read, cik, dated)
    return dated


#: Form 3s sharing one date from this many people are the ISSUER's event.
FORM3_CLUSTER = 3


def _drop_issuer_event_dates(executives: list[dict]) -> None:
    """A Form 3 dates the day a person became subject to Section 16 — which
    is their appointment only when the company already was. When the company
    itself becomes subject (a listing, a lost foreign-private-issuer status),
    every sitting officer and director files a Form 3 on the same day:
    Embraer's whole board carried "since 2026-03-18". A date shared by
    `FORM3_CLUSTER` or more people is that event, not a seat, and is dropped.
    """
    by_date: dict[str, int] = {}
    for e in executives:
        if e.get("since"):
            by_date[e["since"]] = by_date.get(e["since"], 0) + 1
    clusters = {d for d, n in by_date.items() if n >= FORM3_CLUSTER}
    for e in executives:
        if e.get("since") in clusters:
            log.info("SEC EDGAR: %s Form 3s dated %s — the issuer's event, not %r's seat",
                     by_date[e["since"]], e["since"], e["name"])
            e["since"] = None


# ── Departures from 8-K Item 5.02 ────────────────────────────────────────────
#
# EDGAR has no structured statement that a director or officer LEFT: a
# departing insider files no form (a "Former …" Form 4 above is the rare
# exception), so the roles list only ever grew. Item 5.02 of Form 8-K is where
# a company must announce departures and appointments within four business
# days. The submissions index says WHICH 8-Ks carry Item 5.02 (`items`), so
# no document is opened blind; the item text itself is prose, read only for
# names we already list — never to mint anyone.

_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}
_DATE_RX = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})\b")
_EFFECTIVE_RX = re.compile(r"\beffective\s+(?:as\s+of\s+|on\s+)?" + _DATE_RX.pattern, re.IGNORECASE)
#: Anything that says a seat ends, however it is phrased.
_DEPARTURE_RX = re.compile(
    r"\b(?:resign|retir|step(?:ped|s)?\s+down|depart|ceas|terminat|passed\s+away|"
    r"died|death|will\s+leave|leaving|no\s+longer\s+serv|transition\s+from|"
    r"not\s+(?:to\s+)?stand\s+for\s+re-?election)", re.IGNORECASE)
#: A succession names TWO people: the successor before the verb, the person
#: leaving after it — "Mr. Borders succeeds Chris Kondo", "a transition of
#: duties from Kate Adams". Only the name after the verb is a departure, and
#: the sentence's date (the successor's start) is when the seat ends.
_SUCCESSION_RX = re.compile(
    r"\b(?:succeed(?:s|ed)|replac(?:es|ed)|transition\s+of\s+(?:duties|responsibilities)\s+from"
    r"|assum(?:es|ed)\s+the\s+(?:role|position|duties)\s+(?:of\s+[\w ,]+?\s+)?from)\b",
    re.IGNORECASE)
#: A departure already in the past — datable from the sentence's own date.
_HARD_PAST_RX = re.compile(
    r"\b(?:resigned|retired|passed\s+away|died|was\s+terminated|ceased|"
    r"stepped\s+down|departed)\b", re.IGNORECASE)
#: "… from his role as Chief Executive Officer to …" — which seat is ending.
_ROLE_AS_RX = re.compile(
    r"\b(?:role|position|office|capacity|service|duties)\s+(?:as|of)\s+(?:a\s+|the\s+)?"
    r"(?:Company['’]s\s+)?(?P<title>[A-Z][A-Za-z&,\- ]{2,60}?)"
    r"(?=\s+(?:to|and|of\s+the|effective|at|,)|[.;])")
_ITEM_502_RX = re.compile(r"Item\s*5\.02\.?(?P<text>.*?)(?=Item\s*\d\.\d\d|SIGNATURES?\b)",
                          re.IGNORECASE | re.DOTALL)
#: The item's statutory heading, which every 8-K repeats after "Item 5.02" in
#: some subset — "Departure of Directors or Certain Officers; Election of
#: Directors; Appointment of Certain Officers; Compensatory Arrangements of
#: Certain Officers." It is not a sentence about anyone, but its first word
#: is "Departure": glued to the first sentence it marked that sentence a
#: departure, and everyone named in it was closed at the sentence's
#: effective date — Berkshire's May-2025 8-K closed Greg Abel's CEO seat on
#: the day it began, 2026-01-01 (found 2026-10-25).
_ITEM_502_HEADING_RX = re.compile(
    r"^\s*(?:(?:Departure\s+of\s+Directors(?:\s+or\s+Certain\s+Officers)?|Election\s+of\s+Directors|"
    r"Appointment\s+of\s+Certain\s+Officers|Compensatory\s+Arrangements\s+of\s+Certain\s+Officers)"
    r"\s*[;,.]?\s*)+", re.IGNORECASE)
#: Sentence ends — but not the dot of an honorific ("Mr. Cook"). A lettered
#: enumerator "(b) On May 4, 2025 …" / "b) …" starts a sentence too: the item's
#: sub-paragraphs are lettered, and a lowercase "b)" after a period used to
#: glue two paragraphs into one sentence.
_SENTENCE_RX = re.compile(r"(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bDr\.)(?<=[.!?])\s+(?=[A-Z(“\"]|\(?[a-z]\)\s)")


def _iso_from_words(m) -> str:
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def _item_502_text(html_text: str) -> str | None:
    """The item's prose, without its statutory heading."""
    m = _ITEM_502_RX.search(sec_13dg_text._plain_text(html_text))
    return _ITEM_502_HEADING_RX.sub("", m.group("text")).strip() if m else None


def _name_rx(full_name: str) -> re.Pattern | None:
    """The person as prose names them: "Tim Cook", "Timothy D. Cook", "Mr. Cook".

    The surname must be there; the first name may be a short form (Tim for
    Timothy) or replaced by an honorific. A bare surname never matches — one
    8-K names several people.
    """
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
    if len(parts) < 2 or len(parts[-1]) < 3:
        return None
    last, first = re.escape(parts[-1]), parts[0].rstrip(".")
    stem = re.escape(first[:3]) if len(first) >= 3 else re.escape(first)
    return re.compile(
        rf"\b(?:(?:Mr|Ms|Mrs|Dr|Messrs)\.?\s+{last}\b|{stem}[\w.]*\s+(?:[A-Z][\w.]*\s+)?{last}\b)")


def _departures_in_text(text: str, names: list[str], filed: str | None) -> list[dict]:
    """Which of `names` the Item 5.02 text says are leaving, and from when.

    A sentence must name the person AND say a seat ends. The end date is the
    sentence's "effective <date>" when it states one; failing that, a
    departure already in the past ("resigned", "passed away") is dated by the
    sentence's own date, or the filing date. A forward-looking notice with no
    effective date — "decided not to stand for re-election at the annual
    meeting" — is NOT closed: the person still serves, and the meeting date is
    not in the text.
    """
    out: dict[tuple, dict] = {}
    patterns = [(n, _name_rx(n)) for n in names]
    for sentence in _SENTENCE_RX.split(text):
        succession = _SUCCESSION_RX.search(sentence)
        if not succession and not _DEPARTURE_RX.search(sentence):
            continue
        # In a succession only the person named AFTER the verb is leaving.
        zone = sentence[succession.end():] if succession else sentence
        for name, rx in patterns:
            if rx is None or not rx.search(zone):
                continue
            eff = _EFFECTIVE_RX.search(sentence)
            if eff:
                until = _iso_from_words(eff)
            elif succession or _HARD_PAST_RX.search(sentence):
                d = _DATE_RX.search(sentence)
                until = _iso_from_words(d) if d else filed
            else:
                continue
            if not until:
                continue
            rm = _ROLE_AS_RX.search(sentence)
            role = _title_to_role(rm.group("title").strip()) if rm else None
            out.setdefault((name, role), {"name": name, "until": until, "role": role,
                                          "sentence": sentence.strip()[:300]})
    return list(out.values())


def fetch_departures(cik: str, names: list[str], limit: int = MAX_8K_FETCH) -> list[dict]:
    """Departures of the named people from the company's newest 8-Ks with
    Item 5.02. One submissions read (the index says which 8-Ks qualify), then
    at most `limit` primary documents — Archives files, so cached forever."""
    if not names:
        return []
    try:
        submissions = sec_client._submissions(cik)
    except Exception as exc:  # noqa: BLE001 - a lookup that fails must not fail the scrape
        log.warning("SEC EDGAR: departures lookup failed for CIK=%s: %s", cik, exc)
        return []
    recent       = submissions.get("filings", {}).get("recent", {})
    forms        = recent.get("form",            [])
    items        = recent.get("items",           [])
    accessions   = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    filing_dates = recent.get("filingDate",      [])
    cik_int = sec_client._cik_int(cik)

    found: dict[tuple, dict] = {}
    read = 0
    for i, form in enumerate(forms):
        if form not in ("8-K", "8-K/A") or "5.02" not in (items[i] if i < len(items) else ""):
            continue
        if read >= limit:
            break
        doc = (primary_docs[i] if i < len(primary_docs) else "").split("/")[-1]
        acc = (accessions[i] if i < len(accessions) else "").replace("-", "")
        if not doc or not acc:
            continue
        try:
            html_text = sec_client._get_text(f"{sec_client.ARCHIVES_URL}/{cik_int}/{acc}/{doc}")
        except httpx.HTTPError as exc:
            log.debug("SEC EDGAR: 8-K fetch failed: %s", exc)
            continue
        read += 1
        text = _item_502_text(html_text)
        if not text:
            continue
        filed = filing_dates[i] if i < len(filing_dates) else None
        for d in _departures_in_text(text, names, filed):
            d["source_url"]  = sec_client._filing_index_url(cik, accessions[i]) or None
            d["source_date"] = filed
            found.setdefault((d["name"], d["role"]), d)     # newest 8-K wins
    log.info("SEC EDGAR: %d departure(s) in %d 8-K(s) with Item 5.02 for CIK=%s",
             len(found), read, cik)
    return list(found.values())


def fetch_insider_holding(name: str, issuer_cik: str,
                          shares_outstanding: "sec_shares.SharesOutstanding | dict | float | None" = None,
                          person_cik: str | None = None) -> dict | None:
    """
    Person-centric insider lookup (all structured XML): resolve the individual's
    CIK, read THEIR most recent Form 4 that reports on `issuer_cik`, and return
    their current share holding (+ a stake %% when shares_outstanding is known)
    and the CIK it was filed under.

    Reaches insiders the issuer-side Form-4 scan misses (e.g. a CEO whose filings
    are flooded out of the company's recent window, or a director who has not
    traded within it). ``person_cik`` is the CIK the person already holds; without
    one the name is searched and each candidate filer tried in turn. Verifying
    issuerCik on the filing guards against name collisions. Returns None if not
    found / no shares.
    """
    from app.scraper.mapper import normalize_cik
    issuer_int = str(sec_client._cik_int(issuer_cik))
    for cik in ([normalize_cik(person_cik)] if person_cik else sec_lookup._lookup_person_ciks(name)):
        found = _own_form4_holding(cik, issuer_int, shares_outstanding)
        if found is not _NOT_THEIRS:
            return found
    return None


_NOT_THEIRS = object()     # the candidate filed no Form 4 about the issuer


def _own_form4_holding(cik: str, issuer_int: str,
                       shares_outstanding: "sec_shares.SharesOutstanding | dict | float | None"):
    """The holding a filer's newest Form 4 about the issuer states (None when
    it states no shares), or _NOT_THEIRS when none of their newest
    MAX_PERSON_FORM4 Form 4s is about the issuer."""
    try:
        subs = sec_client._submissions(cik)
    except httpx.HTTPError:
        return _NOT_THEIRS
    rec   = subs.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    accs  = rec.get("accessionNumber", [])
    docs  = rec.get("primaryDocument", [])
    dates = rec.get("filingDate", [])

    opened = 0
    for i, f in enumerate(forms):
        if f not in ("4", "4/A"):
            continue
        if opened >= MAX_PERSON_FORM4:
            break
        acc = accs[i].replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        doc = doc.split("/")[-1] if "/" in doc else doc
        if not doc:
            continue
        opened += 1
        try:
            xml = sec_client._get_text(f"{sec_client.ARCHIVES_URL}/{int(cik)}/{acc}/{doc}")
        except httpx.HTTPError:
            continue
        m = re.search(r"<issuerCik>0*(\d+)</issuerCik>", xml)
        if not m or m.group(1) != issuer_int:
            continue                       # this Form 4 is about a different company
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None
        holdings = _form4_holdings(root)
        pick = stake_for(holdings, shares_outstanding)
        if not pick:
            return None
        return {
            **pick,
            # the positions, so a vehicle already written for the issuer can be
            # taken out of this person's (`without_vehicles`, findings item 111)
            "holdings":      holdings,
            "blocks":        _form4_blocks(root),
            "cik":           cik,
            "source_url":    sec_client._filing_index_url(cik, accs[i]),
            "source_date":   dates[i] if i < len(dates) else None,
        }
    return _NOT_THEIRS


def _class_of(title: str | None) -> str:
    """The class letter a security title names ("Class B Common Stock" → "B"), else ""."""
    m = re.search(r"\bclass\s+([A-Z])\b", title or "", re.I)
    return m.group(1).upper() if m else ""


#: A derivative-table row that is an option, a unit, a right, a note… is not a
#: share held. One titled plainly as a class of common stock is: Berkshire's
#: Class A is convertible into Class B, so its filers list their Class A in
#: Table II — Warren Buffett's 188,290 Class A shares sit there.
_NOT_A_SHARE = re.compile(r"option|unit|right|warrant|note|debenture|preferred|phantom|award|\bsar\b|swap|forward", re.I)


def _form4_holdings(root: ET.Element) -> dict[str, float]:
    """What the filer holds after the reported transactions, per security
    title: the positions of `_form4_blocks` summed."""
    out: dict[str, float] = {}
    for b in _form4_blocks(root):
        out[b["title"]] = out.get(b["title"], 0.0) + b["shares"]
    return out


def _form4_blocks(root: ET.Element) -> list[dict]:
    """What the filer holds after the reported transactions, one position per
    security title and nature of ownership: {title, indirect, nature, shares}.

    The rows of a Form 4 are in order; each states the position AFTER it. So
    the last row per security is the position the filing leaves the filer
    in — not the largest value across rows, which was the position midway
    through: Warren Buffett's July 2026 Form 4 converts 8,000 Class A into
    12,000,000 Class B ("owned following": 12,001,162) and gives the B away in
    four gifts (…1,162); the largest was the moment before the gifts. Direct
    and indirect positions (a trust beside the person) in the same security
    are positions of their own (`_form4_holdings` sums them; a vehicle several
    insiders report is taken out first, `split_shared_blocks`); derivative
    rows count only when they are plainly a class of common stock
    (`_NOT_A_SHARE`).
    """
    last: dict[tuple[str, str], float] = {}
    for row in root.iter():
        if row.tag not in ("nonDerivativeTransaction", "nonDerivativeHolding",
                           "derivativeTransaction", "derivativeHolding"):
            continue
        title = (row.findtext("securityTitle/value") or "").strip()
        if row.tag.startswith("derivative") and not (
                re.search(r"common|ordinary", title, re.I) and not _NOT_A_SHARE.search(title)):
            continue
        val = row.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        try:
            n = float((val or "").replace(",", "").strip())
        except ValueError:
            continue
        nature = ((row.findtext("ownershipNature/directOrIndirectOwnership/value") or "D").strip().upper()
                  + "|" + (row.findtext("ownershipNature/natureOfOwnership/value") or "").strip())
        last[(title, nature)] = n
    return [{"title": title, "indirect": nature.startswith("I|"), "nature": nature.split("|", 1)[1],
             "shares": n} for (title, nature), n in last.items()]


def _vehicle_key(block: dict) -> tuple[str, str, float] | None:
    """What makes two insiders' indirect positions one block (findings item
    111): the security, the words naming the holder ("By Control Trust") and
    the count, to the share. None for a direct position or an unnamed one."""
    words = re.sub(r"\s+", " ", block.get("nature") or "").strip().casefold()
    if not block.get("indirect") or not words or not block.get("shares"):
        return None
    return block["title"], words, float(block["shares"])


def without_vehicles(holding: dict, vehicles: list[dict],
                     shares_outstanding: "sec_shares.SharesOutstanding | dict | float | None") -> dict | None:
    """A person's holding (`fetch_insider_holding`) without the positions that
    are vehicles already written for the issuer — {name, share_class, shares}
    of their edges: an indirect position whose holder is named so, of that
    class and that count (findings item 111). The person-centric lookup reads
    one filing at a time and cannot see that another insider reports the same
    block; the issuer's scan did. None when nothing of their own is left."""
    known = {(v.get("share_class") or "", (v.get("name") or "").casefold(), float(v.get("shares") or 0))
             for v in vehicles}
    held = dict(holding.get("holdings") or {})
    for b in holding.get("blocks") or []:
        if b.get("indirect") and (b["title"], vehicle_name(b["nature"]).casefold(), float(b["shares"])) in known:
            held[b["title"]] = held.get(b["title"], 0.0) - b["shares"]
    held = {t: n for t, n in held.items() if n > 0}
    if held == (holding.get("holdings") or {}):
        return holding
    pick = stake_for(held, shares_outstanding)
    return {**holding, **pick, "holdings": held} if pick else None


def vehicle_name(nature: str) -> str:
    """The holder a nature of ownership names, as the filer wrote it:
    "By Control Trust" → "Control Trust"."""
    return re.sub(r"^\s*(?:held\s+)?by\s+", "", re.sub(r"\s+", " ", nature or ""), flags=re.I).strip(" .,;:") or nature


#: below this share of its class an identical count is as likely two people's
#: own accounts as one vehicle: TSMC's officers each hold 7,036 shares "LTI
#: Trust" (equal grants), Wells Fargo's 893.67 "Through 401(k) Plan" (three
#: people), two unrelated insiders 2,000 "Spouse" — all far below it — and a
#: block this small changes no picture (findings item 111's guard, 83
#: issuers; the Slim family's GFI block is 0.33 % of Series B)
VEHICLE_MIN_PCT = 0.1
#: words that point at a footnote instead of naming the holder ("See footnote",
#: "(1)"): the holder is in the footnote, which is not read
_NO_HOLDER = re.compile(r"^(?:see|refer|as\s+described)\b|\bfoot\s*note|^[\W\d]*$", re.I)


def _names_a_holder(nature: str) -> bool:
    """Whether the words name a holder: no pointer to a footnote, and written
    as a name — a capitalised word ("Control Trust", "GFI"), not NYT's bare
    "trust" of the Sulzberger family, which says only what kind of holder."""
    name = vehicle_name(nature)
    return not _NO_HOLDER.search(name) and any(w[:1].isupper() for w in name.split())


def split_shared_blocks(executives: list[dict],
                        outstanding: "sec_shares.SharesOutstanding | dict | float | None" = None) -> list[dict]:
    """The indirect positions two or more insiders of one issuer report alike,
    taken off each insider's holdings and returned once each (findings item
    111, the user's choice of 2026-11-12: the vehicle is the owner).

    A Form 3/4 states what the reporting person may be deemed to own
    beneficially, and a family's shared vehicle is in every member's filing:
    America Movil's Form 3s of 2026-03 by Carlos and Patrick Slim Domit each
    list "By Control Trust" 17,742,515,522, "By Control Empresarial Trust by
    Control Empresarial" 10,933,355,873 and "By GFI" 196,009,735 Series B
    shares — summed into every member's edge, the family held 169 % of the
    class. Alike means the same security, the same words and the same count;
    the footnotes that name the trust in law are not read, so a vehicle hidden
    in another position ("By Spouse", which a footnote breaks down) stays as
    filed. Only a block of at least VEHICLE_MIN_PCT of its class (measured
    against ``outstanding``; none without it) whose words name a holder: equal
    small counts are often each person's own account, and "See footnote" names
    nobody. Each returned block: {name, nature, title, shares, reporters,
    source_url, source_date} — the newest reporter's filing."""
    seen: dict[tuple, list[dict]] = {}
    nature_of: dict[tuple, str] = {}             # the words as the first reporter wrote them
    for ex in executives:
        for b in ex.get("blocks") or []:
            if k := _vehicle_key(b):
                nature_of.setdefault(k, b["nature"])
                if ex not in seen.setdefault(k, []):
                    seen[k].append(ex)
    def large(title: str, shares: float) -> bool:
        pick = stake_for({title: shares}, outstanding) if outstanding is not None else None
        return bool(pick and pick.get("stake_percent") is not None and pick["stake_percent"] >= VEHICLE_MIN_PCT)
    shared = {k: who for k, who in seen.items()
              if len({ex.get("person_cik") or ex.get("name") for ex in who}) >= 2
              and _names_a_holder(nature_of[k]) and large(k[0], k[2])}
    out: list[dict] = []
    for (title, _words, shares), who in shared.items():
        nature = nature_of[(title, _words, shares)]
        newest = max(who, key=lambda ex: ex.get("source_date") or "")
        out.append({"name": vehicle_name(nature), "nature": nature, "title": title or None, "shares": shares,
                    "reporters": [ex.get("name") for ex in who],
                    "source_url": newest.get("source_url"), "source_date": newest.get("source_date")})
        for ex in who:
            held = dict(ex.get("holdings") or {})
            held[title] = max(0.0, held.get(title, 0.0) - shares)
            ex["holdings"] = {t: n for t, n in held.items() if n > 0}
            pick = stake_for(ex["holdings"], None)
            ex["shares_owned"] = pick["shares_owned"] if pick else None
            ex["share_class"] = pick["share_class"] if pick else None
    if out:
        log.info("SEC EDGAR: %d position(s) reported alike by several insiders written once: %s",
                 len(out), "; ".join(f"{b['name']} {b['shares']:,.0f}" for b in out))
    return out


def stake_for(holdings: dict[str, float],
              outstanding: "sec_shares.SharesOutstanding | dict | float | None") -> dict | None:
    """The one holding an edge carries, with its stake: the class with the
    largest percentage, else the largest count.

    A count of one class is divided by the count of THAT class. Where the
    issuer states its classes and the title names none, or names one the
    issuer does not state, there is no percentage — a Class B count over a
    Class A total is how 1,274 % happened, and a Berkshire B share is a
    fifteen-hundredth of an A share. An unclassed issuer total divides any
    title, as before. The denominator and its date travel with the pick.
    """
    detail = sec_shares._as_detail(outstanding)
    best: tuple[tuple[float, float], dict] | None = None
    for title, shares in holdings.items():
        if not shares or shares <= 0:
            continue
        cls = _class_of(title)
        denom: float | None = None
        if detail is not None:
            denom = (detail.by_class.get(cls) if cls else None) if detail.by_class else detail.total
        pct = sec_13dg_text._pct_of(shares, denom)
        rank = (pct if pct is not None else -1.0, shares)
        if best is None or rank > best[0]:
            best = (rank, {"shares_owned": shares, "share_class": title or None,
                           "stake_percent": pct, "shares_outstanding": denom,
                           "denominator_date": detail.as_of if (detail and denom is not None) else None})
    return best[1] if best else None
