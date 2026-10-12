"""SEC business combinations — the acquirer's own XBRL facts.

Every material acquisition since 2009 is tagged in the acquirer's 10-K / 10-Q
financial statements as FIELDS: an axis member for the acquired company
(`us-gaap:BusinessAcquisitionAxis` = `goog:WizIncMember`), the consideration
paid (`us-gaap:BusinessCombinationConsiderationTransferred1`) and — only as
the fact's context period, never as a fact of its own — the closing date.
That is a STATED start for an ownership edge, which no other SEC family gives
(Exhibit 21 lists what is held at year-end; 13D/G date a threshold crossing),
and it names companies the filer's Exhibit 21 omits as insignificant
(Alphabet's lists three subsidiaries; Wiz and Intersect are here).

What the instance does NOT carry, learned on Alphabet's filings:
- no identifier for the target (no CIK, no LEI) and no percentage — a business
  combination means control, so the edge is `controlling` with no stake;
- the target's name lives in the LABEL linkbase ("Wiz Inc [Member]"); the
  member's QName is a fallback, and it is NOT stable across filings
  (`goog:IntersectMember` in the 10-K, `goog:IntersectAcquisitionMember` in
  the next 10-Q) — the runner dedupes by normalised name, not by QName;
- announced and closed are different CONCEPTS: the FY2025 10-K tags Wiz with
  `PaymentsToAcquireBusinessesGross` (the agreed 32 bn) in a March-2025 month
  and Intersect with `BusinessCombinationPriceOfAcquisitionExpected` — pending
  deals, never written. Only a consideration-TRANSFERRED fact in a one-day
  context is a closing; the same fact in a longer period is written undated;
- a context carrying a second axis (the intangible-asset classes under the
  deal) is a breakdown, not the deal — single-axis contexts only;
- dimensional facts never reach the aggregated XBRL APIs (companyfacts,
  frames): the instance document itself is the only place to read them.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from app.scraper.sec_edgar import (ARCHIVES_URL, SUBMISSIONS_URL, _cik10, _cik_int,
                                   _get, _get_text, _iso_date)

log = logging.getLogger(__name__)

#: the forms whose financial statements carry business-combination notes
FORMS = ("10-K", "10-Q")
#: filings read per run, newest first — ~2 years of quarterly reporting
MAX_FILINGS = 8
#: the edge/claim `filing_type` per form: the form that carried the fact
FILING_TYPE = {"10-K": "10-K XBRL", "10-Q": "10-Q XBRL"}
#: the dimension that names one acquired business (local part of the QName)
AXIS = "BusinessAcquisitionAxis"
#: a closing, in order of preference: the plain consideration first, then the
#: variant that includes an equity interest held before the combination
CONSIDERATION_CONCEPTS = (
    "BusinessCombinationConsiderationTransferred1",
    "BusinessCombinationConsiderationTransferredIncludingEquityInterestInAcquireeHeldPriorToCombination",
)
#: a member that stands for several deals at once, not one company
AGGREGATE_LABEL = re.compile(
    r"\b(other|acquisitions|business combinations|individually (?:im)?material|"
    r"current year|prior year|acquired propert(?:y|ies))\b", re.I)
#: what a filer buys when it buys no company: assets, a business line, a
#: practice. Only decisive when the name carries no legal form — "Jackson
#: Labs Technologies, LLC" is a company, "Industrial Technologies" is not.
BUSINESS_LINE = re.compile(
    r"\b(fiber|technolog(?:y|ies)|practices?|propert(?:y|ies)|assets?|pipelines?|stores?|"
    r"branches|operations|portfolio|business(?:es)?|segment|division|plants?|facilit(?:y|ies)|"
    r"wells|acreage|rights|network)\b", re.I)
#: the legal forms a company name ends in or carries — counted, not stripped
LEGAL_FORM = re.compile(
    r"\b(inc\.?|incorporated|llc|l\.l\.c\.|corp\.?|corporation|ltd\.?|limited|l\.?p\.?|plc|"
    r"co\.?|company|gmbh|s\.?a\.?|ag|n\.?v\.?|b\.?v\.?|holdings?|group|bank|trust|partners)\b", re.I)
#: two companies in one member ("Alpine Lumber Company And OC Cluss Lumber Company")
_BASKET_JOIN = re.compile(r"\s+and\s+", re.I)
#: a deal word filers append to the company's name ("Intersect Acquisition")
DEAL_SUFFIX = re.compile(r"\s+(acquisition|merger|transaction|business combination)$", re.I)
_MEMBER_SUFFIX = re.compile(r"\s*\[\s*member\s*\]\s*$", re.I)
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_XLINK = "{http://www.w3.org/1999/xlink}"


def _local(tag: str | None) -> str:
    """The local part of a Clark-notation or prefixed name."""
    if not tag:
        return ""
    tag = tag.rsplit("}", 1)[-1]
    return tag.rsplit(":", 1)[-1]


def recent_filings(cik: str, max_filings: int = MAX_FILINGS) -> list[dict]:
    """The filer's newest 10-K / 10-Q filings, newest first, from the inline
    submissions list: `{form, accession, filing_date, report_date}`. A 404 is
    "no such filer" → []. Older submission pages are not followed — two years
    of acquisitions is what the present needs."""
    try:
        subs = _get(f"{SUBMISSIONS_URL}/CIK{_cik10(cik)}.json")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise
    recent = (subs.get("filings") or {}).get("recent") or {}
    rows = zip(recent.get("form") or [], recent.get("accessionNumber") or [],
               recent.get("filingDate") or [], recent.get("reportDate") or [])
    out = []
    for form, accession, filed, report in rows:
        if form not in FORMS:
            continue
        out.append({"form": form, "accession": accession,
                    "filing_date": _iso_date(filed), "report_date": _iso_date(report)})
        if len(out) >= max_filings:
            break
    return out


def instance_files(cik: str, accession: str) -> dict | None:
    """The XBRL instance (`*_htm.xml`) and label linkbase (`*_lab.xml`) URLs of
    one filing, from its directory index; None when the filing has no instance
    (a paper-era or non-XBRL filing)."""
    acc = accession.replace("-", "")
    base = f"{ARCHIVES_URL}/{_cik_int(cik)}/{acc}"
    try:
        index = _get(f"{base}/index.json")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    names = [item.get("name") or "" for item in (index.get("directory") or {}).get("item") or []]
    instance = next((n for n in names if n.endswith("_htm.xml")), None)
    if not instance:
        return None
    labels = next((n for n in names if n.endswith("_lab.xml")), None)
    # EDGAR's renderer writes MetaLinks.json beside every inline-XBRL filing,
    # with every element's labels — whichever agent filed it. The separate
    # label linkbase is there for Workiva filings (Alphabet) and absent from
    # Donnelley ones (Microsoft, Berkshire), where it stays inside the zip.
    metalinks = "MetaLinks.json" if "MetaLinks.json" in names else None
    return {"instance": f"{base}/{instance}",
            "metalinks": f"{base}/{metalinks}" if metalinks else None,
            "labels": f"{base}/{labels}" if labels else None}


def parse_instance(xml_text: str, cik: str) -> dict | None:
    """The business combinations one instance states, or None when it is not XML.

    {cik, document_type, period_end,
     acquisitions: [{member, closing | None, value_usd | None, concept}],
     pending_only: [member QNames on the axis with no consideration fact],
     multi_axis_contexts: n}

    A context counts when its identifier is THIS filer's CIK (a co-registrant
    filing's other subjects are not this filer's deals) and its segment names
    exactly one dimension, the acquisition axis. The closing date is the
    context's `instant`, or its `startDate` when start and end are one day;
    a longer period gives no date. The value is read only in USD; a
    consideration stated in another currency is still a closing, undated in
    value. `xsi:nil` facts are not facts.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    cik10 = _cik10(cik)

    units: dict[str, str] = {}
    for unit in root.findall("{*}unit"):
        measure = unit.find(".//{*}measure")
        if unit.get("id") and measure is not None and measure.text:
            units[unit.get("id")] = _local(measure.text.strip())

    contexts: dict[str, dict] = {}
    multi_axis = 0
    for ctx in root.findall("{*}context"):
        ident = ctx.find("{*}entity/{*}identifier")
        if ident is None or (ident.text or "").strip() != cik10:
            continue
        members = [el for el in ctx.iter()
                   if _local(el.tag) in ("explicitMember", "typedMember")]
        on_axis = [el for el in members if _local(el.get("dimension")) == AXIS]
        if not on_axis:
            continue
        if len(members) > 1:
            multi_axis += 1
            continue
        member = (on_axis[0].text or "").strip()
        if not member:
            continue
        period = ctx.find("{*}period")
        instant = period.find("{*}instant") if period is not None else None
        start = period.find("{*}startDate") if period is not None else None
        end = period.find("{*}endDate") if period is not None else None
        closing = None
        if instant is not None and instant.text:
            closing = instant.text.strip()
        elif start is not None and end is not None and start.text and end.text \
                and start.text.strip() == end.text.strip():
            closing = start.text.strip()
        contexts[ctx.get("id") or ""] = {"member": member, "closing": closing}

    best: dict[str, dict] = {}
    cover: dict[str, str | None] = {"EntityCentralIndexKey": None, "DocumentType": None,
                                    "DocumentPeriodEndDate": None}
    for el in root:
        name = _local(el.tag)
        if name in cover and el.get("contextRef") and el.text:
            # the FIRST of each: a combined filing (Eversource with its
            # utilities) states one EntityCentralIndexKey per registrant, the
            # primary first; its contexts all identify that primary
            if cover[name] is None:
                cover[name] = el.text.strip()
            continue
        ctx = contexts.get(el.get("contextRef") or "")
        if ctx is None or el.get(_XSI_NIL) == "true" or name not in CONSIDERATION_CONCEPTS:
            continue
        value = None
        if units.get(el.get("unitRef") or "") == "USD":
            try:
                value = float((el.text or "").strip())
            except ValueError:
                value = None
            # The plausibility bound at the writer: a consideration is a price
            # paid, so zero or negative is a tagging slip (a reversal, a
            # placeholder) — the deal stands, the figure does not.
            if value is not None and value <= 0:
                value = None
        row = {"member": ctx["member"], "closing": ctx["closing"], "value_usd": value,
               "concept": name}
        # the plain consideration beats the variant; a dated fact beats an undated one
        rank = (CONSIDERATION_CONCEPTS.index(name), 0 if ctx["closing"] else 1)
        held = best.get(ctx["member"])
        if held is None or rank < held[0]:
            best[ctx["member"]] = (rank, row)

    named = {c["member"] for c in contexts.values()}
    acquisitions = [row for _, row in best.values()]
    return {"cik": cover["EntityCentralIndexKey"], "document_type": cover["DocumentType"],
            "period_end": cover["DocumentPeriodEndDate"],
            "acquisitions": acquisitions,
            "pending_only": sorted(named - set(best)),
            "multi_axis_contexts": multi_axis}


def parse_labels(xml_text: str) -> dict[str, str]:
    """QName → the filer's label for it, from a label linkbase: the standard
    `label` role, else `terseLabel`. {} when the linkbase is not XML.

    The linkbase links in three hops: a locator (`xlink:href="….xsd#goog_WizIncMember"`)
    → an arc → the label resources, so the concept id (prefix_Local) is read off
    the locator's fragment and turned back into the instance's QName."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    concept_of: dict[str, str] = {}          # locator label → concept id
    arcs: dict[str, list[str]] = {}          # locator label → label resource labels
    texts: dict[str, dict[str, str]] = {}    # label resource label → {role: text}
    for el in root.iter():
        name = _local(el.tag)
        if name == "loc":
            href = el.get(f"{_XLINK}href") or ""
            if "#" in href and el.get(f"{_XLINK}label"):
                concept_of[el.get(f"{_XLINK}label")] = href.split("#", 1)[1]
        elif name == "labelArc":
            src, dst = el.get(f"{_XLINK}from"), el.get(f"{_XLINK}to")
            if src and dst:
                arcs.setdefault(src, []).append(dst)
        elif name == "label":
            lab = el.get(f"{_XLINK}label")
            role = (el.get(f"{_XLINK}role") or "").rsplit("/", 1)[-1]
            if lab and role and el.text and el.text.strip():
                texts.setdefault(lab, {})[role] = re.sub(r"\s+", " ", html.unescape(el.text)).strip()
    out: dict[str, str] = {}
    for loc, concept in concept_of.items():
        for lab in arcs.get(loc, []):
            roles = texts.get(lab) or {}
            text = roles.get("label") or roles.get("terseLabel")
            if text:
                out[concept.replace("_", ":", 1)] = text
                break
    return out


def parse_metalinks(data: dict) -> dict[str, str]:
    """QName → the filer's label for it, from a filing's MetaLinks.json: the
    standard `label` role, else `terseLabel`. {} for anything else.

    `instance.<primary document>.tag.<prefix_Local>.lang.en-us.role` holds the
    labels of every element the filing uses, members included."""
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for inst in (data.get("instance") or {}).values():
        for concept, tag in ((inst or {}).get("tag") or {}).items():
            roles = (((tag or {}).get("lang") or {}).get("en-us") or {}).get("role") or {}
            text = roles.get("label") or roles.get("terseLabel")
            if text and "_" in concept:
                # entities arrive escaped ("H&amp;E Equipment Services Inc")
                out[concept.replace("_", ":", 1)] = re.sub(r"\s+", " ", html.unescape(str(text))).strip()
    return out


def clean_label(label: str | None) -> str | None:
    """A member label as a company name: "[Member]" and a trailing deal word
    dropped ("Intersect Acquisition [Member]" → "Intersect"); None for a member
    that is not one company — several deals at once ("Other Acquisitions",
    "Current Year Acquisitions"), two companies in one member ("Alpine Lumber
    Company And OC Cluss Lumber Company"), or assets and business lines bought
    without a company ("Industrial Technologies", "Fiber-To-The-Home",
    "Gulf Coast NGL Pipelines" — a name with no legal form made of the words
    such purchases are described in). Seen across 144 filers' filings."""
    if not label:
        return None
    name = _MEMBER_SUFFIX.sub("", label).strip()
    if not name or AGGREGATE_LABEL.search(name):
        return None
    name = DEAL_SUFFIX.sub("", name).strip()
    if not name:
        return None
    forms = len(LEGAL_FORM.findall(name))
    if forms >= 2 and _BASKET_JOIN.search(name):
        return None
    # a comma-joined or very long member is a basket too: "Alpine Lumber,
    # O.C. Cluss, Truckee Tahoe, St. George Truss, …" — one company's name
    # has at most one comma (", Inc.") and rarely more than eight words
    if name.count(",") >= 2 or len(name.split()) > 8:
        return None
    if forms == 0 and BUSINESS_LINE.search(name):
        return None
    return name


def fallback_name(qname: str) -> str:
    """"goog:WizIncMember" → "Wiz Inc": the QName's local part, "Member"
    dropped, split at its capitals. For an instance whose label linkbase is
    missing or silent."""
    local = qname.rsplit(":", 1)[-1]
    local = re.sub(r"Member$", "", local)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", local).strip()


def _labels_of(files: dict) -> dict[str, str]:
    """The filing's member labels: MetaLinks.json first (every inline filing
    has one), the label linkbase where the filing ships one, else nothing —
    the caller then falls back to the QName. A 404 on either is "absent"."""
    for key, reader in (("metalinks", lambda url: parse_metalinks(_get(url))),
                        ("labels", lambda url: parse_labels(_get_text(url)))):
        if not files.get(key):
            continue
        try:
            labels = reader(files[key])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            continue
        if labels:
            return labels
    return {}


def fetch_acquisitions(cik: str, max_filings: int = MAX_FILINGS) -> dict:
    """The business combinations one filer's newest 10-K / 10-Q instances state.

    {filings: [{form, accession, filing_date, url,
                acquisitions: [{member, name, closing, value_usd, concept}],
                pending_only, skipped_aggregate}],
     newest_url: the newest instance read (the freshness key) | None,
     no_instance: n, unparseable: n}

    Newest first; the label linkbase is fetched only for a filing that states
    a deal. Every URL read here is an immutable filing document, so the cache
    serves the second run. A 404 on an instance is "not XBRL", a 5xx is EDGAR
    having a moment and is raised.
    """
    out: dict = {"filings": [], "newest_url": None, "no_instance": 0, "unparseable": 0}
    for filing in recent_filings(cik, max_filings):
        files = instance_files(cik, filing["accession"])
        if not files:
            out["no_instance"] += 1
            continue
        try:
            xml_text = _get_text(files["instance"])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                out["no_instance"] += 1
                continue
            raise
        parsed = parse_instance(xml_text, cik)
        if parsed is None:
            out["unparseable"] += 1
            continue
        if out["newest_url"] is None:
            out["newest_url"] = files["instance"]
        labels: dict[str, str] | None = None
        rows, skipped_aggregate = [], 0
        for acq in parsed["acquisitions"]:
            if labels is None:
                labels = _labels_of(files)
            label = labels.get(acq["member"])
            if label:
                name, name_source = clean_label(label), "label"
            else:
                name, name_source = clean_label(fallback_name(acq["member"])), "qname"
            if not name:
                skipped_aggregate += 1
                continue
            rows.append({**acq, "name": name, "label": label, "name_source": name_source})
        out["filings"].append({"form": filing["form"], "accession": filing["accession"],
                               "filing_date": filing["filing_date"], "url": files["instance"],
                               "acquisitions": rows, "pending_only": parsed["pending_only"],
                               "skipped_aggregate": skipped_aggregate})
    return out
