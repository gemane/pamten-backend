"""
SEC EDGAR — a filer's own 13D/G statements: what it owns of others.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
import html as html_lib
import logging

import httpx
from app import former_names
from app.scraper.edge_schema import READ_FIELD, READ_FORM
from app.scraper import sec_13dg_text, sec_13dg_xml, sec_client, sec_filer

log = logging.getLogger(__name__)


# ── Filer-side holdings: what THIS company owns of others ─────────────────────
#
# The issuer-side readers handle filings where the company is the SUBJECT — who owns it
# (SC 13D/13G) and its insiders (Form 3/4). An asset manager has none of that:
# Vanguard is privately held and isn't a listed issuer, so its node stayed empty
# while its ~3,400 filings — 13D/13G disclosures it makes ABOUT other companies —
# went unread. This reads the filer side, which for an institutional holder is
# the whole point of it being in the graph at all.
#
# Modern 13D/13G filings are structured XML (`primary_doc.xml`, schema X0202)
# carrying the subject's CIK, its name and the percentage as fields, so there is
# no HTML to scrape and one fetch per filing. Older filings predate the schema
# and are skipped rather than guessed at.

HOLDINGS_DEFAULT_LIMIT = 100    # subjects for an explicit holdings run (CLI)
# Default for a normal company scrape. Cheap where it matters: a filer whose
# recent filings are live stakes needs one fetch each, so 25 subjects is ~25
# requests.
HOLDINGS_SCRAPE_LIMIT  = 25
# EDGAR splits a prolific filer's index into pages: `filings.recent` plus an
# archive list. Vanguard's old CIK has 13 pages covering back to 1999, so they are
# read lazily, newest page first, and only while the fetch budget lasts.
HOLDINGS_MAX_ARCHIVE_PAGES = 4

_HOLDING_FORMS = ("SCHEDULE 13G", "SCHEDULE 13D", "SC 13G", "SC 13D")


def _parse_holding_filing(filer_cik: str, accession: str,
                          filer_name: str | None = None) -> dict | None:
    """Subject company + stake from one 13D/13G, or None if it isn't parseable.

    Returns `percent` as a float — **0.0 is meaningful**, not missing: an
    amendment reporting 0% is the filer declaring it has dropped below the 5%
    threshold, i.e. the end of a holding rather than the absence of one.

    Reads through the shared `_parse_13dg_xml`, which knows both schedules.
    Written against 13G tag names alone, this function returned None for every
    Schedule 13D — `issuerCik`/`classPercent` are 13G spellings, and 13D uses
    `issuerCIK`/`percentOfClass`. Since `_HOLDING_FORMS` includes 13D, that was
    a silent hole in what a filer was seen to own.
    """
    xml = sec_13dg_xml._fetch_13dg_xml(filer_cik, accession)
    if not xml or not xml.get("issuer_cik") or not xml.get("issuer_name"):
        return None

    # Read the percent off the filer's own reporting person rather than the
    # document, because the same tag reappears under `items` with a different
    # value — Wellington's Nasdaq 13G/A carries 5.4 on the cover and 5.36 below.
    person = sec_13dg_xml._filer_person(xml, filer_cik, filer_name)
    percent = person.get("percent") if person else None
    # Row 13 is the bloc's figure for a party to a voting agreement: split it
    # into the filer's own stake and the bloc's voting power as the issuer side
    # does (`_stake_from_person`), or BRC's and Altria's 52.3 % and 51.9 % land
    # as their stakes and AB InBev sums to 110 % (findings item 98). Only then:
    # a parent with its subsidiaries keeps row 13 (`_votes_others_shares`).
    stake, voting = percent, None
    if person and percent and sec_13dg_xml._votes_others_shares(person):
        stake, voting = sec_13dg_xml._stake_from_person(xml, person)

    return {
        "subject_cik":  sec_client._cik_int(xml["issuer_cik"]).zfill(10),
        "subject_name": xml["issuer_name"],
        # the subject's security as the cover states it — the securities
        # bridge's input (findings item 71)
        "subject_cusip": xml.get("issuer_cusip"),
        # the security the percentage is a percentage OF, as the issuer side
        # reads it (findings item 157): without it Intel's 100 % of Mobileye's
        # Class B and General Atlantic's 1.4 % of its Class A were added up
        "share_class":  xml.get("class_title"),
        "percent":      percent,          # row 13 as stated: None unread, 0 an exit
        "stake":        stake,            # the filer's own holding
        "voting":       voting,           # the bloc it votes within, None alone
        "accession":    accession,
        "event_date":   xml.get("event_date"),
        "final_report": xml.get("final_report"),
        "read_from":    READ_FIELD,
    }


#: The subject company's block on a filing index page: "Alibaba Group Holding
#: Ltd (Subject)" and, a few tags on, its CIK.
_SUBJECT_ROW = re.compile(r'class="companyName">\s*([^<(]+?)\s*\(Subject\).*?CIK=(\d+)',
                          re.DOTALL | re.IGNORECASE)


def _parse_holding_filing_text(filer_cik: str, accession: str,
                               filer_name: str | None,
                               form_type: str | None = None) -> dict | None:
    """Subject company + stake from a pre-2025 "SC 13D"/"SC 13G", or None.

    Those filings have no `primary_doc.xml` (findings item 44): asking for it
    was a 404 on every old filing — up to 175 per filer and scrape, never
    cached because only answers are — and the stake was never read. Read it
    the way the issuer side reads its old filings instead: the index page
    names the subject company with its CIK and links the cover document, and
    the cover page gives the percentage (`form`: the SEC's numbered rows).

    The filer's index also lists filings ABOUT the filer by others. Those are
    recognised from the index page alone (subject == filer) and cost no
    second request; the caller drops them.

    The index page's subject is the filing agent's say; the cover's "(Name of
    Issuer)" is the filer's. When the two disagree the filing is dropped, as
    the issuer side drops Embraer's Eve Holding 13D/A — the subject's former
    names count as agreement, so a 2016 "Yahoo! Inc." cover still matches
    ALTABA INC.
    """
    index_url = sec_client._filing_index_url(filer_cik, accession)
    try:
        html = sec_client._get_text(index_url)
    except httpx.HTTPError as exc:
        log.debug("SEC EDGAR: index fetch failed %s: %s", index_url, exc)
        return None
    m = _SUBJECT_ROW.search(html)
    if not m:
        return None
    subject_cik  = m.group(2).zfill(10)
    subject_name = html_lib.unescape(m.group(1).strip())
    row = {"subject_cik": subject_cik, "subject_name": subject_name, "percent": None,
           "stake": None, "voting": None, "subject_cusip": None, "share_class": None,
           "accession": accession, "event_date": None,
           "final_report": None, "read_from": READ_FORM}
    if subject_cik == sec_client._cik_int(filer_cik).zfill(10):
        return row                      # inbound: the caller skips it, unread
    doc_url = sec_13dg_text._primary_doc_url(html)
    if not doc_url:
        return None
    try:
        text = sec_client._get_text(doc_url)
    except httpx.HTTPError as exc:
        log.debug("SEC EDGAR: cover fetch failed %s: %s", doc_url, exc)
        return None
    issuer = sec_13dg_text._parse_issuer_from_text(text)
    # The former names are asked only on a mismatch, and an empty list is no
    # agreement (`_issuer_matches` reads "no names" as nothing to contradict).
    if not sec_13dg_text._issuer_matches([subject_name], issuer) and not (
            (former := former_names.names(sec_filer.fetch_former_names(subject_cik)))
            and sec_13dg_text._issuer_matches(former, issuer)):
        log.info("SEC EDGAR: dropping holding %s — its cover names %r, the index %r",
                 accession, issuer, subject_name)
        return None
    cover = sec_13dg_text._cover_page_for(text, filer_name)
    percent = sec_13dg_text._parse_percent_from_text(cover)
    # own stake and bloc, as the issuer side splits an old cover — only for a
    # party to a voting agreement (item 98, see `_votes_others_shares`)
    stake, voting = percent, None
    if percent and sec_13dg_xml._votes_others_shares(sec_13dg_text._parse_power_rows(cover)):
        bloc = sec_13dg_text._co_filers_form_a_bloc(form_type or "13D", sec_13dg_text._cover_page_count(text))
        stake, voting = sec_13dg_text._own_stake_and_voting(cover, percent, document=text, in_group=bloc)
    row.update(percent=percent, stake=stake, voting=voting,
               event_date=sec_13dg_text._parse_event_date_from_text(text),
               final_report=sec_13dg_text._parse_final_report_from_text(text),
               subject_cusip=sec_13dg_text._parse_cusip_from_text(text),
               share_class=sec_13dg_text._parse_class_title_from_text(text))
    return row


def _holding_filings_in(block: dict) -> list[dict]:
    """The 13D/13G rows of one EDGAR filing-index page, newest first."""
    return sorted(
        (
            {"form": f, "accession": a, "date": d}
            for f, a, d in zip(block.get("form", []),
                               block.get("accessionNumber", []),
                               block.get("filingDate", []))
            if any(f.startswith(p) for p in _HOLDING_FORMS)
        ),
        key=lambda r: r["date"], reverse=True,
    )


def _iter_filing_pages(cik: str):
    """Yield a filer's 13D/13G filings page by page, newest first.

    `filings.recent` comes first, then EDGAR's archive pages. A page is only
    fetched when the caller asks for it, so a scrape that finds what it needs in
    recent never pays for the archive — and one that needs to reach back for a
    stake disclosed years ago can.
    """
    try:
        subs = sec_client._submissions(cik)
    except Exception as exc:  # noqa: BLE001 - a filer with no submissions file isn't an error
        # ... but a timeout is not "no submissions file" (findings item 41)
        if sec_client._section_failed(sec_client.SECTION_HOLDINGS, exc):
            log.error("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        else:
            log.warning("SEC EDGAR: submissions fetch failed for CIK=%s: %s", cik, exc)
        return

    yield _holding_filings_in(subs.get("filings", {}).get("recent", {}))

    for page in (subs.get("filings", {}).get("files") or [])[:HOLDINGS_MAX_ARCHIVE_PAGES]:
        name = page.get("name")
        if not name:
            continue
        try:
            yield _holding_filings_in(sec_client._get(f"{sec_client.SUBMISSIONS_URL}/{name}"))
        except Exception as exc:  # noqa: BLE001 - a missing archive page isn't fatal
            log.warning("SEC EDGAR: archive page %s failed: %s", name, exc)
            return


def fetch_filer_holdings(cik: str, limit: int = HOLDINGS_DEFAULT_LIMIT,
                         max_filings: int | None = None) -> list[dict]:
    """Companies this filer discloses a >5% stake in, newest disclosure per company.

    Only OUTBOUND filings. EDGAR's index for a CIK lists filings the company is
    *named in* as well as ones it submitted, so a 13G somebody else filed about it
    appears here too — and reading that filing's issuer gives the company itself
    back. See the subject == filer skip below.

    One row per subject company:
      stake_percent — the last disclosed non-zero percentage
      until         — set when a *newer* amendment reported 0%, i.e. the filer has
                      since dropped below the threshold; the holding is history,
                      not a current position

    That distinction is load-bearing. Vanguard moved its 13G reporting from
    VANGUARD GROUP INC (CIK 0000102909) to VANGUARD CAPITAL MANAGEMENT LLC
    (0002100119) in spring 2026, closing ~1,800 positions with 0% amendments on
    the way out. Keeping only the newest filing per subject would report that the
    old entity owns nothing — true, but useless; recording the last real stake
    with its end date preserves the history the filings actually describe.

    ``max_filings`` bounds the work. The subject company is only knowable by
    fetching the filing — the submissions index doesn't carry it — so filings
    cannot be de-duplicated by subject in advance, and a filer with thousands of
    amendments would otherwise be thousands of requests. Defaults to a multiple of
    ``limit``; raise both to walk further back (the ~1,800 zero amendments at the
    front of the old Vanguard CIK need roughly that many fetches before the last
    real stakes appear).
    """
    if max_filings is None:
        max_filings = limit * 5 + 50

    pages = _iter_filing_pages(cik)
    filings = next(pages, [])
    if not filings:
        return []       # not an institutional filer — the common case, one JSON read

    holdings: list[dict] = []
    closed_since: dict[str, str] = {}   # subject -> date of the newest 0% amendment
    done: set[str] = set()
    fetched = 0
    filer_name: str | None = None       # for a joint old filing's cover page; asked once

    # Newest first, so the first non-zero filing seen for a subject is its most
    # recent real stake, and any 0% already seen for it is the exit that followed.
    # When a page runs out, pull the next archive page — a stake closed years ago
    # is only reachable there.
    while True:
        # Budget first, so a satisfied scrape never pays for an archive page.
        if len(holdings) >= limit or fetched >= max_filings:
            break
        if not filings:
            filings = next(pages, [])
            if not filings:
                break
        filing = filings.pop(0)
        # Each filing in the format it has: a "SCHEDULE 13G" (since Dec 2024)
        # is XML, an "SC 13G" is a text/HTML cover (findings item 44).
        if sec_13dg_xml._is_structured(filing["form"]):
            parsed = _parse_holding_filing(cik, filing["accession"], filer_name)
        else:
            if filer_name is None:
                filer_name = sec_filer.fetch_filer_name(cik) or ""
            parsed = _parse_holding_filing_text(cik, filing["accession"], filer_name,
                                                filing["form"])
        fetched += 1
        if not parsed:
            continue
        sid = parsed["subject_cik"]
        # A filing whose subject IS this filer is an INBOUND one — somebody else's
        # 13D/13G about them — and EDGAR's index carries it because the company is
        # named in it, not because the company submitted it. Reading its issuer
        # gives the company back, so it lands as "X holds 7.48% of X", with the
        # percentage being some third party's stake in X.
        #
        # Nine of those were live in the graph, Apple, Microsoft and Alphabet among
        # them. An issuer does not file 13D/13G about its own stock (buybacks go in
        # a 10-K or 8-K), so subject == filer always means inbound and is always
        # safe to drop.
        if sid == sec_client._cik_int(cik).zfill(10):
            log.debug("SEC EDGAR: skipping inbound filing %s — %s is the subject, "
                      "not the filer", filing["accession"], sid)
            continue
        if sid in done:
            continue

        if parsed["percent"] is None:
            # Unreadable, not "nothing held": skipped, so the holder's older
            # readable filing is used — never a false exit.
            continue
        if not parsed["percent"]:
            # The last zero assigned before the holding is the OLDEST newer
            # than it: the day the position ended, not a later repetition.
            closed_since[sid] = parsed.get("event_date") or filing["date"]
            continue

        done.add(sid)
        holdings.append({
            "subject_cik":   sid,
            "subject_name":  parsed["subject_name"],
            "subject_cusip": parsed.get("subject_cusip"),
            # the class the percentage is of — the panel adds only percentages
            # of one security (findings items 110, 157)
            "share_class":   parsed.get("share_class"),
            # the filer's own holding; a group member's bloc goes to voting
            # (findings item 98: BRC's 52.3 % of AB InBev was written as its stake)
            "stake_percent": parsed.get("stake"),
            "voting_power_pct": parsed.get("voting"),
            "file_date":     filing["date"],
            "event_date":    parsed.get("event_date"),
            "since":         sec_13dg_text._stake_start(filing["form"], parsed.get("event_date"), filing["date"])[0],
            "since_basis":   sec_13dg_text._stake_start(filing["form"], parsed.get("event_date"), filing["date"])[1],
            "form_type":     filing["form"],
            "filing_type":   sec_client._short_form(filing["form"]),
            # `field` from a schedule's XML, `form` from an old cover page.
            # Both writers take the row's word — without it, 681 13D/G edges
            # had no grade after the 2026-10-27 rebuild.
            "read_from":     parsed["read_from"],
            "until":         closed_since.get(sid),
            # the filer's last report: open, but dimmed from this day (item 35)
            "final_report":  sec_13dg_text._final_since(parsed.get("final_report"), parsed.get("event_date"),
                                          filing["date"]),
            "source_url":    sec_client._filing_index_url(cik, filing["accession"]),
        })

    log.info("SEC EDGAR: %d holdings for CIK=%s (%d of %d filings fetched)",
             len(holdings), cik, fetched, len(filings))
    return holdings
