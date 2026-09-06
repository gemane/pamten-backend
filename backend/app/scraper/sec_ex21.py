"""SEC Exhibit 21 — the statutory subsidiary list.

Every US public company's 10-K carries Exhibit 21 ("Subsidiaries of the
registrant"), a name + jurisdiction table; foreign private issuers file the
same as Exhibit 8.1 of the 20-F. It is the statutory answer to "who does this
company own" — the role Wikidata's community-edited subsidiary lists used to
play before they were demoted to claims-only.

Two honesty caveats carried onto every edge:
- Filers list only *significant* subsidiaries (Reg S-K Item 601(b)(21) lets
  them omit the rest); the absence of a subsidiary here proves nothing.
- The exhibit states existence and jurisdiction, never a stake — edges carry
  ownership_type "subsidiary" semantics with no invented percentage.

Parsing: the exhibit is free-form HTML, but in practice a two-column table
(name | jurisdiction) or its visual equivalent. The parser reads table rows
first and falls back to line pairs; jurisdictions like "Delaware, U.S." are
split into country US (the state is kept as display text). Measured, not
documented: Apple/Microsoft/Alphabet exhibits are all two-column tables.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from app.scraper.sec_edgar import _cik10, _get, _get_text, _iso_date

log = logging.getLogger(__name__)

# 10-K first (domestic, Ex-21), then 20-F (foreign private issuers, Ex-8.1 —
# same content, different exhibit number in the rulebook).
_ANNUAL_FORMS = ("10-K", "20-F")
_EXHIBIT_PATTERNS = (re.compile(r"ex[-._]?21", re.I),
                     re.compile(r"exhibit[-._]?21", re.I),
                     re.compile(r"subsidiar", re.I),
                     re.compile(r"ex[-._]?8[-._]?1", re.I))


def annual_exhibit_candidates(cik: str) -> list[dict]:
    """Candidate subsidiary-exhibit files from the newest 10-K/20-F.

    A list, not one file: exhibit numbering is ambiguous by filename alone —
    AB InBev's `dex215.htm` is exhibit 2.15 (securities descriptions), not
    21.5, and only parsing tells them apart. For a 20-F the ex-8 patterns
    are tried first (that is where its subsidiary list lives), for a 10-K
    the ex-21 ones. A 404 on the submissions API (a stale CIK on an old
    filer) is "no filings", not an error."""
    try:
        subs = _get(f"https://data.sec.gov/submissions/CIK{_cik10(cik)}.json")
    except Exception as exc:  # noqa: BLE001 - stale CIKs 404; treat as absent
        log.info("submissions unavailable for CIK %s: %s", cik, exc)
        return []
    recent = (subs.get("filings") or {}).get("recent") or {}
    rows = zip(recent.get("form") or [], recent.get("accessionNumber") or [],
               recent.get("filingDate") or [])
    ex21 = [re.compile(r"ex[-._]?21", re.I), re.compile(r"exhibit[-._]?21", re.I),
            re.compile(r"subsidiar", re.I)]
    ex8 = [re.compile(r"ex[-._]?8[-._]?1", re.I), re.compile(r"dex8", re.I),
           re.compile(r"subsidiar", re.I)]
    for form, accession, filed in rows:
        if form not in _ANNUAL_FORMS:
            continue
        acc = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
        index = _get(f"{base}/index.json")
        names = [it.get("name") or ""
                 for it in (index.get("directory") or {}).get("item") or []]
        patterns = ex8 + ex21 if form == "20-F" else ex21 + ex8
        out, seen = [], set()
        for pat in patterns:
            for name in names:
                if name in seen or not name.lower().endswith((".htm", ".html")):
                    continue
                if pat.search(name):
                    seen.add(name)
                    out.append({"url": f"{base}/{name}", "form": form,
                                "filing_date": _iso_date(filed),
                                "accession": accession})
        # the newest annual filing decides; do not walk back to older years
        return out
    return []


class _TableTextParser(HTMLParser):
    """Tables of rows of cell texts. Per-TABLE grouping matters: an exhibit
    can hold several tables (cover blocks, direct + indirect sections), and
    each carries its own header row naming its columns."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    @property
    def rows(self) -> list[list[str]]:   # flattened view (tests, debugging)
        return [r for t in self.tables for r in t]

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            # Zero-width characters first: Texas Roadhouse's exhibit has a
            # whole filler column of U+200B, which is truthy and was taken as
            # the jurisdiction of all 62 subsidiaries.
            text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", "".join(self._cell))
            text = re.sub(r"\s+", " ", text).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                (self._table if self._table is not None else
                 self._orphan()).append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def _orphan(self) -> list:
        # rows outside any <table> (malformed HTML) — collect as one table
        if not self.tables or self.tables[-1] is not self.__dict__.setdefault(
                "_orphans", []):
            self.tables.append(self.__dict__["_orphans"])
        return self.__dict__["_orphans"]

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


# Header detection: which column is which, by what the filer CALLS it.
_H_NAME = re.compile(r"subsidiar|name|entity|compan", re.I)
_H_JURISDICTION = re.compile(r"jurisdiction|incorporat|organi[sz]|country|state", re.I)
_H_OWNERSHIP = re.compile(r"ownership|percent|%|owned|interest", re.I)
# "Location"/"Address" is where an office SITS, not where the company is
# registered — Bank of America has both columns, and taking Location wrote
# "San Francisco, CA" as a jurisdiction.
_H_NOT_JURISDICTION = re.compile(r"location|address|city", re.I)

_PERCENT = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*%\s*$")


def _find_header(table: list[list[str]]) -> dict | None:
    """Column indexes from a header row in the table's first few rows."""
    for row in table[:5]:
        name_i = jur_i = own_i = None
        for i, cell in enumerate(row):
            if not cell:
                continue
            if jur_i is None and _H_JURISDICTION.search(cell) \
                    and not _H_NOT_JURISDICTION.search(cell):
                jur_i = i
            elif own_i is None and _H_OWNERSHIP.search(cell):
                own_i = i
            elif name_i is None and _H_NAME.search(cell):
                name_i = i
        if name_i is not None and jur_i is not None:
            return {"name": name_i, "jurisdiction": jur_i, "ownership": own_i,
                    "header_row": row}
    return None


# Header/footer rows and the registrant's own line are not subsidiaries.
_NOISE = re.compile(
    r"^(subsidiaries|name|entity|jurisdiction|state|country|list of|exhibit|"
    r"significant|(?:in)?directly[- ]|partially[- ]|wholly[- ]owned|\*+$)", re.I)


def parse_exhibit(html: str) -> list[dict]:
    """[{name, jurisdiction, stake_percent?}] from an Ex-21/Ex-8.1 page.

    Per table: a header row naming the columns wins (that is how Bank of
    America's Location column is told apart from its Jurisdiction one, and
    how Astronics' Ownership Percentage column becomes a real stake). A
    table with a header that names NO jurisdiction-ish column is a different
    kind of table (AB InBev's securities listings) and is skipped whole.
    Only a headerless table falls back to the first-two-columns heuristic.

    Jurisdiction text is kept as filed; the ISO mapping is the writer's
    separate, lossy view of it."""
    parser = _TableTextParser()
    parser.feed(html)
    out, seen = [], set()
    for table in parser.tables:
        header = _find_header(table)
        if header is None and any(any(c) for r in table[:5] for c in r
                                  if _H_NOT_JURISDICTION.search(c or "")):
            continue   # a labelled table that is about locations, not registration
        table_rows: list[dict] = []
        for row in table:
            if header is not None:
                if row is header["header_row"]:
                    continue
                name = row[header["name"]] if header["name"] < len(row) else ""
                jurisdiction = (row[header["jurisdiction"]]
                                if header["jurisdiction"] < len(row) else "")
                stake = None
                if header["ownership"] is not None and header["ownership"] < len(row):
                    if m := _PERCENT.match(row[header["ownership"]] or ""):
                        stake = float(m.group(1))
            else:
                cells = [c for c in row if c]
                if len(cells) < 2:
                    continue
                name, jurisdiction, stake = cells[0], cells[1], None
                if _PERCENT.match(jurisdiction):
                    # a percent is a stake, not a place — try the next cell
                    stake = float(_PERCENT.match(jurisdiction).group(1))
                    jurisdiction = cells[2] if len(cells) > 2 else ""
            if not name or not jurisdiction:
                continue
            if _NOISE.match(name) or _NOISE.match(jurisdiction):
                continue
            # a jurisdiction is short; a long second column means prose
            if len(jurisdiction) > 60 or len(name) < 2:
                continue
            entry = {"name": name.rstrip("*").strip(),
                     "jurisdiction": jurisdiction}
            if stake is not None:
                entry["stake_percent"] = stake
            table_rows.append(entry)
        # Table-level sanity for HEADERLESS tables: a real subsidiary table's
        # jurisdictions overwhelmingly map to countries; a securities listing's
        # ("Trading symbol", "New York Stock Exchange") map not at all. A
        # named header earns trust; a heuristic table must earn it by content.
        if header is None and table_rows:
            mapped = sum(1 for e in table_rows
                         if jurisdiction_country(e["jurisdiction"]) is not None)
            if mapped < len(table_rows) / 2:
                continue
        for entry in table_rows:
            key = entry["name"].casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


_US_SUFFIX = re.compile(r",?\s*(U\.?S\.?A?\.?|United States)$", re.I)
# Chubb writes "USA (Delaware)"; Occidental writes Canadian provinces bare.
_USA_PAREN = re.compile(r"^(U\.?S\.?A?\.?|United States)\s*\(", re.I)
_GB_NATIONS = {"england & wales", "england and wales", "england", "scotland",
               "wales", "northern ireland"}
_CA_PROVINCES = {"alberta", "british columbia", "manitoba", "new brunswick",
                 "newfoundland and labrador", "nova scotia", "ontario",
                 "prince edward island", "quebec", "saskatchewan"}
_EXTRA_PLACES = {"nevis": "KN", "cayman": "KY", "prc": "CN",
                 "british virgin islands": "VG", "virgin islands (british)": "VG",
                 "korea": "KR",       # bare "Korea" in practice means the South
                 "columbia": "CO",    # a recurring filer typo for Colombia
                 "dubai": "AE", "macau": "MO", "macau sar": "MO",
                 "macao sar": "MO"}
# "Saudi Arabia, Kingdom of" — inverted official names
_INVERTED_TAIL = re.compile(r",\s*(kingdom|republic|state|grand duchy)\s+of$", re.I)

# Some filers fuse the legal form into the jurisdiction cell — Texas
# Roadhouse writes "Kentucky limited liability company". Longest forms
# first, stripped repeatedly, and only as a FALLBACK after the direct
# lookups fail, so a real country name is never eaten.
_LEGAL_FORM_TAIL = re.compile(
    r"\s+(non-?profit corporation|limited liability compan(?:y|ies)|limited partnership|"
    r"general partnership|unlimited company|corporation|company|"
    r"partnership|entity|trust|llc|l\.?l\.?p\.?|l\.?p\.?|inc\.?)$", re.I)


def jurisdiction_country(jurisdiction: str | None) -> str | None:
    """ISO-2 country for a filed jurisdiction, or None when unmappable.

    "Delaware, U.S." → US (the state stays in the stored jurisdiction text);
    bare US state names → US; country names via the shared nationality table.
    None means "store the text, skip the code" — never guess."""
    from app.scraper.gleif_reference import _US_STATE_NAMES
    from app.scraper.maintenance import nationality_to_iso2
    cleaned = (jurisdiction or "").strip()
    # Curly apostrophes and a leading article are filer typography, not
    # meaning: "The People\u2019s Republic of China" is China.
    cleaned = cleaned.replace("\u2019", "'")
    cleaned = re.sub(r"^the\s+", "", cleaned, flags=re.I)
    cleaned = _INVERTED_TAIL.sub("", cleaned).strip()
    if not cleaned:
        return None
    if _US_SUFFIX.search(cleaned) or _USA_PAREN.match(cleaned):
        return "US"
    low = cleaned.casefold()
    if low in _GB_NATIONS:
        return "GB"
    if low in _CA_PROVINCES:
        return "CA"
    if low in _EXTRA_PLACES:
        return _EXTRA_PLACES[low]
    if cleaned.casefold() in _US_STATE_NAMES:   # bare "Delaware" — filers vary
        return "US"
    if code := nationality_to_iso2(cleaned):
        return code
    # "Canada (Ontario)" — country with a parenthetical subdivision: the
    # country part decides (the _USA_PAREN special case, generalized).
    if m := re.match(r"^([^()]+?)\s*\(", cleaned):
        if code := jurisdiction_country(m.group(1)):
            return code
    # "Quebec, Canada" / "Toronto, Ontario, Canada" — the LAST comma part is
    # the country; the front is a subdivision the code doesn't need.
    if "," in cleaned:
        if code := jurisdiction_country(cleaned.rsplit(",", 1)[1]):
            return code
    # Fallback: peel fused legal forms ("Kentucky limited liability company")
    # and retry — repeatedly, since forms stack ("X company limited").
    stripped = cleaned
    while (peeled := _LEGAL_FORM_TAIL.sub("", stripped)) != stripped:
        stripped = peeled.strip()
        if stripped.casefold() in _US_STATE_NAMES:
            return "US"
        if code := nationality_to_iso2(stripped):
            return code
    return None


# ISO 3166-2 subdivision codes the FRONTEND can name (US states, CA provinces,
# GB nations, and the handful GLEIF uses). A jurisdiction like "Florida, USA"
# says more than "US" — it says Florida — and the panel shows it as a
# "Registered in" row. Only these families; elsewhere a region is administrative,
# not a domicile choice, so there is nothing to record (see _jurisdiction_code
# in gleif_lei_cdf for the same reasoning).
_CA_PROVINCE_CODES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL", "nova scotia": "NS",
    "northwest territories": "NT", "nunavut": "NU", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "saskatchewan": "SK",
    "yukon": "YT",
}
_GB_NATION_CODES = {"england": "ENG", "scotland": "SCT", "wales": "WLS",
                    "northern ireland": "NIR", "england & wales": "ENG",
                    "england and wales": "ENG"}
_OTHER_SUBDIVISION_CODES = {"nevis": "KN-N", "saint kitts": "KN-K",
                           "dubai": "AE-DU", "abu dhabi": "AE-AZ",
                           "sharjah": "AE-SH"}


def _us_state_code(name: str) -> str | None:
    from app.scraper.gleif_reference import _US_STATES
    inv = {v.casefold(): k for k, v in _US_STATES.items()}
    return inv.get(name.strip().casefold())


def jurisdiction_subdivision(jurisdiction: str | None) -> str | None:
    """ISO 3166-2 subdivision for a filed jurisdiction, or None.

    "Florida, USA" -> US-FL, "Delaware" -> US-DE, "England" -> GB-ENG,
    "Quebec, Canada" -> CA-QC, "Canada (Ontario)" -> CA-ON, "Nevis" -> KN-N.
    Only families the panel can name; None means "no finer grain to show",
    never a country (that is jurisdiction_country's job)."""
    cleaned = (jurisdiction or "").strip().replace("\u2019", "'")
    if not cleaned:
        return None
    # peel a US/Canada country suffix or parenthetical to expose the state/province
    # "Florida, USA" / "Delaware, U.S." -> "Florida"; "USA (Delaware)" -> "Delaware"
    core = _US_SUFFIX.sub("", cleaned).strip().strip(",").strip()
    for pat, iso in ((_USA_PAREN, "US"),):
        if pat.match(cleaned):
            inside = re.search(r"\(([^)]+)\)", cleaned)
            if inside:
                core = inside.group(1).strip()
    # "Canada (Ontario)" / "Quebec, Canada" -> the province part
    m_paren = re.match(r"^(?:canada)\s*\(([^)]+)\)$", cleaned, re.I)
    if m_paren:
        core = m_paren.group(1).strip()
    elif "," in cleaned and cleaned.rsplit(",", 1)[1].strip().casefold() in (
            "canada", "usa", "u.s.", "u.s.a.", "united states"):
        core = cleaned.rsplit(",", 1)[0].strip()
        # a two-level "Toronto, Ontario, Canada" -> take the last non-country part
        if "," in core:
            core = core.rsplit(",", 1)[1].strip()
    # peel a fused legal form ("Virginia corporation" -> "Virginia"), same as
    # jurisdiction_country's fallback
    peeled = core
    while (nxt := _LEGAL_FORM_TAIL.sub("", peeled)) != peeled:
        peeled = nxt.strip()
    core = peeled or core
    low = core.casefold()
    if code := _us_state_code(core):
        return f"US-{code}"
    if code := _CA_PROVINCE_CODES.get(low):
        return f"CA-{code}"
    if code := _GB_NATION_CODES.get(low):
        return f"GB-{code}"
    if code := _OTHER_SUBDIVISION_CODES.get(low):
        return code
    return None


def fetch_subsidiaries(cik: str) -> dict | None:
    """The latest annual filing's subsidiary list for a CIK, with provenance.

    Tries each candidate exhibit until one parses to subsidiaries — filename
    numbering is ambiguous (ex215 = 2.15 or 21.5), so the content decides.
    {"subsidiaries": [...], "form", "filing_date", "url"} or None."""
    for meta in annual_exhibit_candidates(cik):
        subs = parse_exhibit(_get_text(meta["url"]))
        if subs:
            return {"subsidiaries": subs, "form": meta["form"],
                    "filing_date": meta["filing_date"], "url": meta["url"]}
        log.info("candidate %s parsed to zero subsidiaries — trying the next",
                 meta["url"])
    return None
