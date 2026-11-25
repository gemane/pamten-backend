"""
SEC EDGAR — from a name to its filer.

A company: the tickers file, then EDGAR's company-name index (10-K filers,
20-F filers, then an exact name among every registrant — read from the bulk
name list where it is configured), then full-text search. A person: the CIKs
filed under their name.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
import logging
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import httpx
from app.scraper.mapper import (fold_apostrophes, name_key, strip_edgar_tag,
                                strip_partnership_form, strip_trailing_forms, trailing_form)
from app.scraper import sec_client

log = logging.getLogger(__name__)
MAX_PERSON_CANDIDATES = 5   # filers a person's name search returns, tried in turn
# … and from the bulk name list: it lists every filer of the name, not only
# those with a Form 4, so the right one stands further down (John W. Stanton:
# sixth of seven). Measured on 300 people: 5 finds 91 of the stored CIKs, 10
# finds 95, more finds no more; a candidate with no Form 4 costs one request.
LIST_PERSON_CANDIDATES = 10

# Module-level cache for the tickers file (populated on first use per process)
_tickers_cache: dict | None = None


# ── Name helpers ──────────────────────────────────────────────────────────────

def _normalize_sec_name(raw: str) -> str:
    """
    SEC Form 3/4 stores individual names as 'LAST FIRST [MIDDLE]'.
    Converts to 'First [Middle] Last' with Title Case.
    e.g. 'COOK TIMOTHY D' → 'Timothy D Cook'
         'NADELLA SATYA'  → 'Satya Nadella'
    """
    words = [w.strip(".,") for w in raw.strip().split() if w.strip(".,")]
    if len(words) >= 2:
        last  = words[0].capitalize()
        first = " ".join(w.capitalize() for w in words[1:])
        return f"{first} {last}"
    return raw.title()


# ── Ticker-file company lookup ────────────────────────────────────────────────

def _get_tickers() -> dict:
    """Fetch (and cache) EDGAR's company_tickers.json for the process lifetime."""
    global _tickers_cache
    if _tickers_cache is None:
        log.info("SEC EDGAR: loading company_tickers.json")
        _tickers_cache = sec_client._get(sec_client.TICKERS_URL)    # through _response: the retry (item 38)
    return _tickers_cache


#: Legal-form and filler words that carry no company identity. The single
#: source for BOTH name-comparison helpers in this module: `_LEGAL_SUFFIXES`
#: (ticker matching) and `_significant_tokens` (issuer verification) — two
#: shapes of the same knowledge, which drifted apart when they were two lists.
_LEGAL_FORM_WORDS = (
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "ltd", "limited", "llc", "lp", "llp", "plc", "sa", "nv", "se", "ag", "ab",
    "as", "asa", "spa", "gmbh", "bv", "kk", "sarl", "srl",
    "group", "holding", "holdings",
)

#: at the end of a name only (`mapper.strip_trailing_forms`): anywhere, "NV
#: Energy" was "energy" and the tickers file's ENERGY FUELS INC its prefix match
_LEGAL_SUFFIXES = trailing_form(_LEGAL_FORM_WORDS)


def _ticker_normalize(name: str) -> str:
    """Lowercase, strip EDGAR's tag, apostrophes, punctuation and the legal
    forms at the end for name comparison ("Domino's Pizza" is
    company_tickers.json's "DOMINOS PIZZA INC", "Lennar Corporation" its
    "LENNAR CORP /NEW/")."""
    name = fold_apostrophes(strip_edgar_tag(name)).lower()
    name = re.sub(r"\s+", " ", name.replace(".", "").replace(",", " ")).strip()
    return strip_trailing_forms(strip_partnership_form(name), _LEGAL_SUFFIXES)


#: EDGAR's tag on an ADR programme's registration: "MTN GROUP LTD/ADR" is the
#: depositary's F-6 filer for MTN's American receipts, no 10-K, 20-F or
#: holders of its own. Matched only by its tag, it would give MTN Group a CIK
#: that carries nothing (2026-10-30, the user's choice: ADR-only listings out).
_ADR_TAG = re.compile(r"[/\\]\s*adrs?\s*[/\\]?\s*$", re.IGNORECASE)


def is_adr_listing(title: str | None) -> bool:
    """The tickers file's title names an ADR programme, not the company."""
    return bool(_ADR_TAG.search(title or ""))


# Minimum SequenceMatcher ratio for a match to be accepted.
# Rejects EFTS false positives (e.g. a Chesapeake Corp 10-K that merely
# *mentions* Nestlé in its text) and overly loose prefix matches.
_MIN_NAME_SIMILARITY = 0.55
#: the tickers lookup's unordered tier needs this many words — the writers' rule
#: for a name in another word order (`graph_writer._UNORDERED_MIN_WORDS`, item 58)
_UNORDERED_MIN_WORDS = 3

#: Words that make a name a fund vehicle — an investment company registered
#: under its sponsor's name, not the sponsor.
_FUND_WORDS = frozenset({"fund", "funds", "trust", "etf", "etfs", "portfolio", "portfolios"})


def _fund_words(norm: str) -> set[str]:
    return set(re.findall(r"[a-z]+", norm)) & _FUND_WORDS


def _name_similarity(a: str, b: str) -> float:
    """Normalized similarity [0, 1] between two company names after stripping suffixes.

    0 when ``b`` names a fund vehicle ``a`` does not: the full-text search's
    best hit for "Baillie Gifford & Co" — the 13F manager, CIK 0001088875,
    which files neither a 10-K nor a proxy — is the DEF 14A of BAILLIE GIFFORD
    FUNDS (CIK 0001120543), and at 0.84 it passed. Before the 2026-10-29
    rebuild the manager's node carried the funds' CIK."""
    na = _ticker_normalize(a)
    nb = _ticker_normalize(b)
    if not na or not nb:
        return 0.0
    if _fund_words(nb) - _fund_words(na):
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


#: A title carrying two names of one company: "PETROBRAS - PETROLEO BRASILEIRO
#: SA", "BrasilAgro - Brazilian Agricultural Real Estate Co" — the trade name,
#: a spaced dash, the legal name. ("Veraxa Biotech AG - Post de-SPAC" has the
#: shape too; its second part is no name, and no query is a prefix of it.)
_TITLE_NAMES = re.compile(r"\s+[-–—]\s+")


def _title_names(title: str) -> list[str]:
    """The normalised names of a two-name title; none for any other title."""
    names = _TITLE_NAMES.split(strip_edgar_tag(title))
    return [_ticker_normalize(n) for n in names] if len(names) > 1 else []


def _title_similarity(query: str, title: str) -> float:
    """`_name_similarity` of ``query`` against a tickers title, or against the
    closest of the names a two-name title carries (`_TITLE_NAMES`).

    The target "Petrobras" is the prefix of "PETROBRAS - PETROLEO BRASILEIRO
    SA" and scored 0.45 against the whole of it: the lookup refused the
    company's own entry, and the name index then gave Petrobras Argentina S.A.
    (0.64, a former subsidiary) — two rebuilds scraped the wrong company, the
    second with 48 of its subsidiaries (findings item 136). The fund hold stays
    on the whole title: a part must not hide the word that makes it a fund."""
    whole = _name_similarity(query, title)
    names = _TITLE_NAMES.split(strip_edgar_tag(title))
    if len(names) < 2 or _fund_words(_ticker_normalize(title)) - _fund_words(_ticker_normalize(query)):
        return whole
    return max([whole] + [_name_similarity(query, n) for n in names])


def _lookup_in_tickers(query: str) -> dict | None:
    """
    Look up a company in EDGAR's listed-company tickers file.
    Prefers exact normalized-name matches; falls back to prefix matches,
    preferring the shortest (most specific) result.
    Returns {cik: zero-padded-10-digit, name: str} or None.
    """
    try:
        tickers = _get_tickers()
    except httpx.HTTPError as exc:
        log.warning("SEC EDGAR: tickers file fetch failed: %s", exc)
        return None

    q = _ticker_normalize(query)
    exact: list[tuple[str, str, int]] = []   # (title, cik, len)
    prefix: list[tuple[str, str, int]] = []
    # EDGAR conforms a filer named after a person surname first: the file's
    # "PRICE T ROWE GROUP INC" is "T. Rowe Price Group, Inc." — the same words in
    # another order, which neither the exact nor the prefix match sees
    # (findings item 59, 2026-11-05/06). Held to three words or more, as the
    # writers' unordered rule is (item 58): two words swapped are too often
    # another company.
    unordered: list[tuple[str, str, int]] = []
    q_words = tuple(sorted(q.split()))
    q_unordered = q_words if len(q_words) >= _UNORDERED_MIN_WORDS else None

    for entry in tickers.values():
        title = entry.get("title", "")
        if is_adr_listing(title):
            continue
        cik   = str(entry.get("cik_str", "")).zfill(10)
        norm  = _ticker_normalize(title)
        if norm == q or q in _title_names(title):
            exact.append((title, cik, len(title)))
        elif norm.startswith(q):
            prefix.append((title, cik, len(title)))
        elif q_unordered and tuple(sorted(norm.split())) == q_unordered:
            unordered.append((title, cik, len(title)))

    for pool in (exact, prefix):
        if pool:
            pool.sort(key=lambda x: x[2])   # shortest name = most specific
            title, cik, _ = pool[0]
            sim = _title_similarity(query, title)
            if sim < _MIN_NAME_SIMILARITY:
                log.warning(
                    "SEC EDGAR: tickers match rejected (similarity %.2f < %.2f): %r vs %r",
                    sim, _MIN_NAME_SIMILARITY, query, title,
                )
                continue
            log.info("SEC EDGAR: tickers matched %r → %r (CIK=%s, sim=%.2f)", query, title, cik, sim)
            return {"cik": cik, "name": title}
    if unordered:
        # the words are the same, so no similarity hold: it would reject the
        # very order difference this tier is for (0.29 for T. Rowe Price)
        unordered.sort(key=lambda x: x[2])
        title, cik, _ = unordered[0]
        log.info("SEC EDGAR: tickers matched %r → %r (CIK=%s, the same words in another order)", query, title, cik)
        return {"cik": cik, "name": title}

    return None


# ── EDGAR company-name index lookup (second vector) ───────────────────────────

#: The passes of the name index, in order: 10-K filers, 20-F filers, then every
#: registrant ("" = no form filter). The first two take the closest name
#: (`_closest_registrant`); the last, which lists subsidiaries, ADR programmes
#: and namesakes alike, takes an exact name only (`_exact_registrant`).
_BROWSE_PASSES = ("10-K", "20-F", "")
#: registrants browse-edgar lists per pass: 5 was enough for a filtered pass,
#: and the type-free pass must reach the exact name past the prefix namesakes
#: (10 registrants start with "Nestle"); each list entry costs a submissions request
_BROWSE_COUNT = {"": 20}
_BROWSE_COUNT_DEFAULT = 5

#: What the scraper reads of a filer: holder statements (both namings, the
#: Dec-2024 rename), insider forms, 13F reports, the annual and quarterly
#: reports (Exhibit 21, acquisitions, cover). EDGAR holds some names twice —
#: CITADEL SECURITIES LLC is CIK 0001307720 (SC 13D/G and Form 4) and
#: 0001146184 (broker-dealer FOCUS reports) — and the one that files what we
#: read is the one a company name means here (findings item 63).
_READ_FORM_PREFIXES = ("SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G", "13F-HR", "10-K", "20-F", "10-Q")
_READ_FORMS = frozenset({"3", "4", "5", "3/A", "4/A", "5/A"})


def _browse_registrants(root: ET.Element) -> list[tuple[str, str | None]]:
    """(CIK, conformed name or None) for every registrant a browse-edgar
    company search names.

    EDGAR answers two ways. Several matches come as a list of registrants:
    Atom entries whose <id> carries the CIK and whose name a server-side bug
    leaves empty (the submissions document supplies it). A single match comes
    as that registrant's own filing feed: a <company-info> block at the root,
    with the CIK and the conformed name, over entries that are filings, not
    companies. The lookup read the list only, so a name with exactly one
    registrant was "not found" — Sears Holdings Corp, a 10-K filer, and every
    holder that files under a name of its own (findings item 63). The person
    lookup (`_lookup_person_ciks`) always read both shapes."""
    out: list[tuple[str, str | None]] = []
    ns_a = {"a": "http://www.w3.org/2005/Atom"}
    info = root.find("{*}company-info")           # a direct child: the lone shape only
    if info is not None and (cik := (info.findtext("{*}cik") or "").strip()):
        out.append((cik.zfill(10), (info.findtext("{*}conformed-name") or "").strip() or None))
        return out
    for entry in root.findall("a:entry", ns_a):
        id_text = (entry.findtext("a:id", "", ns_a) or "").strip()
        m = re.search(r"cik=(\d+)", id_text, re.IGNORECASE)
        cik = m.group(1) if m else (entry.findtext(".//{*}company-info/{*}cik") or "").strip()
        if cik and cik.zfill(10) not in {c for c, _ in out}:
            out.append((cik.zfill(10), None))
    return out


def _registrant(cik: str, conformed: str | None) -> tuple[str | None, dict | None]:
    """The registrant's name — from the feed when the answer named it, else
    from its submissions document — and that document when it was read."""
    if conformed:
        return conformed, None
    try:
        sub = sec_client._submissions(cik)
    except httpx.HTTPError:
        return None, None
    return (sub.get("name") or "").strip() or None, sub


def _closest_registrant(name: str, registrants: list[tuple[str, str | None]], form_type: str) -> dict | None:
    """The filtered passes' rule: the registrant closest to the query by
    `_name_similarity`, at least `_MIN_NAME_SIMILARITY` — chosen over the whole
    list, so a subsidiary listed ahead of its parent does not win by position."""
    best_sim = 0.0
    best: dict | None = None
    for cik, conformed in registrants:
        registered, _ = _registrant(cik, conformed)
        if not registered:
            continue
        s = _name_similarity(name, registered)
        log.debug("SEC EDGAR: browse-edgar candidate (sim=%.2f): %r vs %r", s, name, registered)
        if s > best_sim:
            best_sim, best = s, {"cik": cik, "name": registered}
    if best and best_sim >= _MIN_NAME_SIMILARITY:
        log.info("SEC EDGAR: browse-edgar matched %r → %r (CIK=%s, sim=%.2f, form=%s)",
                 name, best["name"], best["cik"], best_sim, form_type)
        return best
    return None


def _files_what_we_read(sub: dict) -> bool:
    forms = ((sub.get("filings") or {}).get("recent") or {}).get("form") or []
    return any(f in _READ_FORMS or f.startswith(_READ_FORM_PREFIXES) for f in forms)


def _latest_filing(sub: dict) -> str:
    return max(((sub.get("filings") or {}).get("recent") or {}).get("filingDate") or [], default="")


def _exact_registrant(name: str, registrants: list[tuple[str, str | None]]) -> dict | None:
    """The type-free pass's rule (findings item 63): a registrant of exactly the
    query's name (`mapper.name_key`: legal form, tag, case, accents and
    punctuation aside), whatever it files.

    Without a form filter the index lists every registrant whose name starts
    with the query — the company, its holdings and subsidiaries, the depositary's
    ADR programme, and namesakes abroad — and similarity cannot tell them apart:
    "Nestlé" scores NESTLE HOLDINGS INC and NESTLE SA alike (0.83), "MFS
    Investment Management" scores the Japanese K.K. 1.00. The exact key takes
    NESTLE SA and refuses both MFS subsidiaries (the adviser is MASSACHUSETTS
    FINANCIAL SERVICES CO, found by no name); an ADR programme is never the
    company (`is_adr_listing`, the user's rule of 2026-10-30). EDGAR's
    duplicates of one name go to the registrant that files what the scraper
    reads, then to the one that filed last."""
    key = name_key(name)
    if not key:
        return None
    found: list[tuple[str, str, dict | None]] = []
    for cik, conformed in registrants:
        registered, sub = _registrant(cik, conformed)
        if not registered or is_adr_listing(registered):
            continue
        if name_key(registered) != key:
            log.debug("SEC EDGAR: browse-edgar registrant %r is not %r by name", registered, name)
            continue
        found.append((cik, registered, sub))
    if not found:
        return None
    if len(found) > 1:
        ranked = []
        for cik, registered, sub in found:
            if sub is None:
                try:
                    sub = sec_client._submissions(cik)
                except httpx.HTTPError:
                    sub = {}
            ranked.append(((_files_what_we_read(sub), _latest_filing(sub)), cik, registered))
        ranked.sort(key=lambda r: r[0], reverse=True)       # stable: the feed's order last
        log.info("SEC EDGAR: %d registrants named %r; taking CIK %s (files what we read: %s, last filing %s)",
                 len(ranked), name, ranked[0][1], *ranked[0][0])
        found = [(ranked[0][1], ranked[0][2], None)]
    cik, registered, _ = found[0]
    log.info("SEC EDGAR: browse-edgar matched %r → %r (CIK=%s, exact name, any form)", name, registered, cik)
    return {"cik": cik, "name": registered}


def _lookup_edgar_by_name(name: str) -> dict | None:
    """
    Second vector: EDGAR's registered company-name index via browse-edgar.

    Unlike the EFTS full-text search, browse-edgar only returns companies whose
    *registered name* starts with the query — filing text that merely mentions
    the company never shows up here, so a company that is no registrant at
    all (Heineken, Saudi Aramco) stays unfound however often filings name it.

    Three passes (`_BROWSE_PASSES`): 10-K filers, 20-F filers — the closest
    name wins — and then every registrant, where only the exact name does.
    The third is what finds a filer with no annual report: a 13D/G holder
    (Capital Ventures International, Tencent Holdings Ltd), a Form 4 reporting
    owner (NESTLE SA), a 13F manager. Until 2026-11-06 the lookup stopped after
    20-F, and a holder in the target list for its own holdings had a CIK only
    when an issuer's scrape had happened to write it first: Tesla's 13D/G page
    was lost to the network and its seven holder-targets scraped nothing
    (findings item 63). Each pass reads both of EDGAR's answer shapes
    (`_browse_registrants`).
    """
    for form_type in _BROWSE_PASSES:
        if not form_type:
            break                       # the type-free pass: `_exact_name_registrant` below
        registrants = _browse_index(name, form_type)
        if registrants is None:
            continue
        if match := _closest_registrant(name, registrants, form_type):
            return match
    if match := _exact_name_registrant(name):
        return match
    log.info("SEC EDGAR: browse-edgar found no registered match for %r", name)
    return None


def _named_registrants(name: str, query: str) -> tuple[list[tuple[str, str | None]] | None, bool]:
    """Every registrant that may be exactly ``name``, found under ``query``,
    and whether the bulk name list answered: from the list when there is one
    (`sec_names.registrants_named` — no request, and no punctuation trap: the
    live index lists nothing for "Amgen Inc." with its dot), else from the live
    index. A list hit carries no name on purpose: the list holds former names
    too, so `_exact_registrant` reads the filer's record and holds its CURRENT
    name against the query. None when the live request failed."""
    from app.scraper import sec_names
    listed = sec_names.registrants_named(name, query)
    if listed is not None:
        return [(cik, None) for cik in listed[:_BROWSE_COUNT[""]]], True
    return _browse_index(query, ""), False


def _files_something_read(match: dict, name: str, how: str) -> bool:
    """Whether the matched registrant files what the scraper reads; says so when not."""
    try:
        sub = sec_client._submissions(match["cik"])
    except httpx.HTTPError:
        sub = {}
    if _files_what_we_read(sub):
        return True
    log.info("SEC EDGAR: %r %s is %r (CIK %s), which files nothing the scraper reads — not taken",
             name, how, match["name"], match["cik"])
    return False


def _exact_name_registrant(name: str) -> dict | None:
    """The registrant of exactly this name, whatever it files — the type-free
    pass of the name lookup, and the rule a proxy table's holder is found by
    (`sec_writer._table_holder_node`): first among the names starting with the
    name as written, then among those starting with its base words.

    A registrant found through the bulk name list is taken when it files what
    the scraper reads: the list reaches EDGAR's dormant duplicates that the
    live index's spelling hid — "T. Rowe Price Group, Inc." lists nothing
    live, and in the list it is T ROWE PRICE GROUP INC, five no-action letters
    of 2015, not the filer PRICE T ROWE GROUP INC (item 59). One that files
    nothing read is left to the live index, asked as before the list: a fund
    that files Form D alone, a broker that files 13F notices, is still the
    holder a proxy names, and the live index's answer for the name as written
    took 12 such of 752 holder names (guard 2026-11-24)."""
    tried: set[str] = set()
    registrants, from_list = _named_registrants(name, name)
    if registrants:
        match = _exact_registrant(name, registrants)
        if match and from_list and not _files_something_read(match, name, "in the name list"):
            live = _browse_index(name, "")
            match = _exact_registrant(name, live) if live else None
        if match:
            return match
        tried = {cik for cik, _ in registrants}

    # The index is a PREFIX search on the registered spelling, so a query
    # carrying another form word than the registrant's never lists it:
    # "Independent Franchise Partners, Llp" lists nothing, "independent
    # franchise partners" lists the 13F filer (findings item 69, 2026-11-06
    # rebuild). So the type-free pass asks once more with the name's base words
    # when the full name listed no registrant of its key; the hold is the same
    # exact key — and the registrant must file what the scraper reads
    # (`_files_what_we_read`): the broader query lists EDGAR's dormant
    # duplicates alone when the filer is spelled otherwise — "t rowe price
    # group" lists T ROWE PRICE GROUP INC (five no-action letters, 2015) and
    # never the filer PRICE T ROWE GROUP INC, conformed surname first (item
    # 59) — and a record with nothing to read (YouTube Inc, two REGDEX of
    # 2006) gives the scrape nothing a name search should decide.
    base = " ".join(name_key(name))
    if base and base.casefold() != name.casefold():
        # not the ones the first query already held against the name
        registrants = [r for r in _named_registrants(name, base)[0] or [] if r[0] not in tried]
        if registrants and (match := _exact_registrant(name, registrants)) \
                and _files_something_read(match, name, "by its base name"):
            return match
    return None


def _browse_index(query: str, form_type: str) -> list[tuple[str, str | None]] | None:
    """The registrants browse-edgar lists for ``query`` (a prefix of the
    registered name) filtered to ``form_type`` filers ("" for every
    registrant), in both of EDGAR's answer shapes; None when the request or
    its parse failed."""
    wanted = _BROWSE_COUNT.get(form_type, _BROWSE_COUNT_DEFAULT)
    try:
        resp = sec_client._response(sec_client.BROWSE_URL, {      # the retry (findings item 38)
            "company":     query,
            "action":      "getcompany",
            "type":        form_type,
            "dateb":       "",
            "owner":       "include",
            "count":       sec_client.browse_count(wanted),
            "search_text": "",
            "output":      "atom",
        })
    except httpx.HTTPError as exc:
        log.debug("SEC EDGAR: browse-edgar (%s) failed for %r: %s", form_type or "any form", query, exc)
        return None
    try:
        root = ET.fromstring(resp.content)
    except (ET.ParseError, TypeError):
        return None
    # the page may be larger than asked for (`browse_count`); each entry kept
    # costs a submissions request, so the list stays as long as it always was
    return _browse_registrants(root)[:wanted]


# ── EDGAR search helpers ──────────────────────────────────────────────────────

def _parse_hit(src: dict) -> tuple[str | None, str | None]:
    """
    Extract (entity_name, cik) from an EDGAR full-text search _source dict.
    Real field names: display_names, ciks, adsh (not entity_name/accession_no).
    """
    ciks = src.get("ciks", [])
    cik  = ciks[0].zfill(10) if ciks else sec_client._cik_from_accession(src.get("adsh", ""))

    display_names = src.get("display_names", [])
    entity_name   = None
    if display_names:
        entity_name = re.split(r"\s{2,}|\s+\(", display_names[0])[0].strip()

    return entity_name, cik


# ── Company search ────────────────────────────────────────────────────────────

def search_company(name: str) -> dict | None:
    """
    Find a company's CIK and registered name on EDGAR.

    Three-vector strategy:
    1. company_tickers.json      — fast, unambiguous for all US-listed companies.
    2. browse-edgar name index   — EDGAR's registered company-name search: 10-K and
                                   20-F filers by the closest name, then any registrant
                                   of exactly that name, whatever it files (a 13D/G
                                   holder, a Form 4 owner, a 13F manager; findings
                                   item 63). A company that is no registrant at all
                                   (Heineken, Saudi Aramco) stays unfound here, since
                                   its name never appears as a filer's — only inside
                                   other filers' text.
    3. EFTS full-text search     — last resort, guarded by name-similarity check.

    Returns {cik: zero-padded-10-digit, name: str} or None.
    """
    log.info("SEC EDGAR: searching for company %r", name)

    result = _lookup_in_tickers(name)
    if result:
        return result

    log.info("SEC EDGAR: tickers miss for %r, trying browse-edgar name index", name)
    result = _lookup_edgar_by_name(name)
    if result:
        return result

    log.info("SEC EDGAR: browse-edgar miss for %r, falling back to EFTS full-text search", name)
    for forms in ("10-K", "DEF 14A"):
        try:
            data = sec_client._get(sec_client.SEARCH_URL, {"q": f'"{name}"', "forms": forms})
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                continue
            entity_name, cik = _parse_hit(hits[0]["_source"])
            if entity_name and cik:
                sim = _name_similarity(name, entity_name)
                if sim < _MIN_NAME_SIMILARITY:
                    log.warning(
                        "SEC EDGAR: EFTS match rejected (similarity %.2f < %.2f): %r vs %r",
                        sim, _MIN_NAME_SIMILARITY, name, entity_name,
                    )
                    continue
                log.info("SEC EDGAR: full-text matched %r → CIK=%s (sim=%.2f)", entity_name, cik, sim)
                return {"cik": cik, "name": entity_name}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("SEC EDGAR: company search error (%s): %s", forms, exc)

    log.warning("SEC EDGAR: company %r not found", name)
    return None


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def _name_words(name: str) -> list[str]:
    """The words of a person's name, without commas and a Jr./Sr./III suffix."""
    return [w for w in name.replace(",", " ").split()
            if w.strip(".").lower() not in _NAME_SUFFIXES]


def _lookup_person_ciks(name: str) -> list[str]:
    """Resolve an individual's name to the EDGAR filer CIKs it may be (they file Form 4s).

    EDGAR names a person surname first ("DENHOLM ROBYN M") and its name search
    matches from the start, so "Robyn Denholm" — the order Wikidata gives —
    finds nobody (findings item 29). The surname-first form is asked first,
    then the name as given (an SEC-styled name already is surname first).
    Answered from the bulk name list where one is kept (`sec_names`), else by
    the live search. A search with one match answers with that filer's name, kept only when it
    has the given name and the surname; a search with several answers with
    CIKs alone, each a candidate the caller's issuer check decides."""
    words = _name_words(name)
    if not words:
        return []
    # the bulk name list, when this machine keeps one: the same question
    # without a request (findings item 135)
    from app.scraper import sec_names
    listed = sec_names.person_ciks(words, LIST_PERSON_CANDIDATES)
    if listed is not None:
        return listed
    return _search_person_ciks(words)


def _search_person_ciks(words: list[str]) -> list[str]:
    """`_lookup_person_ciks` by EDGAR's live name search (browse-edgar)."""
    queries = [f"{words[-1]} {words[0]}"] if len(words) > 1 else []
    queries.append(" ".join(words))
    wanted = {w.strip(".").upper() for w in (words[0], words[-1])}
    found: list[str] = []
    for query in queries:
        try:
            resp = sec_client._response(sec_client.BROWSE_URL, {      # the retry (findings item 38)
                "company": query, "action": "getcompany", "type": "4",
                "dateb": "", "owner": "include", "count": sec_client.browse_count(MAX_PERSON_CANDIDATES),
                "output": "atom",
            })
        except httpx.HTTPError:
            continue
        conformed = re.search(r"<conformed-name>([^<]*)</conformed-name>", resp.text)
        if conformed and not wanted <= set(conformed.group(1).upper().replace(",", " ").split()):
            continue                       # the one filer it found is someone else
        for cik in re.findall(r"<cik>(\d+)</cik>", resp.text):
            if cik.zfill(10) not in found:
                found.append(cik.zfill(10))
        if found:
            break
    return found[:MAX_PERSON_CANDIDATES]
