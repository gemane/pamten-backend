"""
SEC EDGAR — shares outstanding, the denominator of a stake: the XBRL cover
fact, per class where the cover gives classes, and how old it may be.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

import httpx
from app.scraper import sec_client

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SharesOutstanding:
    """An issuer's stated share count: dated, and per class where it has more
    than one. `by_class` is keyed by the class letter ("A", "B") and stays
    empty for a single unclassed count; `total` sums the classes — which is
    what a 13F holding of an unstated class is measured against, and what
    a Form 4 holding never is: a count of one class is divided only by the
    count of that class, or not at all (`stake_for`)."""
    total: float
    as_of: str | None
    by_class: dict[str, float]


#: A plain-tag series whose last value is older than this, counted back from
#: the day the stake is computed, is one the issuer stopped keeping, not its
#: current count. Berkshire's ended in April 2011, when it began reporting
#: per class with a dimension (which the aggregated endpoints drop); its last
#: value, 941,481 Class A shares, made Warren Buffett's Class B count a stake
#: of 1,274 %. Fifteen months lets an annual filer be a quarter late.
DENOMINATOR_MAX_AGE_DAYS = 15 * 30


def _as_detail(x: "SharesOutstanding | dict | float | int | None") -> "SharesOutstanding | None":
    """Callers hand over the dataclass, its dict (as stored in a scrape
    result) or a bare total (the 13F path, older callers): one shape."""
    if x is None:
        return None
    if isinstance(x, SharesOutstanding):
        return x
    if isinstance(x, dict):
        return SharesOutstanding(float(x["total"]), x.get("as_of"), dict(x.get("by_class") or {}))
    return SharesOutstanding(float(x), None, {})


def fetch_shares_outstanding(cik: str, on: str | None = None) -> float | None:
    """The issuer's total count (`fetch_shares_outstanding_detail`), for the
    callers that measure an unclassed holding — the 13F path."""
    d = fetch_shares_outstanding_detail(cik, on)
    return d.total if d else None


def fetch_shares_outstanding_detail(cik: str, on: str | None = None) -> "SharesOutstanding | None":
    """
    Latest reported common shares outstanding for an issuer (SEC XBRL facts),
    used to turn a share count into a stake percentage — dated, per class
    where the issuer reports classes. Best-effort — None if unavailable.

    A plain-tag value older than DENOMINATOR_MAX_AGE_DAYS before `on` (today
    when unset) is not the current count: the cover is read instead.

    Two layers. The concept API serves single-class issuers; a MULTI-CLASS
    issuer reports one cover-page fact per class, and dimensioned facts never
    reach the aggregated endpoints — SpaceX's 13.18bn total (7.70bn Class A +
    5.49bn Class B, stated to the share on its 10-Q cover) is simply absent
    there, which is why its holders had counts but no percentages while every
    news article computed them. The fallback reads the inline-XBRL cover of the
    newest 10-Q/10-K and sums the per-class facts.

    Restricted to 10-Q/10-K deliberately: a foreign private issuer files 20-F,
    and its cover counts UNDERLYING shares while 13F filers report ADRs/GDRs —
    Televisa's GDR bundles 585 shares, so that division is wrong by 585x. No
    denominator beats a confidently wrong one.
    """
    for concept in ("dei/EntityCommonStockSharesOutstanding",
                    "us-gaap/CommonStockSharesOutstanding"):
        try:
            data = sec_client._get(f"{sec_client.COMPANYCONCEPT_URL}/CIK{cik}/{concept}.json")
        except httpx.HTTPError:
            continue
        units = data.get("units") or {}
        vals = units.get("shares") or (next(iter(units.values()), []) if units else [])
        dated = [(v.get("end") or "", v.get("val")) for v in vals if v.get("val")]
        if dated:
            dated.sort()                      # most recent reporting period last
            end, val = dated[-1]
            if end < _freshness_floor(on):
                log.info("SEC EDGAR: %s for CIK %s ends %s — stopped, not current; reading the cover",
                         concept, cik, end)
                continue
            try:
                return SharesOutstanding(float(val), end or None, {})
            except (TypeError, ValueError):
                continue
    return _shares_outstanding_from_cover(cik)


def _freshness_floor(on: str | None) -> str:
    """The oldest reporting date still taken as current, as YYYY-MM-DD."""
    try:
        ref = datetime.strptime(on[:10], "%Y-%m-%d") if on else datetime.now(timezone.utc).replace(tzinfo=None)
    except ValueError:
        ref = datetime.now(timezone.utc).replace(tzinfo=None)
    return (ref - timedelta(days=DENOMINATOR_MAX_AGE_DAYS)).strftime("%Y-%m-%d")


def _shares_outstanding_from_cover(cik: str) -> "SharesOutstanding | None":
    """The per-class share counts from the newest 10-Q/10-K inline-XBRL cover,
    each class told from its XBRL context (`us-gaap:CommonClassAMember`), dated
    by the context's instant. One class: an unclassed count."""
    try:
        subs = sec_client._submissions(cik)
    except httpx.HTTPError:
        return None
    rec = (subs.get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    idx = next((i for i, f in enumerate(forms) if f in ("10-Q", "10-K")), None)
    if idx is None:
        return None
    acc = rec["accessionNumber"][idx].replace("-", "")
    doc = (rec.get("primaryDocument") or [""])[idx]
    if not doc:
        return None
    try:
        html = sec_client._get_text(f"{sec_client.ARCHIVES_URL}/{sec_client._cik_int(cik)}/{acc}/{doc}")
    except httpx.HTTPError:
        return None
    return cover_counts(cover_facts(html), html)


def cover_facts(html: str) -> dict[str, float]:
    """The cover's share counts, contextRef → count. Pure, for the tests.

    One ix:nonFraction fact per share class. Keyed by contextRef, which is
    what distinguishes the classes — and, being a dict, also what keeps a
    fact echoed elsewhere in the document from double-counting.

    **The displayed figure times 10^`scale`.** Toast's 10-Q cover shows "514"
    and "64" with `scale="6"` — 514 million Class A, 64 million Class B. Read
    as written, a director's 3,566 shares became 693.77 % of Class A; the
    database refused the edge and the whole Toast scrape failed (both
    rebuilds, 2026-10-26 and -27)."""
    seen: dict[str, float] = {}
    for tag in re.findall(r"<ix:nonFraction[^>]*>", html):
        if 'name="dei:EntityCommonStockSharesOutstanding"' not in tag:
            continue
        ctx = re.search(r'contextRef="([^"]+)"', tag)
        if not ctx:
            continue
        m = re.search(re.escape(tag) + r"([\d,]+(?:\.\d+)?)<", html)
        if not m:
            continue
        scale = re.search(r'\bscale="(-?\d+)"', tag)
        try:
            seen[ctx.group(1)] = float(m.group(1).replace(",", "")) * 10 ** int(scale.group(1) if scale else 0)
        except ValueError:
            continue
    return seen


def cover_counts(facts: dict[str, float], html: str) -> "SharesOutstanding | None":
    """`SharesOutstanding` from a cover's facts (contextRef → count) and the
    document the contexts are defined in. Pure, for the tests."""
    if not facts:
        return None
    by_class: dict[str, float] = {}
    as_of: str | None = None
    for ctx, val in facts.items():
        block = re.search(r'<(?:xbrli:)?context id="%s"[^>]*>(.*?)</(?:xbrli:)?context>' % re.escape(ctx), html, re.S)
        body = block.group(1) if block else ""
        member = re.search(r"ClassOfStockAxis\"[^>]*>\s*[\w.-]+:(\w+)Member", body)
        letter = re.search(r"Class([A-Z])(?![a-z])", member.group(1)) if member else None   # CommonClassAMember → A
        key = letter.group(1) if letter else ""
        by_class[key] = by_class.get(key, 0.0) + val
        instant = re.search(r"<(?:xbrli:)?instant>(\d{4}-\d{2}-\d{2})", body)
        if instant and (as_of is None or instant.group(1) > as_of):
            as_of = instant.group(1)
    total = sum(by_class.values())
    if not total:
        return None
    classes = {k: v for k, v in by_class.items() if k} if len(by_class) > 1 else {}
    return SharesOutstanding(total, as_of, classes)
