"""
SEC EDGAR — quarterly Form 13F: the managers holding an issuer's shares, the
filing deadlines, and a manager's affiliated filers.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
from datetime import datetime, timedelta, timezone
import logging
import xml.etree.ElementTree as ET

import httpx
from app.scraper import sec_13dg_text, sec_13dg_xml, sec_client

log = logging.getLogger(__name__)


def latest_13f_deadline(today):
    """The most recent 13F filing deadline that has already passed.

    Form 13F is due 45 days after each calendar quarter end (Mar 31, Jun 30,
    Sep 30, Dec 31 → May 15, Aug 14, Nov 14, Feb 14/15). Computed, not
    hardcoded, so leap years around the December deadline come out right.
    Inclusive of today: on deadline day the quarter counts as due.
    """
    from datetime import date, timedelta
    deadlines = [date(y, m, d) + timedelta(days=45)
                 for y in (today.year - 1, today.year)
                 for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(d for d in deadlines if d <= today)


def next_13f_deadline(today):
    """The first 13F filing deadline still ahead — when new filings are due.

    The previous year matters: in early January the next deadline is LAST
    year's December quarter (Dec 31 + 45 days = mid-February).
    """
    from datetime import date, timedelta
    deadlines = [date(y, m, d) + timedelta(days=45)
                 for y in (today.year - 1, today.year, today.year + 1)
                 for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return min(d for d in deadlines if d > today)


_AFFILIATE_FORMS = ("13F-NT", "13F-HR")


def fetch_affiliated_managers(cik: str) -> list[dict]:
    """The affiliated managers a filer names on its latest 13F cover page.

    A fund group files one 13F per manager, and the cover page lists the group's
    OTHER managers with their CIKs — ten of them for Vanguard, including the
    entity that took over its 13G reporting. That is an authoritative statement of
    group membership from the filer itself, far better evidence than matching on a
    shared name prefix, and it costs a single document fetch.

    What it does NOT establish is ownership or control: "reports 13F holdings
    alongside" is exactly as much as the form says. Callers should record it as an
    affiliation, not as an OWNS edge.
    """
    try:
        subs = sec_client._submissions(cik)
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        return []

    recent = subs.get("filings", {}).get("recent", {})
    filings = sorted(
        (
            {"form": f, "accession": a, "date": d}
            for f, a, d in zip(recent.get("form", []),
                               recent.get("accessionNumber", []),
                               recent.get("filingDate", []))
            if any(f.startswith(p) for p in _AFFILIATE_FORMS)
        ),
        key=lambda r: r["date"], reverse=True,
    )
    if not filings:
        return []           # not a 13F filer — most companies

    latest = filings[0]
    url = f"{sec_client.ARCHIVES_URL}/{sec_client._cik_int(cik)}/{latest['accession'].replace('-', '')}/primary_doc.xml"
    try:
        root = ET.fromstring(sec_client._get_text(url))
    except Exception as exc:  # noqa: BLE001 - pre-XML or malformed cover page
        log.debug("SEC EDGAR: no parseable 13F cover page for %s: %s", cik, exc)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for el in root.findall(".//{*}otherManager"):
        raw_cik = sec_13dg_xml._xml_field(el, "cik")
        name = sec_13dg_xml._xml_field(el, "name")
        if not name:
            continue
        # Some entries carry a short/unpadded CIK (the Vanguard notice has one at
        # 9 digits); normalise so it matches an EDGAR-sourced node.
        manager_cik = sec_client._cik_int(raw_cik).zfill(10) if raw_cik and raw_cik.strip().isdigit() else None
        key = manager_cik or name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "cik":         manager_cik,
            "name":        name.strip(),
            "source_url":  sec_client._filing_index_url(cik, latest["accession"]),
            "source_date": latest["date"],
            "form_type":   latest["form"],
        })
    log.info("SEC EDGAR: %d affiliated managers for CIK=%s (from %s)",
             len(out), cik, latest["form"])
    return out


# ── Quarterly holdings — Form 13F ────────────────────────────────────────────
#
# Every institutional manager over $100M files a quarterly holdings report whose
# information table is structured XML: issuer name, CUSIP, dollar value, share
# count, voting authority. It is the third beneficial-ownership family this
# module reads, and it fills a structural blind spot: 13D/G only exists above
# the 5% threshold, so an 0.9% Nvidia or a 1.2% PIF position in SpaceX can
# never appear there — but every one of them is in somebody's 13F.
#
# Issuers are identified ONLY by CUSIP and a free-text name. The CUSIP is the
# reliable key; it is harvested from the 13D/G schedules we already parse
# (issuerCusipNumber) and adopted from the first name-verified 13F row, then
# stored on the entity — individual identifiers from public filings, not a
# licensed CUSIP database.

_13F_PAGE = 10          # EFTS returns 10 document hits per page

# One quarter plus the 45-day filing deadline: every filing about the CURRENT
# reporting period fits, and nothing older gets in. Without this, relevance
# ordering happily serves 2022 filings for a widely-held issuer — the Televisa
# smoke run wrote "current" positions with as-of dates spanning four years.
_13F_WINDOW_DAYS = 135

# Graph-known filers per EFTS request when they are asked for by CIK (findings
# item 40). Measured 2026-11-02: 500 CIKs in one `ciks=` list work, 1,200 are a
# 400 (the URL is too long); 250 leaves room for long query terms.
_13F_CIK_BATCH = 250


def _13f_base_params(window_days: int) -> dict:
    params: dict = {"forms": "13F-HR"}
    if window_days:
        start = (datetime.now(timezone.utc) - timedelta(days=window_days)).date()
        params |= {"dateRange": "custom", "startdt": start.isoformat(),
                   "enddt": datetime.now(timezone.utc).date().isoformat()}
    return params


def _13f_entry(hit: dict) -> dict | None:
    """One EFTS hit as {accession, filer_cik, file_date, display_name}."""
    src = hit.get("_source", {})
    accession = (hit.get("_id") or "").split(":")[0]
    ciks = src.get("ciks") or []
    if not accession or not ciks:
        return None
    return {"accession": accession, "filer_cik": ciks[0].lstrip("0") or "0",
            "file_date": src.get("file_date") or "",
            "display_name": (src.get("display_names") or [""])[0]}


def _13f_priority_filings(queries: list[str], window_days: int,
                          priority: list[str]) -> tuple[dict[str, dict], int]:
    """The newest 13F naming any of `queries` for each graph-known filer, asked
    for BY CIK: ({10-digit CIK: entry}, failed requests).

    Relevance ranks a mega-filer's one row among thousands at the tail of every
    query, so the relevance sweep never reaches it, and priority could only
    reorder what that sweep found. Berkshire lost BlackRock, Vanguard, JPMorgan
    and 43 more this way on 2026-11-01: on its class-B CUSIP and its name they
    ranked past the 2,500-hit cap, 3 and 6 of 46 found (findings item 40).
    EFTS filters by filer with `ciks=`, and one request per term found 43-46
    of the 46. A failed batch is logged and counted, never fatal: the relevance
    sweep still stands."""
    base = _13f_base_params(window_days)
    found: dict[str, dict] = {}
    failed = 0
    for query in queries:
        for i in range(0, len(priority), _13F_CIK_BATCH):
            batch = priority[i:i + _13F_CIK_BATCH]
            wanted = set(batch)
            params = {**base, "q": f'"{query}"', "ciks": ",".join(batch)}
            offset = 0
            while True:
                try:
                    data = sec_client._get(sec_client.SEARCH_URL, {**params, "from": offset})
                except httpx.HTTPError as exc:
                    log.warning("SEC EDGAR: 13F search %r by %d CIKs failed (%s)",
                                query, len(batch), exc)
                    failed += 1
                    break
                hits = data.get("hits", {}).get("hits", [])
                qtotal = data.get("hits", {}).get("total", {}).get("value", 0)
                for h in hits:
                    entry = _13f_entry(h)
                    cik = entry and sec_client._cik_int(entry["filer_cik"]).zfill(10)
                    # only the filers asked for: a co-filer on the same
                    # filing is not a graph-known holder
                    if not entry or cik not in wanted:
                        continue
                    best = found.get(cik)
                    if best is None or entry["file_date"] > best["file_date"]:
                        found[cik] = entry
                offset += max(len(hits), _13F_PAGE)
                if not hits or offset >= qtotal:
                    break
    return found, failed


def _13f_filings_for(queries: list[str], limit: int,
                     window_days: int = _13F_WINDOW_DAYS) -> tuple[list[dict], int]:
    """13F filings whose info table mentions ANY of `queries`, newest-per-filer.

    A list because one issuer circulates under several identifiers: SpaceX
    filers report CUSIPs 84615Q103 and 69608A108 side by side, and NVIDIA
    abbreviates the name to "SPACE EXPLORATION TECHN CORP" — a single
    exact-phrase query misses whole classes of filers whichever term it uses.
    That holds for a rarely-held issuer, whose queries all fit under the
    discovery cap below. The cap is shared by the queries and spent by the
    first one for a widely-held issuer — Berkshire's class-A CUSIP alone
    returns 2,466 filings — so the later queries add nothing to the pool and
    the relevance sample of non-known filers comes from the first query; the
    graph-known filers are asked for by CIK per query regardless
    (`_13f_priority_filings`), so no holder is lost to it (findings item 53).

    Full-text search matches the information-table DOCUMENT, so each hit is one
    filing naming the issuer. Hits are relevance-ordered, NOT date-ordered, so
    the search is windowed to the last `window_days` (0 = all time): a stale
    position from years ago is not a current holding, however relevant the
    document. Within the window, for a rarely-held issuer (SpaceX: 91 filings)
    `limit` covers everything; for a mega-cap it is a sample — the caller
    reports fetched-vs-total so nobody mistakes one for the other. Amendments
    (13F-HR/A) restate the whole table, so per filer only the newest accession
    survives.
    """
    base_params = _13f_base_params(window_days)
    seen_acc: set[str] = set()
    by_filer: dict[str, dict] = {}
    total = 0
    # Discovery sweeps WIDER than the fetch limit: EFTS pages carry ~100 hits
    # (documented 10 — measured, as ever), so surveying thousands of matching
    # filings costs a handful of requests. WHO gets fetched is decided later,
    # by priority — relevance ranks a mega-filer's single row near the bottom.
    # 2,500 covers a private-company universe COMPLETELY (SpaceX: 1,823
    # filings — FMR's ranked outside a 1,250 sweep and Fidelity went missing);
    # a mega-cap's 10,000-hit universe stays a prioritised sample either way.
    discover_cap = max(limit * 5, 2500)
    # No early exit between queries when the cap is spent: the loop below then
    # asks a later query for its FIRST page only — the guards take no hit and
    # stop — which reads its total, so `filings_total` is the largest of every
    # query's count and not the first query's alone (findings item 53).
    for query in queries:
      params = {**base_params, "q": f'"{query}"'}
      offset = 0
      while offset < discover_cap:
        try:
            data = sec_client._get(sec_client.SEARCH_URL, {**params, "from": offset})
        except httpx.HTTPStatusError as exc:
            # EFTS 500s deterministically on deep pagination for some queries
            # (SpaceX's page at from=30 fails every time). A failed LATER page
            # must not sink the holders already read; and with SEVERAL queries
            # a failed FIRST page only kills ITS query — the others still run.
            # "The search is down" is only the verdict when every query's
            # first page failed and nothing at all was found.
            if offset == 0 and len(queries) == 1 and not seen_acc:
                raise
            log.warning("SEC EDGAR: 13F search %r offset %d failed (%s) — "
                        "continuing with %d filings", query, offset, exc,
                        len(seen_acc))
            break
        hits = data.get("hits", {}).get("hits", [])
        qtotal = data.get("hits", {}).get("total", {}).get("value", 0)
        total = max(total, qtotal)
        if not hits:
            break
        for h in hits:
            if len(seen_acc) >= discover_cap:
                break
            entry = _13f_entry(h)
            if not entry or entry["accession"] in seen_acc:
                continue
            seen_acc.add(entry["accession"])
            best = by_filer.get(entry["filer_cik"])
            if best is None or entry["file_date"] > best["file_date"]:
                by_filer[entry["filer_cik"]] = entry
        offset += max(len(hits), _13F_PAGE)
        if offset >= qtotal or len(seen_acc) >= discover_cap:
            break
    return list(by_filer.values()), total


def _13f_documents(filer_cik: str, accession: str) -> tuple[str | None, str | None]:
    """(info table XML, primary_doc XML) for one 13F accession, either None on miss.

    The info table's file NAME varies by filing agent (informationtable.xml,
    infotable.xml, form13fInfoTable.xml…), so it is found by shape — an XML that
    is not the primary document — rather than by name.
    """
    nodash = accession.replace("-", "")
    base = f"{sec_client.ARCHIVES_URL}/{sec_client._cik_int(filer_cik)}/{nodash}"
    try:
        index = sec_client._get(f"{base}/index.json")
    except httpx.HTTPError:
        return None, None
    names = [i.get("name", "") for i in index.get("directory", {}).get("item", [])]
    info_name = next((n for n in names
                      if n.lower().endswith(".xml") and "primary" not in n.lower()), None)
    info = primary = None
    try:
        if info_name:
            info = sec_client._get_text(f"{base}/{info_name}")
        if "primary_doc.xml" in names:
            primary = sec_client._get_text(f"{base}/primary_doc.xml")
    except httpx.HTTPError:
        pass
    return info, primary


def _valid_cusip(raw: str | None) -> str | None:
    """A CUSIP is EXACTLY nine alphanumerics. MercadoLibre's stored identifier
    was a ten-character typo from some filer's table (0 filings on EDGAR use
    it; the real nine-character one appears in 10,000) — malformed values are
    dropped at the row, like normalize_lei does for LEIs."""
    c = (raw or "").strip().upper()
    return c if len(c) == 9 and c.isalnum() else None


def _13f_rows(info_xml: str) -> list[dict]:
    """The information table as dicts; share rows only (PRN = debt principal)."""
    try:
        root = ET.fromstring(info_xml)
    except ET.ParseError:
        return []
    rows = []
    for t in root.findall(".//{*}infoTable"):
        kind = (sec_13dg_xml._xml_child(t, "sshPrnamtType") or "SH").upper()
        if kind != "SH":
            continue
        # An option row reports the shares UNDERLYING the contract, not shares
        # held — BNP's SpaceX table carries "Option"/"Equity Option" rows worth
        # hundreds of millions that own nothing. putCall marks them.
        if sec_13dg_xml._xml_child(t, "putCall"):
            continue
        rows.append({
            "issuer":  sec_13dg_xml._xml_child(t, "nameOfIssuer"),
            "class":   sec_13dg_xml._xml_child(t, "titleOfClass"),
            "cusip":   _valid_cusip(sec_13dg_xml._xml_child(t, "cusip")),
            "value":   sec_13dg_xml._xml_num(t, "value"),
            "shares":  sec_13dg_xml._xml_num(t, "sshPrnamt"),
        })
    return rows


_GENERIC_NAME_TOKENS = {"technologies", "technology", "techn", "tech", "corp",
                        "corporation", "inc", "incorporated", "company", "co",
                        "holdings", "group", "international", "global", "ltd",
                        "limited", "the"}


def _13f_issuer_matches(names: list[str], candidate: str | None) -> bool:
    """The row-acceptance check for 13F info tables — stricter than
    `_issuer_matches`, because these rows come from OTHER issuers' tables in
    the same filing: the tolerant check accepted "AGILENT TECHNOLOGIES INC"
    for SpaceX (similarity 0.64, carried entirely by "Technologies"), which
    both mis-attributed Agilent rows as SpaceX holdings and poisoned the
    CUSIP union with Agilent's identifier. On top of the similarity, the
    candidate's first DISTINCTIVE token (skipping generic filler like
    Technologies/Corp/Inc) must appear among the company's own name tokens —
    "SPACE" anchors SpaceX; "AGILENT" anchors nothing of ours."""
    if not candidate or not sec_13dg_text._issuer_matches(names, candidate):
        return False
    own_tokens = {t for n in names for t in re.split(r"[^a-z0-9]+", n.lower()) if t}
    def known(tok: str) -> bool:
        return tok in own_tokens or any(o.startswith(tok) or tok.startswith(o)
                                        for o in own_tokens if len(o) > 3 and len(tok) > 3)
    distinctive = [t for t in re.split(r"[^a-z0-9]+", candidate.lower())
                   if t and t not in _GENERIC_NAME_TOKENS]
    if not distinctive:
        return True   # candidate is ALL generic tokens — fall back to similarity
    # TWO conditions, both learned from real poisonings:
    # - the FIRST distinctive token must be ours: issuer names lead with the
    #   brand, so "MDA SPACE" / "SIDUS SPACE" / "PALANTIR TECHNOLOGIES" fail
    #   here however many later tokens coincide;
    # - a MAJORITY of distinctive tokens must be ours: one anchored word is
    #   not identity ("SPACE CAMP HOLIDAYS" leads with space and still
    #   is not SpaceX). An abbreviation passes both ("SPACE EXPLORATION
    #   TECHN" → leads with space, 2 of 2 known).
    if not known(distinctive[0]):
        return False
    hits = sum(1 for t in distinctive if known(t))
    return hits * 2 >= len(distinctive)


def fetch_13f_holders(company_name: str, known_names: list | None = None,
                      cusips: list[str] | None = None, limit: int = 100,
                      window_days: int = _13F_WINDOW_DAYS,
                      priority_ciks: set[str] | None = None) -> dict:
    """Institutional holders of one issuer, from Form 13F information tables.

    Matching ladder: a known CUSIP matches rows exactly; without one, rows are
    verified by `_issuer_matches` against the company's names — the same
    tolerant, diacritic-folded check the 13D/G path uses, because the free-text
    `nameOfIssuer` is typed by each filing agent ("APPLE INC" / "Apple Inc") —
    and the first verified row's CUSIP is returned in `cusip_seen` for the
    caller to stamp on the entity, making the next run exact.

    A filer may hold the issuer in several rows (share classes, lending
    programs): rows aggregate per (filer, share class), shares and value
    summed. Values are dollars (the SEC dropped the report-in-thousands rule in
    2023). Percentages are deliberately NOT computed here — the caller divides
    shares by shares outstanding, so every percent in the graph is arithmetic
    on counts rather than a transcribed number.
    """
    names = known_names or [company_name]
    # The NAME is always among the queries: it is how brand-new CUSIPs are
    # discovered (a filer using a third identifier still spells some name).
    queries = [*dict.fromkeys([*(cusips or []), company_name])]
    discovered, total = _13f_filings_for(queries, limit, window_days)
    # WHO gets fetched: every filer the graph already knows, whatever its
    # relevance rank — a mega-filer's single SpaceX row among a thousand
    # positions ranks near the bottom of 5,000 hits, which is exactly how
    # NVIDIA and Alphabet stayed invisible at any sane fetch limit. Known
    # filers are asked for by CIK as well (findings item 40): reordering only
    # what the relevance sweep found lost Berkshire's 46 biggest holders. All
    # of them are fetched; `limit` more fill in relevance order.
    prio = sorted({sec_client._cik_int(c).zfill(10) for c in (priority_ciks or set()) if c})
    by_cik = {sec_client._cik_int(f["filer_cik"]).zfill(10): f for f in discovered}
    by_prio, prio_failed = (_13f_priority_filings(queries, window_days, prio)
                            if prio else ({}, 0))
    for cik, entry in by_prio.items():
        held = by_cik.get(cik)
        if held is None or entry["file_date"] > held["file_date"]:
            by_cik[cik] = entry
    known = set(prio)
    priority_filings = [f for cik, f in by_cik.items() if cik in known]
    others = [f for f in discovered if sec_client._cik_int(f["filer_cik"]).zfill(10) not in known]
    filings = [*priority_filings, *others[:limit]]
    known_cusips = set(cusips or [])
    holders: list[dict] = []
    period_seen = None
    # One issuer legitimately has SEVERAL CUSIPs — SpaceX's filers report
    # 84615Q103 (Class A) and 69608A108 side by side — so a name-matching run
    # must NOT lock onto the first CUSIP it sees or every filer holding another
    # class is silently skipped. With no stored CUSIP the whole run matches by
    # name; the most-reported CUSIP is returned for stamping.
    cusip_counts: dict[str, int] = {}
    cusip_filers: dict[str, set] = {}
    for f in filings:
        info, primary = _13f_documents(f["filer_cik"], f["accession"])
        if not info:
            continue
        period = None
        manager = None
        if primary:
            try:
                proot = ET.fromstring(primary)
                period = sec_client._iso_date(sec_13dg_xml._xml_child(proot, "periodOfReport"))
                manager = sec_13dg_xml._xml_child(proot, "name")
            except ET.ParseError:
                pass
        if not manager:
            # "Gigafund Management Company, LLC  (CIK 0001713833)" → the name
            manager = re.sub(r"\s*\(CIK[^)]*\)\s*$", "", f["display_name"]).strip()
        by_class: dict[str, dict] = {}
        for row in _13f_rows(info):
            # A row belongs to the issuer if its CUSIP is one we know OR its
            # free-text name matches — the name path stays active even with
            # stored CUSIPs, because that is how the NEXT cusip is learned.
            if not ((row["cusip"] and row["cusip"] in known_cusips)
                    or _13f_issuer_matches(names, row["issuer"])):
                continue
            if row["cusip"]:
                cusip_counts[row["cusip"]] = cusip_counts.get(row["cusip"], 0) + 1
                cusip_filers.setdefault(row["cusip"], set()).add(f["filer_cik"])
            agg = by_class.setdefault(row["class"] or "", {"shares": 0, "value": 0})
            agg["shares"] += row["shares"] or 0
            agg["value"] += row["value"] or 0
        for share_class, agg in by_class.items():
            if not agg["shares"]:
                continue
            holders.append({
                "filer_cik":   f["filer_cik"],
                "filer_name":  manager,
                "shares":      agg["shares"],
                "value_usd":   agg["value"] or None,
                "share_class": share_class or None,
                "period":      period or f["file_date"],
                "source_url":  sec_client._filing_index_url(f["filer_cik"], f["accession"]),
            })
            period_seen = period_seen or period
    # A cusip is believed only when INDEPENDENT filers corroborate it: three
    # distinct managers labelling the same security with the company's name.
    # One filer's typo (a mislabelled Zebra row) must not join the union —
    # and unanimity of one is how Palantir's cusip got stamped onto SpaceX.
    corroborated = {c for c, filers in cusip_filers.items() if len(filers) >= 3}
    corroborated |= set(cusips or [])          # already-stored ones stay
    cusip_seen = next(iter(sorted(
        (c for c in cusip_counts if c in corroborated),
        key=lambda c: -cusip_counts[c])), (cusips[0] if cusips else None))
    log.info("SEC EDGAR 13F: %d holders of %r from %d filings (%d total on EDGAR; "
             "%d of %d known filers found by CIK)", len(holders), company_name,
             len(filings), total, len(priority_filings), len(prio))
    return {"cusip_seen": cusip_seen,
            "cusips_seen": sorted(c for c in cusip_counts if c in corroborated),
            "period": period_seen,
            "filings_total": total, "filings_fetched": len(filings),
            # the selection, for the run log (findings item 40)
            "priority_known": len(prio), "priority_found": len(priority_filings),
            "priority_failed": prio_failed,
            "holders": holders}
