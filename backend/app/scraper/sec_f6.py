"""Form F-6 — the CUSIP of a foreign issuer's American Depositary Shares.

A foreign company whose shares trade in the US as ADRs files no 13D/G cover
about itself and no 10-K, so EDGAR gives its node no CUSIP — and without one
the securities bridge (`gleif_isin`, findings item 71) cannot reach its LEI:
GLEIF's ISIN file maps the ADS's US ISIN to the issuer, but nothing on the
node names that ISIN. Nestlé's `sec:0000792990 NESTLE SA` and the GLEIF node
`lei:KY37LUS27QQX7BB93L28 Nestle S.A.` stayed two nodes because of it (items
68 and 72).

The depositary's registration of the ADSs — Form F-6 and its family (F-6EF,
F-6 POS, F-6/A), filed under the FOREIGN ISSUER's CIK — states the ADS CUSIP
in the form of receipt (the deposit agreement's exhibit A or B): Nestlé's
F-6 POS of 2026-02-20 "ADS CUSIP No.: 641069406", its F-6EF of 2009
"CUSIP NO: 641069406", its F-6/A of 2004 "CUSIP Number(s): 641069 50 5" (the
old Rule 144A ADS). GLEIF's file maps US6410694060 and US6410695059 — both
derived from those CUSIPs by rule — to KY37LUS27QQX7BB93L28.

What the filings taught (findings item 72, 2026-11-07):
- the form of receipt is a TEMPLATE in most filings: "CUSIP NUMBER: _______"
  is a blank to fill in, and "a CUSIP number that is different from any CUSIP
  number…" is prose — only a nine-character code whose check digit holds is
  taken (`gleif_isin.cusip_valid`);
- the CUSIP sits in an exhibit, not in the primary document, and in a
  different exhibit per filing — the full submission text (`<accession>.txt`)
  carries every document of the filing in one request;
- an unsponsored or old programme may state none (Wesfarmers' six F-6 filings:
  "CUSIP #" with nothing after it) — then nothing is written, never a guess;
- several depositaries may each register a programme: every stated code is
  kept, all of them the issuer's securities.

Read newest first, stopping at the first filing that states one: the newest
statement is the programme in force, and the reads stay at one or two
requests for a filer that has F-6 filings at all (none for one that does not:
the submissions document says so).
"""
from __future__ import annotations

import html
import logging
import re

from app.scraper import sec_edgar
from app.scraper.gleif_isin import cusip_valid
from app.scraper.sec_edgar import ARCHIVES_URL, _cik_int, _iso_date

# The requests go through `sec_edgar.<helper>` at call time, never a name bound
# at import: this module is imported lazily inside a scrape, and a binding made
# while a test patched `sec_edgar._submissions` kept the patch for good.

log = logging.getLogger(__name__)

#: the depositary-share registration family, as EDGAR names its forms
F6_FORM = re.compile(r"^F-6(EF)?( POS)?(/A)?$")
#: filings read at most per scrape, newest first
MAX_FILINGS = 8
#: the run's section name when a request is lost (findings item 41)
SECTION = "F-6 ADS CUSIP"

# "CUSIP" with its label words ("No.", "NO", "Number(s)", "#"), then the code:
# nine characters, optionally printed in the 6-2-1 groups ("641069 50 5"), in
# brackets ("CUSIP No. [641069 40 6]") or broken over a line ("641069\n50 5").
# The label is matched case-blind, the code only in capitals and digits, so
# prose ("CUSIP number that is different") never yields a candidate.
_CUSIP = re.compile(
    r"(?i:cusip)(?:\s+(?i:no\.?|number\(s\)|number|nos?\.?|#))?\s*[:#.]?\s*\[?"
    r"(?P<code>[0-9A-Z]{6}\s?[0-9A-Z]{2}\s?[0-9])(?![0-9A-Za-z])")
_TAG = re.compile(r"<[^>]+>")


def f6_filings(submissions: dict, max_filings: int = MAX_FILINGS) -> list[dict]:
    """The filer's F-6 family filings from its submissions document, newest
    first: `{form, accession, filing_date}`. The inline `filings.recent` list
    only — a programme registered before the newest thousand filings is
    restated in a later F-6 POS or F-6EF when it is amended."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    rows = zip(recent.get("form") or [], recent.get("accessionNumber") or [],
               recent.get("filingDate") or [])
    out = [{"form": f, "accession": a, "filing_date": _iso_date(d)}
           for f, a, d in rows if F6_FORM.match(f or "")]
    out.sort(key=lambda r: r["filing_date"] or "", reverse=True)
    return out[:max_filings]


def parse_ads_cusips(text: str) -> list[str]:
    """Every check-digit-valid CUSIP a filing's text states after the word
    "CUSIP", in order, without the 6-2-1 spaces. Markup and entities are
    removed first (`&nbsp;` between the label and the code)."""
    plain = html.unescape(_TAG.sub(" ", text or "")).replace("\xa0", " ")
    found = []
    for m in _CUSIP.finditer(plain):
        code = re.sub(r"\s", "", m.group("code"))
        if cusip_valid(code):
            found.append(code)
    return list(dict.fromkeys(found))


def submission_url(cik: str, accession: str) -> str:
    """The full submission text of one filing — every document in one file."""
    return f"{ARCHIVES_URL}/{_cik_int(cik)}/{accession.replace('-', '')}/{accession}.txt"


def fetch_ads_cusips(cik: str, max_filings: int = MAX_FILINGS) -> dict:
    """The ADS CUSIP(s) the filer's newest F-6 filing that states one gives:
    `{"cusips": [...], "source_url": url | None, "filings_read": n}`. A filer
    without F-6 filings costs the submissions read only. A request lost to the
    network is recorded as a lost section (the run goes `partial`) and the
    reading stops — an older filing must not stand in for a newer one."""
    empty = {"cusips": [], "source_url": None, "filings_read": 0}
    try:
        filings = f6_filings(sec_edgar._submissions(cik), max_filings)
    except Exception as exc:  # noqa: BLE001 - an id is a nicety, never worth aborting a scrape
        if not sec_edgar._section_failed(SECTION, exc):
            log.warning("SEC EDGAR: F-6 list for CIK=%s failed: %s", cik, exc)
        return empty
    read = 0
    for filing in filings:
        url = submission_url(cik, filing["accession"])
        try:
            text = sec_edgar._get_text(url)
        except Exception as exc:  # noqa: BLE001
            if not sec_edgar._section_failed(SECTION, exc):
                log.warning("SEC EDGAR: F-6 %s for CIK=%s failed: %s", filing["accession"], cik, exc)
                continue            # a 404 is "not there": the next filing may state it
            return {**empty, "filings_read": read}
        read += 1
        cusips = parse_ads_cusips(text)
        if cusips:
            return {"cusips": cusips, "source_url": url, "filings_read": read}
    return {**empty, "filings_read": read}
