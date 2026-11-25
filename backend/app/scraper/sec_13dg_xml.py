"""
SEC EDGAR — reading a structured 13D/G statement (the XML filed since
December 2024) and its SGML header: the reporting persons, which of them is
the filer, the shares held and voted.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
import html as html_lib
import logging
import xml.etree.ElementTree as ET

from app.scraper import sec_13dg_text, sec_client, sec_filer

log = logging.getLogger(__name__)


def _xml_field(root, tag: str) -> str | None:
    """First value of `tag` at any depth, ignoring the XML namespace.

    Beware the "first": on a multi-person filing the same tag recurs once per
    reporting person AND again inside `items`, with different values. Use
    `_xml_child` against a person's own element when the value belongs to that
    person. Wellington's Nasdaq 13G/A is the case in point — four cover blocks
    reading 5.4 / 5.4 / 5.4 / 5.1 and a fifth value of 5.36 under `items`.
    """
    return _xml_child(root, tag)


def _xml_child(el, tag: str) -> str | None:
    """First value of `tag` within THIS element's subtree, namespace-agnostic."""
    found = el.find(f".//{{*}}{tag}")
    return found.text.strip() if found is not None and found.text else None


def _xml_num(el, tag: str, *, as_int: bool = True):
    """A numeric field, or None when the filing does not state it.

    Absent must not become zero: `_split_stake` reads "no sole dispositive row"
    and "sole dispositive of nothing" as different facts, and conflating them
    invents a 0% stake. Values arrive both as "0" and as "1312500.00", so parse
    through float().
    """
    raw = _xml_child(el, tag)
    if raw is None:
        return None
    try:
        val = float(raw.replace(",", ""))
    except ValueError:
        return None
    return int(val) if as_int else val


#: The two schedules use different tag names for the same facts, and EDGAR
#: publishes them under different namespaces. Layout is chosen by which person
#: container is present, not by the form string or the namespace URI, so a
#: schema bump that keeps the shape keeps working.
_XML_LAYOUTS = (
    # (person container, issuer-cik tag, percent tag, aggregate tag, person-cik tag)
    ("reportingPersonInfo", "issuerCIK", "percentOfClass",
     "aggregateAmountOwned", "reportingPersonCIK"),
    ("coverPageHeaderReportingPersonDetails", "issuerCik", "classPercent",
     "reportingPersonBeneficiallyOwnedAggregateNumberOfShares", None),
)


def _xml_issuer_matches(xml: dict, company_cik: str, known_names: list) -> bool:
    """Is this structured filing about the company being scraped?

    Two tiers, and both must fail before a filing is thrown away. The CIK is
    the strong signal, but it is typed by the filer's agent — the same kind of
    human who filed Embraer's Eve Holding schedule under EMBRAER's name — while
    the reverse also occurs: Wellington's Nasdaq 13G/A carries the correct CIK
    beside the long-superseded name "The NASDAQ OMX Group, Inc.". Rejecting on
    either alone would lose a real owner in one of those two cases.
    """
    issuer_cik = xml.get("issuer_cik")
    if issuer_cik and sec_client._cik_int(issuer_cik) == sec_client._cik_int(company_cik):
        return True
    if issuer_cik and sec_13dg_text._issuer_matches(known_names, xml.get("issuer_name")):
        return True          # right company, stale or reworded name
    return not issuer_cik    # nothing stated is not a mismatch


def _stake_from_person(xml: dict, person: dict) -> tuple:
    """(own stake %, voting-bloc %) for one reporting person of a structured filing.

    The same judgement `_own_stake_and_voting` applies to an HTML cover, fed
    from fields instead of regexes. The denominator is not a tagged value, so
    prefer the one the filer states in the comment and fall back to deriving it
    from the aggregate — derivation is only as precise as `percentOfClass`,
    which is often two significant figures.
    """
    rows = {k: person[k] for k in
            ("sole_voting", "shared_voting", "sole_dispositive", "shared_dispositive")
            if person.get(k) is not None}
    total = sec_13dg_text._shares_outstanding(xml.get("comment_text") or "")
    if not total:
        total = _derive_total(person.get("aggregate"), person.get("percent"))
        if total:
            log.info("SEC EDGAR: derived %s shares outstanding for %r from %s at %s%%",
                     total, person["name"], person.get("aggregate"), person.get("percent"))
    # More than one reporting person on a 13D = a genuine group of co-filers;
    # a lone filer's all-shared power is custodial, and a 13G's co-filers are
    # a family, not a bloc. Test dicts without a schedule read as 13D.
    in_group = sec_13dg_text._co_filers_form_a_bloc(xml.get("schedule") or "13D",
                                      len(xml.get("persons") or []))
    return sec_13dg_text._split_stake(rows, total, person.get("percent"), in_group=in_group)


def _shares_voted(aggregate: int | None, voting_pct: float | None) -> int | None:
    """The count behind a voting bloc, or None when there is no bloc.

    Row 11 is what row 13's percentage is computed from, so this is that
    percentage's numerator. Like the percentage, it belongs to the GROUP and is
    repeated verbatim by every member — Altria and the Stichting each report the
    same 1,020,598,157 shares. Never sum it across owners, for exactly the
    reason `stake_percent` may not be summed across share classes.
    """
    return aggregate if voting_pct is not None else None


def _shares_held(rows: dict, aggregate: int | None, in_group: bool = True) -> int | None:
    """How many shares this filer actually holds, as a count.

    The count is the fact the filing states; the percentage is a division we
    perform against a denominator that moves. Bevco's "5.9%" went stale purely
    because AB InBev issued more shares — its holding never changed — so the
    number worth keeping is the one that did not.

    Dispositive power, not voting: what the filer can sell is what it owns.
    Sole where it has any, otherwise the shares it disposes of jointly (BRC can
    sell nothing alone, but the Stichting it co-owns holds 771,096,582), and the
    reported aggregate only when neither row is given. That is the rule for a
    member of a bloc (`in_group`), whose shared rows are the group's; a filer
    alone holds both rows — sole and shared together.
    """
    sole = rows.get("sole_dispositive")
    shared = rows.get("shared_dispositive")
    if not in_group and (sole or shared):
        # A filer alone disposes jointly with nobody in a bloc — its shared
        # power is over shares it holds through subsidiaries or trusts. Both
        # rows are its holding: SoftBank's Alibaba 13G/A has 3,788,048 sole and
        # 627,002,296 shared (its subsidiaries), and the sole figure alone made
        # a 3.3 % holder a 0.02 % one wherever the count was divided again.
        held = (sole or 0) + (shared or 0)
        # ...unless the rows overlap. The aggregate (row 9) is the union of
        # the power rows, so a sum above it counts the same shares twice:
        # Musk's Tesla 13G/A lists its 699,580,882 shares as sole AND shared,
        # voting and dispositive alike, and the sum stored twice his holding.
        if aggregate and held > aggregate:
            return aggregate
        return held
    if sole:
        return sole
    if shared:
        return shared
    return aggregate


def _derive_total(aggregate: int | None, percent: float | None) -> int | None:
    """Shares outstanding implied by "this many shares are that percent".

    `percent > 0` is not defensive dressing: a 13G/A amending to 0% — the
    filer announcing it has exited — is common, and would divide by zero.
    """
    if not aggregate or not percent or percent <= 0:
        return None
    return round(aggregate / (percent / 100))


def _sgml_group_members(subject_cik: str, accession: str) -> list[dict]:
    """Co-filers named in the submission's SGML header.

    Pre-2024 filings carry no XML, but EDGAR's header has always listed
    `GROUP MEMBERS:` — names only, no CIKs. Read from the header-only document
    (about 4 KB) rather than the full submission, which for AB InBev's schedule
    is 287 KB of exhibits.
    """
    nodash = accession.replace("-", "")
    url = (f"{sec_client.ARCHIVES_URL}/{sec_client._cik_int(subject_cik)}/{nodash}/"
           f"{accession}-index-headers.htm")
    try:
        raw = sec_client._get_text(url)
    except Exception as exc:  # noqa: BLE001 - a missing header must not sink the scrape
        log.debug("SEC EDGAR: no SGML header for %s (%s)", accession, exc)
        return []
    # NOT _plain_text: it collapses every run of whitespace including newlines,
    # and this format is line-oriented — one GROUP MEMBERS per line. Flattened,
    # a single match swallows the rest of the header as one enormous "name".
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", raw))
    names = re.findall(r"GROUP MEMBERS:[ \t]*([^\r\n]+)", text)
    # No type code in the header — the writer falls back to a name heuristic here.
    return [{"name": n.strip(), "cik": None, "source": "sgml", "type_code": None}
            for n in names if n.strip()]


def _is_structured(form_type: str) -> bool:
    """Does this filing have a `primary_doc.xml`?

    Decided from the form name the feed already gave us, so no request is
    wasted discovering it: the December-2024 modernization both mandated the
    XML and renamed the forms, so "SCHEDULE 13D" has it and the older
    "SC 13D" does not. A modern filing whose XML is missing anyway costs one
    404 and falls back to HTML.
    """
    return form_type.strip().upper().startswith("SCHEDULE 13")


def _fetch_13dg_xml(subject_cik: str, accession: str) -> dict | None:
    """The structured filing, or None if it isn't there or won't parse.

    The Archives path accepts the SUBJECT's CIK even though another party
    filed, so this needs nothing the Atom feed did not already provide.
    """
    nodash = accession.replace("-", "")
    url = f"{sec_client.ARCHIVES_URL}/{sec_client._cik_int(subject_cik)}/{nodash}/primary_doc.xml"
    try:
        raw = sec_client._get_text(url)
    except Exception as exc:  # noqa: BLE001 - absent XML is normal; fall back to HTML
        log.debug("SEC EDGAR: no structured XML for %s (%s)", accession, exc)
        return None
    return _parse_13dg_xml(raw)


def _select_person(xml: dict, filer_cik: str | None, filer_name=None) -> dict | None:
    """Which reporting person is *the investor* on this filing, with its CIK.

    A group files one schedule listing every member, so one block has to be
    chosen as the edge's owner and the rest become the group. Prefer the block
    whose CIK is the filer's. 13G blocks carry no CIK: a lone block is the
    filer; among several, the block named as EDGAR names the filer
    (``filer_name``, a callable CIK → name, asked only then). The chosen block
    is given the filer's CIK only in those cases.

    Never the first block on the filer's CIK: Berkshire's 13Gs list Warren E.
    Buffett first, and his name on Berkshire's CIK made a Person
    "sec:0001067983" holding twelve of Berkshire's stakes (findings item 21).
    A block of its own CIK is still taken when nothing matched; a CIK-less one
    is not — None, the filing is skipped.
    """
    persons = xml.get("persons") or []
    if not persons:
        return None
    if filer_cik:
        want = sec_client._cik_int(filer_cik)
        for p in persons:
            if p.get("cik") and sec_client._cik_int(p["cik"]) == want:
                return p
        if len(persons) == 1 and not persons[0].get("cik"):
            persons[0]["cik"] = filer_cik
            return persons[0]
        name = filer_name(filer_cik) if filer_name else None
        for p in persons:
            if not p.get("cik") and name and sec_13dg_text._same_filer_name(p["name"], name):
                p["cik"] = filer_cik
                return p
    if persons[0].get("cik"):
        log.debug("SEC EDGAR: no reporting person matched filer CIK %s; using the first",
                  filer_cik)
        return persons[0]
    log.info("SEC EDGAR: no reporting person is the filer (CIK %s); skipping the filing",
             filer_cik)
    return None


def _parse_13dg_xml(raw: str) -> dict | None:
    """A Schedule 13D or 13G `primary_doc.xml`, normalised.

    Since the SEC's December 2024 beneficial-ownership modernization these
    filings are machine-readable, which removes the guesswork this module used
    to do against HTML cover pages: the issuer is a CIK rather than a name to
    match, the four power rows are fields rather than regex captures, and — the
    reason this was written — every member of a filing group gets its own
    block, so group membership no longer has to be read out of Item 6 prose.

    Returns None for anything unparseable, including pre-2024 filings, which
    simply have no XML; the caller falls back to the HTML path for those.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log.debug("SEC EDGAR: unparseable 13D/G XML: %s", exc)
        return None

    for container, issuer_tag, pct_tag, agg_tag, person_cik_tag in _XML_LAYOUTS:
        blocks = root.findall(f".//{{*}}{container}")
        if blocks:
            break
    else:
        return None

    # Which schedule this is, from the layout that matched: a 13G's co-filers
    # are a fund family, a 13D's a group — see `_co_filers_form_a_bloc`.
    schedule = "13G" if container.startswith("coverPageHeader") else "13D"

    persons = []
    for b in blocks:
        name = _xml_child(b, "reportingPersonName")
        if not name:
            continue
        persons.append({
            "name":               name,
            "cik":                _xml_child(b, person_cik_tag) if person_cik_tag else None,
            "sole_voting":        _xml_num(b, "soleVotingPower"),
            "shared_voting":      _xml_num(b, "sharedVotingPower"),
            "sole_dispositive":   _xml_num(b, "soleDispositivePower"),
            "shared_dispositive": _xml_num(b, "sharedDispositivePower"),
            "aggregate":          _xml_num(b, agg_tag),
            "percent":            _xml_num(b, pct_tag, as_int=False),
            "type_code":          _xml_child(b, "typeOfReportingPerson"),
        })
    if not persons:
        return None

    # The denominator is not a field, but filers usually state it in the
    # comment; that text is worth carrying so `_shares_outstanding` can read it.
    comments = [(e.text or "") for tag in ("commentContent", "comments")
                for e in root.findall(f".//{{*}}{tag}")]

    return {
        "issuer_cik":   _xml_child(root, issuer_tag),
        "issuer_name":  _xml_child(root, "issuerName"),
        # The security identifier 13F info tables key on — harvesting it from
        # the schedules we already read is how companies gain a CUSIP without a
        # licensed database. Both spellings occur in real filings.
        # Three spellings: 13G's `issuerCusip`, the list form `issuerCusips/
        # issuerCusipNumber`, and Schedule 13D's `issuerCUSIP` (Berkshire
        # Hathaway's 13D/A of 2025) — the last was never read, so no 13D
        # cover gave its issuer a CUSIP. Only a valid one (check digit) counts.
        "issuer_cusip": next((c for tag in ("issuerCusipNumber", "issuerCusip", "issuerCUSIP")
                              for e in root.findall(f".//{{*}}{tag}")
                              if (c := sec_13dg_text._cover_cusip(e.text))), None),
        "filer_cik":    _xml_child(root, "cik"),
        # WHICH security the percentages are percentages OF. A percent of class
        # is meaningless without it: Grupo Televisa's filers report 22.3% of
        # "Series A/B/Dividend Preferred" beside 9.7% of "CPOs and Global D
        # shares", and adding those gave the company 115.9% of itself.
        "class_title":  _xml_child(root, "securitiesClassTitle"),
        # The day the position was as stated — 13D "dateOfEvent", 13G
        # "eventDateRequiresFilingThisStatement" (a quarter-end for the
        # passive filers). Not the filing date, which can be weeks later.
        "event_date":   sec_13dg_text._event_date(_xml_child(root, "dateOfEvent")
                                    or _xml_child(root, "eventDateRequiresFilingThisStatement")),
        "persons":      persons,
        "schedule":     schedule,
        # the whole schedule, its Items 4 and 6 included: whether it names
        # an agreement over the votes (`_is_voting_pact`, findings item 103)
        "raw_text": raw,
        "comment_text": " ".join(comments),
        # The filer's LAST report (findings item 35): a 13G ticks Item 5,
        # "Ownership of 5 Percent or Less of a Class" — it has ceased to own
        # more than 5 % and files nothing more; a 13D states in Item 5(e) the
        # day it ceased (free text, "Not applicable." while it has not).
        "final_report": _final_report_of_xml(root, schedule),
    }


def _final_report_of_xml(root, schedule: str) -> str | bool | None:
    """True when a structured 13G ticks Item 5 (ceased to own more than 5 %),
    the stated day when a 13D's Item 5(e) gives one, else None. BlackRock's
    13G/A on Tesla of 2026-10-27 states 4.96 % with the box ticked."""
    if schedule == "13G":
        box = (_xml_child(root, "classOwnership5PercentOrLess") or "").strip().upper()
        return True if box in ("Y", "YES", "TRUE", "X") else None
    return sec_13dg_text._event_date(_xml_child(root, "date5PercentOwnership"))


def _votes_others_shares(rows: dict) -> bool:
    """Does this reporting person vote more shares than it can dispose of —
    alone or jointly? The excess is other parties' shares under a voting
    agreement: a bloc (findings item 98).

    BRC votes 1,031,302,286 AB InBev shares and disposes (jointly) of
    769,317,631; Altria votes 1,020,598,157 and disposes of 159,121,937. A
    parent filing with its own subsidiaries — Berkshire on DaVita, Bank of
    America with its preferred-funding arm, General Atlantic's 30 funds,
    SoftBank on Sprint — disposes of every share it votes: its row 13 IS its
    stake, held through those subsidiaries. 77 of the 83 filer-side holdings
    the issuer-side split would have changed on dev were such families."""
    shared_vote = rows.get("shared_voting") or 0
    disposable = (rows.get("sole_dispositive") or 0) + (rows.get("shared_dispositive") or 0)
    return shared_vote > disposable


def _filer_person(xml: dict, filer_cik: str, filer_name: str | None) -> dict | None:
    """The reporting person block that is the FILER's own (findings item 98).

    A joint schedule carries one block per reporting person, and the first is
    whoever the filer listed first: BRC's 13D/A on AB InBev (May 2026) starts
    with the Stichting, so its row 13 — the bloc's 52.3 % — was written as BRC's
    stake. A 13D block states its CIK; a 13G block only a name. Falls back to
    the first block when neither matches (what was read before)."""
    persons = xml.get("persons") or []
    if not persons:
        return None
    own = sec_client._cik_int(filer_cik).zfill(10)
    for p in persons:
        if p.get("cik") and sec_client._cik_int(p["cik"]).zfill(10) == own:
            return p
    if len(persons) == 1:
        return persons[0]
    if filer_name is None:
        # only a joint 13G gets here (no CIK per block); the submissions
        # document the scan already read — cached, no request
        filer_name = sec_filer.fetch_filer_name(filer_cik) or ""
    if filer_name:
        for p in persons:
            if p.get("name") and sec_13dg_text._same_filer_name(p["name"], filer_name):
                return p
    return persons[0]
