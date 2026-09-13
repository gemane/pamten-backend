"""SEC Form D — the private-company window.

Every private raise under Regulation D is notified on Form D, filed by the
ISSUER as structured XML. It is the only statutory filing family that names a
private company's board: SpaceX's lists Musk, Shotwell, Gracias, Jurvetson —
people no 13F or 13D/G can ever surface for an unregistered issuer, and where
VC partners' board seats become visible.

What is taken (mapped per docs/data-model.md):
- related persons → Person + HAS_ROLE (Executive Officer / Director /
  Promoter, verbatim — Form D's own vocabulary);
- the issuer's jurisdiction of incorporation → country + jurisdiction_code,
  fill-if-missing (a Form D "DELAWARE" becomes US / US-DE via the same
  resolver Exhibit 21 uses);
- filing date → the roles' source_date. Form D states NO tenure dates and no
  person identifiers — names only — so roles carry the filing's date and the
  usual name-matched person resolution (with the CIK-conflict and
  entity-suffix guards).

Deliberately NOT taken: offering amounts as graph facts (a raise is an event,
not ownership — noted for a future timeline concept), and corporate "related
persons" (a GP LLC listed as an officer is an entity; minting it as a Person
is the Berkshire bug — skipped and counted instead).
"""
from __future__ import annotations

import logging
import re

import httpx
import xml.etree.ElementTree as ET

from app.scraper.sec_edgar import _cik10, _get, _get_text, _iso_date

log = logging.getLogger(__name__)


def latest_form_d(cik: str) -> dict | None:
    """The newest Form D (or D/A) filing's parsed content, or None.

    Newest only, like Exhibit 21: one filing = one ingest, and the gate key is
    the accession. Older Form Ds hold earlier boards — history for a later
    iteration, not silently mixed into the present.
    """
    subs = _get(f"https://data.sec.gov/submissions/CIK{_cik10(cik)}.json")
    recent = (subs.get("filings") or {}).get("recent") or {}
    rows = zip(recent.get("form") or [], recent.get("accessionNumber") or [],
               recent.get("filingDate") or [])
    for form, accession, filed in rows:
        if form not in ("D", "D/A"):
            continue
        acc = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
        try:
            xml_text = _get_text(f"{base}/primary_doc.xml")
        except httpx.HTTPStatusError as exc:
            # Only a 404 means "no structured XML" (pre-2009 paper-era D).
            # A 5xx is EDGAR having a moment — surface it, or a transient
            # error masquerades as "this issuer never filed".
            if exc.response.status_code == 404:
                return None
            raise
        parsed = parse_form_d(xml_text)
        if parsed is None:
            return None
        parsed |= {"accession": accession, "filing_date": _iso_date(filed),
                   "url": f"{base}/primary_doc.xml", "form": form}
        return parsed
    return None


def _text(el, tag: str) -> str | None:
    m = el.find(f".//{{*}}{tag}") if hasattr(el, "find") else None
    if m is None or m.text is None:
        return None
    return re.sub(r"\s+", " ", m.text).strip() or None


def parse_form_d(xml_text: str) -> dict | None:
    """{persons: [{name, roles, is_entity}], jurisdiction, entity_type,
    offering_amount, amount_sold} from a Form D primary_doc.xml."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    persons = []
    for rp in root.findall(".//{*}relatedPersonInfo"):
        first = _text(rp, "firstName") or ""
        last = _text(rp, "lastName") or ""
        # Fund filings list their GP LLCs as related persons with a literal
        # "N/A" first name ("N/A" + "137 Holdings Alpha, LLC") — the N/A is
        # filler, not a name part.
        parts = [w for w in f"{first} {last}".split() if w.upper().rstrip(".") != "N/A"]
        name = " ".join(w.capitalize() for w in parts)
        roles = [r.text.strip() for r in rp.findall(".//{*}relationship")
                 if r.text and r.text.strip()]
        if not name or not roles:
            continue
        persons.append({"name": name, "roles": roles})
    def _num(tag):
        v = _text(root, tag)
        try:
            return float(v) if v else None
        except ValueError:
            return None
    return {
        "persons": persons,
        "jurisdiction": _text(root, "jurisdictionOfInc"),
        "entity_type": _text(root, "entityType"),
        "offering_amount": _num("totalOfferingAmount"),
        "amount_sold": _num("totalAmountSold"),
    }
