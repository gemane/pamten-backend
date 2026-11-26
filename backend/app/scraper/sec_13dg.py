"""
SEC EDGAR — an issuer's 13D/G statements: who owns it.

The issuer's statement feed, each statement read (structured XML where the
filing has it, the cover page otherwise), the stake's start, and the restating
of older statements against the newest denominator.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
import logging
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET

import httpx
from app import former_names
from app.scraper.edge_schema import READ_FIELD, READ_FORM
from app.scraper.mapper import (derive_ownership_type)
from app.scraper import sec_13dg_text, sec_13dg_xml, sec_client, sec_filer

log = logging.getLogger(__name__)


# ── Ownership filings (SC 13D / SC 13G) ──────────────────────────────────────

OWNERSHIP_FEED_PAGE      = 100   # entries per browse-edgar page (EDGAR's maximum)
OWNERSHIP_FEED_MAX_PAGES = 10    # pages read for one issuer's 13D/G list, at most
# Filings opened for one issuer's 13D/G list, at most — the most one page could
# ever cost before paging. A filer whose own SC 13s go in under a filing
# agent's accession prefix (Berkshire: 100 of 100) looks inbound to the feed
# filter and is only dropped once opened, so paging alone would have opened
# up to a thousand. Paging widens the window; this keeps the bill (item 49).
OWNERSHIP_MAX_OPENED     = 100


def _ownership_feed_entries(company_cik: str):
    """The SC 13D/G filings ABOUT a company, newest first, from EDGAR's browse feed.

    The feed lists the filings where the company is the subject OR the filer, and
    a page holds at most 100. Until 2026-11-04 one page was read and the
    company's own (outbound) filings were dropped after the cut — so a bank or
    asset manager that files SC 13s itself saw its holders crowded out: Bank of
    America's page was 98 outbound to 2 inbound and reached back only nine
    months, JPMorgan 98/2, Goldman and BlackRock 100/0. Worse, the window moved
    with every filing the company made: three outbound filings on 2026-11-03
    pushed Vanguard Group's 13G/A of 2026-01-30 to position 101, and its closed
    holding of Bank of America vanished between two rebuilds (findings item 49).

    A generator, so a page is fetched only when the caller still needs more:
    the next page is asked for while the last one was full, up to
    OWNERSHIP_FEED_MAX_PAGES. A failed first page loses the section, as before;
    a failed later page loses it too (the list is incomplete) but keeps what
    was read.
    """
    ns = {"a": "http://www.w3.org/2005/Atom"}
    # Normalise company CIK to 10-digit zero-padded form for comparisons
    norm_company_cik = company_cik.zfill(10)
    for page in range(OWNERSHIP_FEED_MAX_PAGES):
        try:
            atom_text = sec_client._get_text(sec_client.BROWSE_URL, {
                "action": "getcompany",
                "CIK":    company_cik,
                # "SC", not "SC 13": EDGAR matches this as a PREFIX, and the
                # December-2024 modernization renamed the forms from "SC 13G" to
                # "SCHEDULE 13G". The two sets are disjoint, so "SC 13" quietly
                # stopped returning anything filed after early 2024 — on Apple, the
                # newest hit was 2024-02-14 while the company had filings from 2026.
                # "SC" spans both; the SC TO-*/SC 14* noise it also matches is
                # dropped by the `"13" not in form_type` test below, before any
                # request is spent on it.
                "type":   "SC",
                "dateb":  "",
                "owner":  "include",
                "start":  str(page * OWNERSHIP_FEED_PAGE),
                "count":  sec_client.browse_count(OWNERSHIP_FEED_PAGE),
                "output": "atom",
            })
        except httpx.HTTPError as exc:
            log.error("SEC EDGAR: browse request failed for CIK=%s (page %d): %s",
                      company_cik, page + 1, exc)
            sec_client._section_failed(sec_client.SECTION_OWNERS, exc)
            return

        try:
            root = ET.fromstring(atom_text)
        except ET.ParseError as exc:
            log.error("SEC EDGAR: Atom feed parse error: %s", exc)
            return

        entries = root.findall("a:entry", ns)
        # Parse entries from Atom feed; grab filing-href for each.
        # Pre-filter outbound filings (where this company is the FILER, not the
        # subject) by reading the accession number's embedded filer-CIK directly
        # from the feed — no HTTP request needed.
        for entry in entries:
            cat       = entry.find("a:category", ns)
            form_type = (cat.get("term") or "").strip() if cat is not None else ""
            if "13" not in form_type:
                continue

            content = entry.find("a:content", ns)
            if content is None:
                continue
            href_elem = content.find("a:filing-href", ns)
            date_elem = content.find("a:filing-date", ns)
            acc_elem  = content.find("a:accession-number", ns)
            index_url = (href_elem.text or "").strip() if href_elem is not None else ""
            file_date = (date_elem.text or "").strip() if date_elem is not None else None
            accession = (acc_elem.text  or "").strip() if acc_elem  is not None else ""

            if not index_url:
                continue

            # Skip filings submitted BY this company (outbound) — filer CIK is the
            # first 10 digits of the accession number (after stripping dashes).
            if accession:
                filer_cik = accession.replace("-", "")[:10].zfill(10)
                if filer_cik == norm_company_cik:
                    continue

            yield {
                "index_url": index_url,
                "form_type": form_type,
                "file_date": file_date,
                "accession": accession,      # builds the structured-XML URL
            }
        if len(entries) < OWNERSHIP_FEED_PAGE:
            return          # the last page: the feed has nothing older
    log.info("SEC EDGAR: 13D/G feed for CIK=%s cut at %d pages", company_cik,
             OWNERSHIP_FEED_MAX_PAGES)


@dataclass
class _Statement:
    """What one 13D/G statement says, as far as it was read. Every field starts
    empty: a cover whose document could not be fetched stays as it is, and the
    loop tells "nothing read" from "nothing held"."""
    pct: float | None = None
    voting: float | None = None
    reported: float | None = None  # row 13 as the filer stated it, before the split
    is_individual: bool | None = None
    share_class: str | None = None
    shares: int | None = None
    shares_total: int | None = None
    voting_shares: int | None = None
    event_date: str | None = None
    final: str | bool | None = None  # the filer's last report (Item 5), findings item 35
    group_members: list[dict] = field(default_factory=list)
    # How the numbers below were read: a structured schedule's named XML
    # fields, or an older cover page's numbered rows found in its text
    # (`form`: the labels are the SEC's, only the figure is read). The two
    # paths could not be told apart afterwards (both cite the index page),
    # and the text path is where every misread 13D/G came from.
    read_from: str | None = None
    cover_cusip: str | None = None  # the issuer's CUSIP as the cover states it
    names: list[str] = field(default_factory=list)  # every reporting person of this statement
    passive: bool = False  # co-filers who report the same shares (no bloc)
    voting_pact: bool = False  # co-filers who vote each other's shares (item 103)


def _filer_of(raw: dict, company_cik: str) -> dict | None:
    """Who filed one statement of the feed: {investor_name, investor_cik,
    index_url, primary_url, xml, person}; None when the filing names nobody.
    A structured schedule is read here (its XML names the filer), an older one
    only as far as its index page."""
    xml = None
    if sec_13dg_xml._is_structured(raw["form_type"]) and raw.get("accession"):
        xml = sec_13dg_xml._fetch_13dg_xml(company_cik, raw["accession"])

    if xml:
        # The XML names the filer, so the index page is not needed at all —
        # one request instead of two — and `_filing_index_url` still gives a
        # readable link for provenance without fetching it.
        person = sec_13dg_xml._select_person(xml, xml.get("filer_cik"), sec_filer.fetch_filer_name)
        if person is None:
            return None
        investor_name = person["name"]
        # `_select_person` gave the block the filer's CIK only where the
        # block IS the filer — never the filer's CIK on another party's name
        investor_cik = sec_client._cik_int(person["cik"]).zfill(10) if person.get("cik") else ""

        index_url = sec_client._filing_index_url(company_cik, raw["accession"])
        primary_url = None
    else:
        investor_name, investor_cik, primary_url = sec_13dg_text._fetch_filing_index(raw["index_url"])
        index_url = raw["index_url"]
        if not investor_name or not investor_cik:
            return None
    return {"investor_name": investor_name, "investor_cik": investor_cik, "index_url": index_url,
            "primary_url": primary_url, "xml": xml, "person": person if xml else None}


def _read_structured(inv: dict, company_name: str, company_cik: str, known_names: list[str]) -> "_Statement | None":
    """A structured schedule (the XML filed since December 2024) read from its
    named fields; None when it is about another issuer."""
    st = _Statement()
    xml, person = inv["xml"], inv["person"]
    st.read_from = READ_FIELD
    if not sec_13dg_xml._xml_issuer_matches(xml, company_cik, known_names):
        log.info("SEC EDGAR: dropping filing by %r — its issuer is %r (CIK %s), not %r",
                 inv["investor_name"], xml.get("issuer_name"),
                 xml.get("issuer_cik"), company_name)
        return None
    st.reported = person.get("percent")
    st.cover_cusip = xml.get("issuer_cusip")
    st.pct, st.voting = sec_13dg_xml._stake_from_person(xml, person)
    st.share_class = xml.get("class_title")
    st.event_date = xml.get("event_date")
    st.final = xml.get("final_report")
    rows = {k: person[k] for k in
                     ("sole_voting", "shared_voting",
                      "sole_dispositive", "shared_dispositive")
                     if person.get(k) is not None}
    # alone or in a bloc decides what the shared rows are (see _shares_held)
    alone = not sec_13dg_text._co_filers_form_a_bloc(xml.get("schedule") or "13D",
                                               len(xml.get("persons") or []))
    st.names = [o["name"] for o in xml.get("persons") or [] if o.get("name")]
    st.passive = alone
    st.shares = sec_13dg_xml._shares_held(rows, person.get("aggregate"), in_group=not alone)
    st.shares_total = (sec_13dg_text._shares_outstanding(xml.get("comment_text") or "")
                     or sec_13dg_xml._derive_total(person.get("aggregate"), person.get("percent")))
    st.voting_shares = sec_13dg_xml._shares_voted(person.get("aggregate"), st.voting)
    st.is_individual = sec_13dg_text._individual_by_code(person.get("type_code"))
    st.group_members = [{"name": o["name"], "cik": o["cik"], "source": "xml",
                      # Item 8's code: IN means a human being. Stated by
                      # the filer, so the writer need not guess from the
                      # shape of the name.
                      "type_code": o.get("type_code")}
                     for o in xml["persons"] if o is not person]
    # a group of co-filers is a voting pact only when the statement
    # names an agreement over the votes (findings item 103)
    st.voting_pact = sec_13dg_text._is_voting_pact(xml.get("schedule") or "13D", len(xml.get("persons") or []),
                                    xml.get("raw_text"))
    return st


def _read_cover(inv: dict, company_name: str, company_cik: str, known_names: list[str]) -> "_Statement | None":
    """An older schedule read from its cover page (`form`: the labels are the
    SEC's, only the figure is read); None when it is about another issuer. A
    document that cannot be fetched leaves the statement as far as it was
    read — empty, when the document itself was lost."""
    st = _Statement()
    try:
        text = sec_client._get_text(inv["primary_url"])
        issuer = sec_13dg_text._parse_issuer_from_text(text)
        if not sec_13dg_text._issuer_matches(known_names, issuer):
            log.info(
                "SEC EDGAR: dropping filing by %r — its issuer is %r, not %r",
                inv["investor_name"], issuer, company_name)
            return None
        # Per-reporter fields come from THIS filer's cover page — a
        # joint filing has one per reporting person, and the first
        # page's numbers belong to someone else.
        cover = sec_13dg_text._cover_page_for(text, inv["investor_name"])
        st.read_from = READ_FORM
        st.pct = sec_13dg_text._parse_percent_from_text(cover)
        st.reported = st.pct
        bloc = sec_13dg_text._co_filers_form_a_bloc(inv["form_type"], sec_13dg_text._cover_page_count(text))
        st.names = sec_13dg_text._cover_page_names(text) or [inv["investor_name"]]
        st.passive = not bloc
        st.voting_pact = sec_13dg_text._is_voting_pact(inv["form_type"], sec_13dg_text._cover_page_count(text), text)
        st.pct, st.voting = sec_13dg_text._own_stake_and_voting(cover, st.pct, document=text, in_group=bloc)
        st.is_individual = sec_13dg_text._parse_reporter_type_from_text(cover)
        st.share_class = sec_13dg_text._parse_class_title_from_text(text)
        st.event_date = sec_13dg_text._parse_event_date_from_text(text)
        st.final = sec_13dg_text._parse_final_report_from_text(text)
        st.cover_cusip = sec_13dg_text._parse_cusip_from_text(text)
        aggregate = sec_13dg_text._parse_aggregate_from_text(cover)
        st.shares = sec_13dg_xml._shares_held(sec_13dg_text._parse_power_rows(cover), aggregate, in_group=bloc)
        st.shares_total = sec_13dg_text._shares_outstanding(text)
        st.voting_shares = sec_13dg_xml._shares_voted(aggregate, st.voting)
        if st.voting and inv.get("accession"):
            # A bloc without an XML membership list: the SGML header
            # names the co-filers. Only fetched for the few filings that
            # actually report one — 3 of AB InBev's 40, not all 40.
            st.group_members = sec_13dg_xml._sgml_group_members(company_cik, inv["accession"])
    except httpx.HTTPError:
        pass
    return st


def _holding(inv: dict, st: "_Statement", since: str | None, since_basis: str | None, until: str | None) -> dict:
    """One holder of the issuer, as `fetch_ownership_filings` returns it."""
    return {
        "investor_name":    inv["investor_name"].title(),
        "investor_cik":     inv["investor_cik"],
        "form_type":        inv["form_type"],
        "file_date":        inv["file_date"],
        "period_of_report": None,
        # The issuer's security id from the schedule cover — the key 13F
        # info tables match on, so a 13D/G scrape leaves the company ready
        # for an exact `sec-13f` run.
        "issuer_cusip":     st.cover_cusip,
        "filing_type":      sec_client._short_form(inv["form_type"]),
        # which reader the numbers came through (edge_schema.READ_GRADES)
        "read_from":        st.read_from,
        "stake_percent":    st.pct,
        # The bloc a group member votes within — see _own_stake_and_voting.
        # None for a lone filer, whose stake already is its whole position.
        "voting_power_pct": st.voting,
        "ownership_type":   derive_ownership_type(st.pct, inv["form_type"]),
        # The security this percentage is a percentage OF.
        "share_class":      st.share_class,
        # The counts behind the percentage, so the arithmetic can be seen
        # and redone: a stake is `shares / shares_outstanding`.
        "shares":           st.shares,
        "shares_outstanding": st.shares_total,
        # The day the count and the percentage were as stated (the
        # cover's date of event) — what they are "as of".
        "event_date":       st.event_date,
        # When the holding began, as far as this filing can say: an
        # original schedule states it, an amendment only bounds it.
        "since":            since,
        "since_basis":      since_basis,
        # The bloc's own count. Belongs to the group, repeated by every
        # member — never summed, exactly like voting_power_pct.
        "voting_shares":    st.voting_shares,
        "is_individual":    st.is_individual,   # None = unknown (use name heuristic)
        # Set when a NEWER filing by this investor reported nothing: the
        # position is history, and the writer closes the edge with it.
        "until":            until,
        # The filer's last report — Item 5 ticked, "5 percent or less": the
        # holding stays, dimmed from this day, never closed (findings item 35)
        "final_report":     sec_13dg_text._final_since(st.final, st.event_date, inv["file_date"]),
        # Provenance: the filing index page (readable), with the primary doc
        # as a fallback. file_date is the filing's date.
        "source_url":       inv.get("index_url") or inv.get("primary_url"),
        # Co-filers on this schedule — the filing group. Returned for the
        # caller to use; deliberately NOT written to the graph (see the
        # module docstring), because membership is not ownership.
        "group_members":    st.group_members,
        # whether they form a voting pact — the only joint schedule the
        # issuer side draws as a group (findings item 103)
        "voting_pact":      st.voting_pact,
    }


def fetch_ownership_filings(company_name: str, company_cik: str | None = None,
                            limit: int = 20, entries=None, exits: list | None = None) -> list:
    """
    Find SC 13D/13G large-shareholder filings where this company is the issuer.
    Uses EDGAR's company browse Atom feed (keyed by CIK), which is more reliable
    than full-text search. Each entry represents an investor holding >5%.
    Names and stake percentages are fetched from individual submissions JSONs.

    ``entries``: read these filings instead of the feed — the same rows, newest
    first ({index_url, form_type, file_date, accession}); the whole-graph table
    pass gives the statements filed since an issuer's ownership table
    (`sec_writer.write_statements_since`, findings item 116).
    ``exits``: a list that receives the reporting persons of every statement
    that reports no position — the caller that reads only the newest filings
    never reaches the older holding an exit closes, and must still know who
    left.
    """
    if not company_cik:
        log.warning("SEC EDGAR: CIK required for ownership search; skipping %r", company_name)
        return []

    log.info("SEC EDGAR: fetching SC 13D/13G via browse for CIK=%s (%r)", company_cik, company_name)
    # Read lazily, page by page: the loop below asks for the next page only
    # while it still needs holders (findings item 49).
    raw_entries = iter(entries) if entries is not None else _ownership_feed_entries(company_cik)
    norm_company_cik = company_cik.zfill(10)

    # One pass per filing: fetch, verify, then claim the investor's slot.
    #
    # This used to be two passes, enrichment then verification, with the dedup
    # claim made in the first. That let a filing dropped for naming the wrong
    # issuer still burn its investor's CIK, so a later, correct filing by the
    # same investor was silently skipped. The claim now happens only after the
    # filing has been verified, which requires it to be in the same loop.
    seen_investor_ciks: set[str] = set()
    # The company filing about itself — compared zero-padded, because the
    # investor CIKs below are zfill(10) and an unpadded entry never matched.
    seen_investor_ciks.add(norm_company_cik)
    # investor CIK → the date they reported holding nothing. `fetch_filer_holdings`
    # has always read a zero this way; this side never did, so an exit was written
    # as a live "owns 0.0%" edge instead of closing the position. The Vanguard
    # Group's January-2026 realignment put eighteen of those in the graph at once:
    # its 13G/As report 0 for every power row because the holdings moved to
    # subsidiaries that now file separately. Newest zero wins (feed order).
    closed_since: dict[str, str] = {}

    # One statement, newest wins (findings item 93). A passive schedule names
    # every reporting person of the SAME shares — Berkshire's 13G/As on Sirius
    # XM list Warren E. Buffett, Berkshire, National Indemnity, GEICO … each
    # with the whole 124.8 M shares — and EDGAR files it under whichever of
    # them the agent picked: the Feb-2025 amendment under Buffett's CIK, the
    # Nov-2025 one under Berkshire's. Deduplicating by the filer's CIK alone
    # read the older one as a second holder (Buffett 34.6 % beside Berkshire
    # 37.1 %, 72 % of the company). The persons carry no CIK on a 13G, so the
    # match is by name. Only non-bloc filings feed it: in a 13D group every
    # member reports shares of its own, and an older amendment under another
    # member's CIK is that member's stake, not a repetition.
    stated_by: list[str] = []
    # The same for an exit: the reporting persons of a newer passive schedule
    # that reported nothing, with its date, so an older holding filed under
    # another member's CIK is closed by it rather than left live.
    group_closed: list[tuple[list[str], str]] = []

    # Every name this CIK has filed under: renames keep the CIK, and covers
    # from before a rename carry the old name. Fetched at most once, and only
    # when a filing actually needs verifying.
    known_names: list[str] | None = None

    results: list[dict] = []
    opened = 0
    # Checked BEFORE the next entry is pulled: pulling it may fetch the feed's
    # next page, which is wasted once enough holders are in.
    while len(results) < limit and opened < OWNERSHIP_MAX_OPENED:
        raw = next(raw_entries, None)
        if raw is None:
            break
        opened += 1

        inv = _filer_of(raw, company_cik)
        if inv is None:
            continue
        investor_name, investor_cik = inv["investor_name"], inv["investor_cik"]

        if not investor_cik or investor_cik in seen_investor_ciks:
            continue
        if sec_13dg_text._named_in(investor_name, stated_by):
            log.info("SEC EDGAR: %r's %s filing repeats a newer statement it is a "
                     "reporting person of — skipped (findings item 93)",
                     investor_name, raw["file_date"])
            continue

        if known_names is None:
            known_names = [company_name] + former_names.names(sec_filer.fetch_former_names(company_cik))

        inv.update(form_type=raw["form_type"], file_date=raw["file_date"], accession=raw.get("accession"))

        # How the numbers were read — a structured schedule's named XML fields, or
        # an older cover page's numbered rows found in its text — is the reader's
        # to say (`read_from`): the two paths could not be told apart afterwards
        # (both cite the index page), and the text path is where every misread
        # 13D/G came from.
        if inv.get("xml"):
            st = _read_structured(inv, company_name, company_cik, known_names)
        elif inv.get("primary_url"):
            st = _read_cover(inv, company_name, company_cik, known_names)
        else:
            # Neither a parsed XML nor a cover page to check: admitting this
            # would be an unverified filing, which is how the Eve Holding rows
            # got in. A modern index page offers no `SC 13`-typed .htm at all,
            # so this is exactly the case a widened feed would otherwise slip
            # through unverified.
            log.info("SEC EDGAR: dropping unverifiable filing by %r (no XML, no cover page)",
                     inv["investor_name"])
            continue

        if st is None:
            continue

        if st.reported is None and st.pct is None and not st.voting:
            # Nothing READ — not "nothing held". This used to count as an exit
            # and closed a live holding whenever a cover defeated the parser.
            # The investor is not marked seen, so its older, readable filing
            # is used instead: possibly out of date, never a false exit.
            log.info("SEC EDGAR: %r's %s cover gave no percentage — skipped, not an exit",
                     inv["investor_name"], inv["file_date"])
            continue

        if not st.pct and not st.voting:
            # Nothing held and no bloc voted: an exit, not a holding. `voting`
            # is what separates this from a group member who can dispose of
            # nothing alone — BRC reports a null stake beside a real 52.3% bloc
            # and must stay. The CIK is deliberately NOT marked seen, so this
            # investor's older, non-zero filing is still read and emitted below,
            # closed with this date — which is what builds the timeline.
            #
            # The feed runs newest first and every zero NEWER than the holding
            # is read before it, so the last one assigned is the oldest: the
            # day the position actually ended (the cover's date of event when
            # it states one), not a later repetition of the exit.
            closed_since[inv["investor_cik"]] = st.event_date or inv["file_date"]
            if exits is not None:
                exits.extend(st.names or [inv["investor_name"]])
            if st.passive:
                group_closed.append((sec_13dg_text._co_filer_names(st.names, inv["investor_name"]),
                                     st.event_date or inv["file_date"]))
            log.info("SEC EDGAR: %r reported no position on %s — closing, not writing 0%%",
                     inv["investor_name"], inv["file_date"])
            continue

        since, since_basis = sec_13dg_text._stake_start(inv["form_type"], st.event_date, inv["file_date"])

        seen_investor_ciks.add(inv["investor_cik"])
        if st.passive:
            stated_by.extend(sec_13dg_text._co_filer_names(st.names, inv["investor_name"]))
        # A newer exit of the same statement filed under another member's CIK
        # closes this one; the oldest such exit is the last one recorded.
        until = closed_since.get(inv["investor_cik"])
        if until is None:
            until = next((d for n, d in reversed(group_closed)
                          if sec_13dg_text._named_in(inv["investor_name"], n)), None)
        log.info(
            "SEC EDGAR: investor %r (CIK=%s) stake=%s is_individual=%s",
            inv["investor_name"], inv["investor_cik"], st.pct, st.is_individual,
        )
        results.append(_holding(inv, st, since, since_basis, until))

    _restate_against_newest_denominator(results)
    log.info("SEC EDGAR: found %d investors for CIK=%s", len(results), company_cik)
    return results


def _class_key(title: str | None) -> str:
    """The share class a cover's title names, without the wording around it.

    BRC's 2026 schedule on AB InBev covers "Ordinary Shares, without nominal
    value and American Depositary Shares, each of which represents one (1)
    Ordinary Share, without nominal value"; Altria's and Bevco's cover
    "Ordinary Shares, without nominal value". Same shares: the depositary
    receipts are only how some of them trade, and the counts are stated in
    ordinary shares. A different class — "Class A Common Stock" beside
    "Class B" — keeps a different key.
    """
    t = (title or "").lower()
    t = re.split(r"\s+and\s+american\s+depositary", t)[0]
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"(without|no)\s+(nominal|par)\s+value|par\s+value\s+\$?[\d.,]+(\s+per\s+share)?", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


#: A newer total this far from the old one is not the same shares any more —
#: a split or a reverse split makes an old share count meaningless.
_DENOMINATOR_DRIFT = 2.0


def _restate_against_newest_denominator(filings: list[dict]) -> None:
    """Re-divide each holder's count by the newest total its class has.

    A stake is `shares / shares_outstanding`, and only the first of the two is
    the holder's: the second moves with every issue and buy-back. Bevco's last
    13D/A on AB InBev is from 2020 — 102,862,718 shares of 1,730,242,027, "5.9%" —
    and a 13D must be amended for any change of 1% or more, so the count still
    stands; the total does not. The company's newest schedule (BRC, May 2026)
    states 1,972,133,054, which makes Bevco's 102,862,718 5.2%.

    In place, on the filings of ONE issuer as read in one scrape: for each
    share class the newest stated total wins; every older filing with a count
    of its own (not a bloc-only row, not an ended one) is restated against it,
    `denominator_date` saying where the total comes from. A total that moved
    more than `_DENOMINATOR_DRIFT`-fold is a split, not the same shares, and is
    left alone.
    """
    newest: dict[str, tuple[str, int]] = {}
    for f in filings:
        total, date = f.get("shares_outstanding"), f.get("file_date") or ""
        if total and date:
            k = _class_key(f.get("share_class"))
            if k not in newest or date > newest[k][0]:
                newest[k] = (date, total)
    for f in filings:
        f.setdefault("denominator_date", None)
        k = _class_key(f.get("share_class"))
        if k not in newest or f.get("until") or f.get("stake_percent") is None or not f.get("shares"):
            continue
        date, total = newest[k]
        old = f.get("shares_outstanding")
        if (f.get("file_date") or "") >= date or not old or total == old:
            continue
        # Only a count that IS the filed percentage may be divided again: if
        # shares / its own total is not the stake it filed, the count is not
        # the holding (a part of it, another unit) and a new total would
        # turn that mismatch into a confidently wrong stake.
        own = f["shares"] / old * 100
        if abs(own - f["stake_percent"]) > max(0.15, 0.05 * f["stake_percent"]):
            log.info("SEC EDGAR: %r's count is %.4g%% of its total but it filed %s%%; not restating",
                     f.get("investor_name"), own, f["stake_percent"])
            continue
        if max(total, old) / min(total, old) > _DENOMINATOR_DRIFT:
            log.info("SEC EDGAR: %r's total moved %s → %s; not restating across a split",
                     f.get("investor_name"), old, total)
            continue
        pct = sec_13dg_text._pct_of(f["shares"], total)
        log.info("SEC EDGAR: restated %r %s%% → %s%% against the %s total",
                 f.get("investor_name"), f.get("stake_percent"), pct, date)
        f["stake_percent"] = pct
        f["shares_outstanding"] = total
        f["denominator_date"] = date
        f["ownership_type"] = derive_ownership_type(pct, f.get("form_type") or "")
