"""
SEC EDGAR — reading a 13D/G statement's cover page and text.

Percent of class, the voting and dispositive rows, CUSIP, issuer and class
title, the reporting persons, the group box, the event date and the
final-report words; and the arithmetic that turns the rows into a stake.

Source, rate limits and licence: see `sec_edgar`.
"""

import re
from datetime import datetime
import html as html_lib
import unicodedata
import logging

import httpx
from app.scraper.mapper import (fold_apostrophes, join_dotted_letters, normalize_entity_name, strip_partnership_form)
from app.scraper import sec_client, sec_lookup

log = logging.getLogger(__name__)


# ── Investor info from EDGAR submissions + filing document ────────────────────

_PERCENT_PATTERNS = [
    # Standard SC 13G/13D cover page: "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW N  X.X%"
    r'percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+row\s+\d+\s+(\d{1,2}\.?\d*)\s*%',
    # Item 13 label followed by the value (some filings use "Item 13" as the header)
    r'item\s*13\.?\s*percent\s+of\s+class[^\n]*?\n[^\n]*?(\d{1,2}\.?\d*)\s*%',
    # Fallback: "percent of class" anywhere, followed within 300 chars by a percentage
    # (uses .{0,300}? instead of [^\d%]{0,300}? to not break on digits in labels)
    r'percent\s+of\s+class\s+represented.{0,300}?(\d{1,2}\.?\d*)\s*%',
]


#: Cover-page rows 5–8 (13G) / 7–10 (13D). Row labels are stable across both.
_POWER_ROWS = {
    "sole_voting":       r'sole\s+voting\s+power',
    "shared_voting":     r'shared\s+voting\s+power',
    "sole_dispositive":  r'sole\s+dispositive\s+power',
    "shared_dispositive": r'shared\s+dispositive\s+power',
}


def _parse_power_rows(text: str) -> dict:
    """Share counts from the cover page's four power rows.

    These are what make a 13D honest. "Beneficial ownership" is about power,
    not property: a member of a voting group reports the WHOLE group's shares
    in row 11, so summing row 13 percentages across a group counts the same
    shares once per member. AB InBev summed to 109.9% that way — Altria and the
    Stichting each reporting the same billion shares.

    Sole dispositive power is what the filer can actually sell: its own stake.
    Altria's cover reads sole-voting 0, shared-voting 1,020,598,157, and
    sole-dispositive 159,121,937 — the last is Altria's real 8.1%, and the
    first is the Voting Agreement bloc it votes within.

    Missing rows are absent from the result rather than zero: "the filing did
    not say" and "the filing said none" must not be confused, since a zero
    would otherwise read as a genuine 0% stake.
    """
    plain = _plain_text(text)
    out: dict = {}
    deferred: dict = {}
    for key, label in _POWER_ROWS.items():
        m = re.search(label + r'(?P<gap>[^0-9]{0,60}?)(?P<val>' + _ZERO_WORDS + r'|[\d,]+)',
                      plain, re.IGNORECASE)
        if not m:
            continue
        gap, val = m.group("gap"), m.group("val")
        if re.search(r'\brow\b', gap, re.IGNORECASE):
            # "See Row 6 above." — the value lives in another row; copy it
            # once every row has been read.
            deferred[key] = int(val) if val.isdigit() else None
            continue
        if re.fullmatch(r'\d{1,2}', val) and int(val) >= 5 \
                and _NEXT_ROW_LABEL.match(plain, m.end()):
            # An empty cell: the digits are the NEXT row's number ("SOLE VOTING
            # POWER 6 SHARED VOTING POWER …"). Berkshire's Activision 13G read
            # as 8 shares this way. Absent, not a count. Rows are numbered 5–13,
            # so a "0" before the next label is a real zero.
            continue
        if re.fullmatch(_ZERO_WORDS, val, re.IGNORECASE):
            out[key] = 0
            continue
        try:
            out[key] = int(val.replace(",", ""))
        except ValueError:
            continue
    for key, row_no in deferred.items():
        ref = _row_key(plain, row_no) if row_no else None
        if ref in out:
            out[key] = out[ref]
    return out


#: How old filings write zero in a power row: "0", "-0-", "—0—", "NONE".
_ZERO_WORDS = r'(?:none|nil|-\s*0\s*-|[—–]\s*0\s*[—–])'

#: What follows a row NUMBER on the cover — used to tell "6" the next row's
#: number from "6" a share count. Row 9/10 labels vary, so the list is the
#: words that open rows 5–13 on both schedules.
_NEXT_ROW_LABEL = re.compile(
    r'\.?\s*\(?\s*(?:sole|shared|aggregate|check|percent|type)\b', re.IGNORECASE)


def _row_key(plain: str, row_no: int) -> str | None:
    """Which power row a cover's row number denotes ("6" → shared_voting on a
    13G, sole_dispositive on a 13D — the two schedules number rows differently,
    so read the label that follows the number rather than assume)."""
    m = re.search(rf'(?:^|[\s(]){row_no}\)?\.?\s*\(?\s*((?:sole|shared)\s+(?:voting|dispositive))\s+power',
                  plain, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).lower().replace(" ", "_")


def _shares_outstanding(text: str) -> int | None:
    """The denominator, from the filing's own footnote ("based on a total of
    N shares issued and outstanding"). Filings state it so the percentages can
    be checked; we use it to turn a share count into a percentage."""
    plain = _plain_text(text)
    m = re.search(r'based\s+(?:up)?on\s+a?\s*total\s+of\s+([\d,]{7,})', plain, re.IGNORECASE)
    if not m:
        m = re.search(r'([\d,]{9,})\s+(?:voting\s+)?shares?\s+issued\s+and\s+outstanding',
                      plain, re.IGNORECASE)
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
        return n if n > 0 else None
    except ValueError:
        return None


def _pct_of(part: float | None, whole: int | float | None) -> float | None:
    """`part` as a percentage of `whole`, or None when no percentage can be stated.

    A holding can be entirely real and still round to 0.0 at four decimals: six
    Apple directors hold 1,139 shares each, which is 0.0000076% of ~15bn shares.
    Storing that as `0.0` says "owns nothing" — the opposite of the truth, and
    indistinguishable from a filer who has actually exited. None says "we know
    the share count, not a meaningful percentage", and the count is on the edge
    to say the rest; `unknown_owners` in the ownership summary already accounts
    for owners whose share cannot be put in a number.

    Same reasoning as `_split_stake`'s sole-dispositive-of-nothing branch, which
    returns None for exactly this reason.
    """
    if part is None or not whole:
        return None
    pct = round(part / whole * 100, 4)
    if pct == 0 and part:
        return None                 # real, but below the precision floor
    if pct > 100:
        # More shares than the class has: the denominator is wrong (a scaled
        # cover read as written — Toast's 514 for 514 million gave 693.77 %),
        # never the holding. No percentage beats a confidently wrong one, and
        # the database refuses a stake above 100, failing the whole scrape.
        log.warning("SEC EDGAR: %s shares of %s outstanding is %s%% — no percentage stated",
                    part, whole, pct)
        return None
    return pct


def _own_stake_and_voting(text: str, reported_pct: float | None,
                          document: str | None = None,
                          in_group: bool = True) -> tuple:
    """Split a 13D/G cover into (own stake %, voting-bloc %).

    Returns the filer's OWN holding as the stake and the reported row-13
    figure as voting power whenever the two genuinely differ — i.e. when the
    filer is part of a group. For a lone filer sole-dispositive equals the
    aggregate and there is no bloc, so voting comes back None and nothing
    about the common case changes.

    `document` is the whole filing when `text` is one reporting person's cover
    page of a joint filing: the outstanding-shares footnote sits outside the
    cover pages. `in_group` is what the caller knows about co-filers — see
    `_co_filers_form_a_bloc`; the default keeps the conservative reading for
    callers that know nothing.
    """
    return _split_stake(_parse_power_rows(text), _shares_outstanding(document or text),
                        reported_pct, in_group=in_group)


def _co_filers_form_a_bloc(form_type: str | None, reporting_persons: int) -> bool:
    """Do this filing's co-filers make the filer a group member?

    Only a 13D with more than one reporting person. A 13G is passive by
    definition: several reporting persons on one are a fund family or a parent
    with its subsidiaries (Berkshire Hathaway + Buffett + National Indemnity on
    Activision, Sequoia's four funds on LinkedIn), each holding the shares it
    reports — not a governance bloc. Treating them as one nulled the stake of
    every such filer and showed the family's aggregate as a phantom "bloc".
    """
    short = sec_client._short_form(form_type) or ""
    if short.upper().startswith("13G"):
        return False
    # 0 = the count is unknown (a cover without the "Name of Reporting Person"
    # rows): keep the conservative reading, as the text path always did.
    return reporting_persons != 1


#: what makes co-filers a voting group rather than a joint filing (findings
#: item 103): an agreement among them over how the shares vote, named in the
#: statement (Items 4 and 6)
_PACT_WORDS = re.compile(r"voting\s+agreement|(?:stock|share)holders['’]?\s+agreement|voting\s+trust", re.I)
#: a cover's "member of a group" box: (a) affirms, (b) disclaims
_GROUP_BOX_XML = re.compile(r"<(?:memberOfGroup|memberGroup)>\s*([ab])\s*<", re.I)
_GROUP_BOX_TEXT = re.compile(r"member\s+of\s+a\s+group[^()]{0,200}\(a\)\s*(\S{1,3})\s*\(b\)\s*(\S{1,3})", re.I)
_BOX_TICKED = re.compile(r"x|☒|þ|ý|✓|☑", re.I)


def _group_boxes(text: str) -> list[str]:
    """Each reporting person's "member of a group" box: 'a' (a group), 'b' (not
    one), '-' (neither ticked) — from the XML schedule or the text covers."""
    boxes = [b.lower() for b in _GROUP_BOX_XML.findall(text or "")]
    if boxes:
        return boxes
    out = []
    for a, b in _GROUP_BOX_TEXT.findall(_plain_text(text or "")):
        out.append("a" if _BOX_TICKED.search(a) else ("b" if _BOX_TICKED.search(b) else "-"))
    return out


def _is_voting_pact(form_type: str | None, reporting_persons: int, text: str | None) -> bool:
    """Do this statement's co-filers form a voting pact — the only joint 13D the
    issuer side draws as a "Voting group" (findings item 103, the user's rule of
    2026-11-11)? A 13D with co-filers (`_co_filers_form_a_bloc`) whose text
    names an agreement over the votes — a voting agreement, a stockholders' or
    shareholders' agreement, a voting trust — and whose persons do not all
    disclaim a group (box (b)). Of the 44 joint 13Ds behind dev's 27 groups,
    two are: AB InBev's (BRC, the Stichting, … — the Voting Agreement) and the
    New York Times' (the Ochs-Sulzberger family trust). The rest are one
    controlling chain or one family filing together, as Rule 13d-1(k) allows —
    Berkshire with its insurers on DaVita, Icahn's entities, SoftBank's funds:
    a "Joint filing" whose figure is one holding. How each statement's row 13
    is split stays as it was (`_co_filers_form_a_bloc`): only the group node
    the writer draws changes."""
    if not _co_filers_form_a_bloc(form_type, reporting_persons) or not _PACT_WORDS.search(text or ""):
        return False
    boxes = _group_boxes(text or "")
    return not (boxes and all(b == "b" for b in boxes))


def _split_stake(rows: dict, total: int | None, reported_pct: float | None,
                 in_group: bool = False) -> tuple:
    """The stake/voting split over already-extracted numbers.

    Separated from the text parsing so the XML path can reuse the judgement
    without going near a regex; `_own_stake_and_voting` above is now a thin
    wrapper that supplies the numbers from an HTML cover page.

    `in_group` is the fact the power rows alone cannot reveal. A lone
    institutional custodian (State Street over News Corp) reports sole
    dispositive 0 and everything "shared" — it holds for client accounts, not
    as a control bloc — and its aggregate IS its 4.7% beneficial stake. A
    genuine group member (BRC in the AB InBev Voting Agreement) has the same
    row shape but reports the WHOLE bloc's shares, which must NOT become its
    individual stake or members sum past 100%. Only co-filers tell them apart.
    """
    sole_disp = rows.get("sole_dispositive")
    shared_vote = rows.get("shared_voting")
    if sole_disp is None or not shared_vote:
        return reported_pct, None
    if sole_disp >= shared_vote:
        return reported_pct, None          # no group: the filer holds it all

    if not in_group:
        shared_disp = rows.get("shared_dispositive") or 0
        if sole_disp == 0 or shared_vote <= sole_disp + shared_disp:
            # A lone filer with all-shared power is a custodian, not a bloc:
            # its reported percent of class is its own stake, exactly like the
            # common case above. Without this, every all-shared custodian
            # (State Street, and many BlackRock/Vanguard filings) lost its
            # stake to a phantom bloc.
            return reported_pct, None
        # A lone filer that holds shares of its OWN and votes more than it can
        # dispose of at all is a party to a voting agreement, filing alone:
        # Altria's 2025 13D/A (the structured kind, one reporting person)
        # disposes of 159,121,937 AB InBev shares and shares the vote over
        # 1,020,598,157 with Bevco and the Stichting. Row 13's 51.9% is that
        # bloc, and as Altria's stake it made Altria AB InBev's majority
        # owner; its own shares are 8.1%. The excess vote is somebody else's
        # shares, so it is the voting power, never the stake. (No denominator:
        # _pct_of states no stake; the bloc stands.)
        return _pct_of(sole_disp + shared_disp, total), reported_pct

    if sole_disp == 0:
        # Everything this filer holds, it holds jointly — BRC S.à.r.l. can
        # dispose of nothing alone; its shares sit in the Stichting it co-owns
        # with EPS. There is no individual stake to state, and 0.0 would read
        # as "owns nothing" — the opposite of the truth. Unknown, plus the
        # bloc it votes in; `unknown_owners` in the ownership summary already
        # accounts for owners whose share we cannot put a number on.
        return None, reported_pct

    if not total:
        # The bloc is real but the denominator is unknown. Keeping the bloc
        # figure as the stake is exactly what produced the >100% sums, so
        # state only what is certain.
        return None, reported_pct
    return _pct_of(sole_disp, total), reported_pct


def _plain_text(raw: str) -> str:
    """Strip HTML tags and decode entities so regex patterns match cleanly."""
    decoded = html_lib.unescape(raw)
    stripped = re.sub(r'<[^>]+>', ' ', decoded)
    return re.sub(r'\s+', ' ', stripped)


# SC 13G/13D Item 8 "Type of Reporting Person" codes that mean an individual.
# All other codes (CO, PN, IA, IC, BK, BD, SA, OO, EP, GP …) are entities —
# except the ones below, which say nothing either way.
_INDIVIDUAL_CODES = {"IN"}

#: "HC" is a parent holding company OR a control person: Berkshire Hathaway
#: Inc. files as HC, and so does James Dondero on NexPoint's 13D/A, beside
#: his wife's IN. Read as an entity it minted a company "Dondero James D"
#: carrying his CIK, which his own Form 4 then gave a Person node too
#: (findings item 21). The code decides nothing; the name does.
_EITHER_CODES = {"HC"}


def _individual_by_code(code: str | None) -> bool | None:
    """Item 8's answer to "is this a human being": True for IN, False for an
    entity code, None when the box is empty or holds only `_EITHER_CODES` —
    the caller then decides by the name. A box may list several codes
    ("HC, CO" on Berkshire's covers); the definite ones decide."""
    codes = {c for c in re.split(r"[\s,;/]+", (code or "").upper()) if c} - _EITHER_CODES
    if not codes:
        return None
    return bool(codes & _INDIVIDUAL_CODES)

def _parse_percent_from_text(text: str) -> float | None:
    """Extract stake % from Item 13 of a SC 13D/13G filing document."""
    plain = _plain_text(text)
    for pattern in _PERCENT_PATTERNS:
        m = re.search(pattern, plain, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                val = float(m.group(1))
                # 0 is a statement (the filer holds nothing — an exit); only
                # an unreadable or impossible figure is None
                if 0 <= val <= 100:
                    return val
            except (ValueError, IndexError):
                pass
    return None


#: Where a 13D/G cover states the issuer's CUSIP: "CUSIP No. 46435G508",
#: "CUSIP Number: 084670702", "00507V109 (CUSIP Number)" (real covers of
#: BlackRock, Berkshire Hathaway), sometimes spaced "82968B 10 3".
_CUSIP_AFTER = re.compile(r"CUSIP\s*(?:No\.?|Number|#)\s*:?\s*([0-9A-Z][0-9A-Z \-]{5,16})", re.I)
_CUSIP_BEFORE = re.compile(r"([0-9A-Z][0-9A-Z \-]{5,16}?)[\s\-_=]*\(\s*CUSIP\s+Number\s*\)", re.I)


def _cover_cusip(raw: str | None) -> str | None:
    """A CUSIP as a cover states it, or None: spaces and dashes go
    ("82968B 10 3"), and only nine characters whose check digit is right are a
    CUSIP. A wrong one would hand the securities bridge (findings item 71) an
    ISIN of another issuer; a typo almost never survives the check digit."""
    from app.scraper.gleif_isin import cusip_valid
    c = re.sub(r"[\s\-]", "", raw or "").upper()
    return c if cusip_valid(c) else None


def _cusip_from_words(words: str, *, from_end: bool = False) -> str | None:
    """The run of words next to the label that makes exactly nine characters,
    if they are a valid CUSIP. After "CUSIP No." the capture runs on into the
    next field ("084670702 1. Names"), so the words are read from the start;
    before "(CUSIP Number)" it reaches back into the class title ("Common
    Stock 82968B 10 3"), so they are read from the end."""
    parts = [w for w in re.split(r"[\s\-]+", words.strip()) if w]
    joined = ""
    for word in (reversed(parts) if from_end else parts):
        joined = word + joined if from_end else joined + word
        if len(joined) >= 9:
            return _cover_cusip(joined) if len(joined) == 9 else None
    return None


def _parse_cusip_from_text(text: str) -> str | None:
    """The issuer's CUSIP from an older (text or HTML) 13D/G cover, the first
    valid one in the document. Every cover states it — the securities bridge
    reads it as the issuer's ISIN — and the XML schedules carry it as a field
    (`_parse_13dg_xml`)."""
    plain = _plain_text(text)
    found = sorted([(m.start(), m.group(1), False) for m in _CUSIP_AFTER.finditer(plain)]
                   + [(m.start(), m.group(1), True) for m in _CUSIP_BEFORE.finditer(plain)])
    for _, words, from_end in found:
        if (c := _cusip_from_words(words, from_end=from_end)):
            return c
    return None


def _parse_issuer_from_text(text: str) -> str | None:
    """Extract the issuer's name from a SC 13D/13G cover page.

    Every 13D/G cover opens with the company whose shares are being reported:
    "Eve Holding, Inc. (Name of Issuer)". This is the only field on the whole
    filing that reliably says WHO the stake is in — the EDGAR index metadata
    does not: Embraer's agent filed its Eve Holding 13D/A with EMBRAER S.A. in
    the SUBJECT COMPANY header, so both the index page and the SGML header
    named the wrong company, and only the cover page told the truth.
    """
    plain = _plain_text(text)
    m = re.search(r'([^()]{2,100}?)\s*\(\s*Name\s+of\s+Issuer\s*\)', plain,
                  re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    # The capture is greedy backwards into the boilerplate; keep the tail after
    # the last sentence-ish break so "…Act of 1934 Eve Holding, Inc." trims to
    # the company name alone.
    name = re.split(r'(?:\*|\d{4}|No\.\s*\d*\)?|schedule\s+13[dg](?:/a)?)\s+',
                    name, flags=re.IGNORECASE)[-1].strip(' .*')
    # Text-format filings underline the name with a rule of dashes; drop any
    # trailing run of punctuation ("EMBRAER S.A. ------------" -> "EMBRAER S.A.").
    name = re.sub(r'[\s\-_=~.]{3,}$', '', name).strip()
    return name or None


#: The issuer check filters tokens rather than substituting substrings, and
#: additionally ignores pure filler and depositary-receipt words that the
#: ticker matcher has no reason to strip.
_NAME_NOISE = frozenset(sec_lookup._LEGAL_FORM_WORDS) | {"the", "of", "and", "adr", "ads"}


def _significant_tokens(name: str) -> set:
    # Fold diacritics first: "Nestlé" must tokenize to "nestle", not split on
    # the é into a stump that matches nothing — that would silently reject
    # every legitimate filing about an accented-name company.
    # Apostrophes go before that, or "Domino's" splits off a stray "s" token.
    folded = unicodedata.normalize("NFKD", fold_apostrophes(name)).encode("ascii", "ignore").decode()
    # "L.P." is one word; a trailing KG/KGaA is a legal form
    tokens = re.split(r"[^a-z0-9]+", strip_partnership_form(join_dotted_letters(folded.lower())))
    return {t for t in tokens if t and t not in _NAME_NOISE}


def _issuer_matches(company_names, issuer_name: str | None) -> bool:
    """Does the filing's cover-page issuer plausibly mean the scraped company?

    Deliberately tolerant: it exists to catch a filing about a DIFFERENT company
    ("Eve Holding" vs "Embraer"), not to police spelling. Sharing one
    significant token with ANY known name of the company is agreement —
    "Anheuser-Busch InBev SA/NV" vs "Anheuser-Busch InBev", "EMBRAER S.A." vs
    "Embraer". `company_names` may be one name or an iterable of them: the
    caller passes the current name plus EDGAR's formerNames, because a rename
    keeps the CIK and the older covers carry the old name — a scrape of "Meta
    Platforms" must not throw away a 13G whose cover says "Facebook, Inc".

    No issuer extracted (None) is also agreement: a positive mismatch is the
    only safe ground to throw a filing away, and old text-format filings may
    not parse.
    """
    if not issuer_name:
        return True
    if isinstance(company_names, str):
        company_names = [company_names]
    theirs = _significant_tokens(issuer_name)
    if not theirs:
        return True
    seen_any = False
    for name in company_names:
        ours = _significant_tokens(name)
        if not ours:
            continue
        seen_any = True
        if ours & theirs:
            return True
    return not seen_any


def _parse_class_title_from_text(text: str) -> str | None:
    """The security a cover page's percentages refer to.

    Every 13D/G cover names it right under the issuer: "Ordinary Shares,
    without nominal value (Title of Class of Securities)". Pre-2024 filings
    have no XML, so this is the only way to know whether two percentages share
    a denominator.
    """
    plain = _plain_text(text)
    m = re.search(r'([^()]{2,120}?)\s*\(\s*Title\s+of\s+Class\s+of\s+Securities\s*\)',
                  plain, re.IGNORECASE)
    if not m:
        return None
    title = m.group(1).strip()
    # The capture reaches back through the issuer line; keep only what follows
    # the "(Name of Issuer)" marker when it is there.
    title = re.split(r'\(\s*Name\s+of\s+Issuer\s*\)', title, flags=re.IGNORECASE)[-1]
    title = re.sub(r'[\s\-_=~.]{3,}$', '', title).strip(' .,*')
    return title or None


def _parse_reporter_type_from_text(text: str) -> bool | None:
    """
    Extract Item 8 'Type of Reporting Person' from a SC 13D/13G filing.
    Returns True if the filer is an individual (code IN), False if it is
    a legal entity (CO, PN, IA, IC, BK, …), or None if the field is absent
    or says nothing either way (HC, `_individual_by_code`). Every code the
    box lists is read; the second and later ones only in capitals, so the
    prose after the box ("CO in the …") is not read as one.
    """
    plain = _plain_text(text)
    m = re.search(
        r'type\s+of\s+reporting\s+person[^a-z]{0,80}'
        r'\b((?:IN|CO|PN|IA|IC|BK|BD|SA|OO|HC|EP|GP|LP)\b(?:[\s,;/]+(?-i:IN|CO|PN|IA|IC|BK|BD|SA|OO|HC|EP|GP|LP)\b)*)',
        plain, re.IGNORECASE | re.DOTALL,
    )
    if m:
        return _individual_by_code(m.group(1))
    return None


#: The row that opens every reporting person's cover page ("NAME OF REPORTING
#: PERSON" on a 13G, "NAMES OF REPORTING PERSONS" on a 13D).
_REPORTING_PERSON_ROW = re.compile(r'names?\s+of\s+reporting\s+persons?', re.IGNORECASE)


def _cover_page_count(text: str) -> int:
    """How many reporting persons a 13D/G document has — one cover page each.
    The text path's co-filer count, which the SGML header otherwise supplies
    only on a second request."""
    return len(_REPORTING_PERSON_ROW.findall(_plain_text(text)))


def _cover_page_names(text: str) -> list[str]:
    """The reporting persons a 13D/G document names, one per cover page.

    The text path's counterpart of the XML's person blocks: the name is what
    follows the "Name of Reporting Person" row up to the S.S./I.R.S. line,
    "(see instructions)" or row 2 — the same cut `_cover_page_for` makes.
    """
    plain = _plain_text(text)
    names = []
    for m in _REPORTING_PERSON_ROW.finditer(plain):
        name = _COVER_NAME_END.split(plain[m.end():m.start() + 200], maxsplit=1)[0]
        name = name.strip(" \t:.-()")
        if name:
            names.append(name)
    return names


def _co_filer_names(names: list[str], filer: str) -> list[str]:
    """The reporting persons of a statement other than its filer.

    Never the filer's own name: another CIK under that name is another legal
    entity — BlackRock's old parent (CIK 0001364742, 'BlackRock Inc.' on its
    2024 13G/As) and the new BlackRock, Inc. (0002012383), whose newer 13Gs on
    the same 33 dev issuers would otherwise have swallowed the old parent's.
    That pair is `close_reported_by_parent`'s, which closes the old holding
    instead of dropping it (findings item 34).
    """
    return [n for n in names if not _same_filer_name(n, filer)]


def _named_in(name: str | None, stated: list[str]) -> bool:
    """Is `name` one of the reporting persons of a statement already taken?"""
    return bool(name) and any(_same_filer_name(n, name) for n in stated)


def _cover_page_for(text: str, filer_name: str | None) -> str:
    """The cover page belonging to `filer_name` in a joint 13D/G, as plain text.

    A joint filing carries one cover page per reporting person, back to back.
    Embraer's 2009 13G has Júlio Bozano (IN, 10.4%) on the first page and his
    holding company Cia. Bozano (CO, 9.2%) on the second. Every field parser
    above takes the FIRST match in the document, so a filer named on a later
    page was read with the first page's type code, percentage and share
    count — Cia. Bozano was minted as a Person owning 10.4%.

    Cut the document at each "Name of Reporting Person" row and hand the
    parsers the page whose header names the filer. A single-page filing, no
    filer name, or a header matching no page returns the whole document, so
    nothing about the common case changes. The shared header (issuer, class
    title) and the outstanding-shares footnote sit outside the pages and are
    still read from the whole document.
    """
    plain = _plain_text(text)
    starts = [m.start() for m in _REPORTING_PERSON_ROW.finditer(plain)]
    if len(starts) < 2 or not filer_name:
        return plain
    wanted = _significant_tokens(filer_name)
    if not wanted:
        return plain
    best, best_score = None, (0, 0.0)
    for start, end in zip(starts, starts[1:] + [len(plain)]):
        page = plain[start:end]
        # The name is what follows the row label up to the next row — the
        # S.S./I.R.S. line, "(see instructions)" or row 2. Nothing after that
        # may count: a shorter name lets more boilerplate into a fixed window,
        # which is how "Berkshire Hathaway International Insurance Ltd." outscored
        # "Berkshire Hathaway Inc." for the filer Berkshire Hathaway Inc.
        name = _REPORTING_PERSON_ROW.sub("", page[:200], count=1)
        name = _COVER_NAME_END.split(name, maxsplit=1)[0]
        header = _significant_tokens(name)
        overlap = len(wanted & header)
        # Most shared tokens wins; the share of the name they cover breaks
        # ties — "Bozano" alone matches both Bozano pages, "Cia. Bozano" only one.
        score = (overlap, overlap / len(header) if header else 0.0)
        if score > best_score:
            best, best_score = page, score
    return best if best is not None else plain


#: What ends the reporting person's name on a cover page.
_COVER_NAME_END = re.compile(
    r'\(?\s*see\s+instructions|s\.?\s*s\.?\s+or\s+i\.?\s*r\.?\s*s|i\.?\s*r\.?\s*s\.?\s+identification'
    r'|\b2\s*[.)]?\s*\(?\s*check\b',
    re.IGNORECASE)


def _fetch_filing_index(index_url: str) -> tuple[str | None, str | None, str | None]:
    """
    Fetch an EDGAR filing index HTML page and extract:
      - investor (filer) name
      - investor CIK (zero-padded 10 digits)
      - primary document URL (for parsing stake percentage)

    The index page has separate filerDiv blocks for the subject company ("Subject")
    and the reporting persons ("Filed by"). We want the latter.
    """
    try:
        html = sec_client._get_text(index_url)
    except httpx.HTTPError as exc:
        log.debug("SEC EDGAR: index fetch failed %s: %s", index_url, exc)
        return None, None, None

    # Extract the first "(Filed by)" company name and its CIK
    name = None
    cik  = None
    m = re.search(
        r'class="companyName">\s*([^<(]+?)\s*\(Filed by\).*?CIK=(\d+)',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m:
        name = html_lib.unescape(m.group(1).strip())
        cik  = m.group(2).zfill(10)

    return name, cik, _primary_doc_url(html)


def _primary_doc_url(index_html: str) -> str | None:
    """The cover document of a pre-2025 13D/G: the first row of the index
    page's document table whose type cell says SC 13."""
    m = re.search(
        r'href="(/Archives/edgar/data/\d+/[^"]+\.(?:htm|txt))"[^>]*>[^<]*</a>\s*</td>\s*<td[^>]*>SC\s*13',
        index_html, re.DOTALL | re.IGNORECASE,
    )
    return f"https://www.sec.gov{m.group(1)}" if m else None


_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}


def _stake_start(form_type: str | None, event_date: str | None,
                 file_date: str | None) -> tuple[str | None, str | None]:
    """(since, since_basis) for a 13D/G holding, from the one filing read.

    An ORIGINAL schedule is due within days of crossing 5 % (10 for a 13D, 45
    for a passive 13G), so its date of event states the start — the file date
    where the cover gives none. An AMENDMENT says only that the position
    existed by then: the scrape reads each holder's newest filing, and its
    date written as a stated start made a holder since 2005, first scraped in
    2026, "since 2026" — absent from every year before. So it is a lower bound
    (`amendment`), which time travel shows dimmed before the date, not hidden.
    """
    when = event_date or file_date
    if not when:
        return None, None
    if "/A" in (form_type or "").upper():
        return when, "amendment"
    return when, None


def _event_date(value: str | None) -> str | None:
    """A cover's date of event as ISO `YYYY-MM-DD`: "02/07/2025" (the
    structured schedules) or "March 10, 2020" (the old HTML covers). None for
    anything else — a date we cannot read is not a date we may guess."""
    v = (value or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", v)
        if not m or m.group(1).lower() not in _MONTHS:
            return None
        month, day, year = _MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _parse_event_date_from_text(text: str) -> str | None:
    """The date printed above "(Date of Event Which Requires Filing of this
    Statement)" on an HTML cover page."""
    plain = _plain_text(text)
    m = re.search(r"([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})\s*\(?\s*"
                  r"Date\s+of\s+Event\s+which\s+Requires", plain, re.IGNORECASE)
    return _event_date(m.group(1)) if m else None


def _parse_aggregate_from_text(text: str) -> int | None:
    """Row 11, "Aggregate Amount Beneficially Owned" — the count the cover's
    percent-of-class is a percentage of."""
    m = re.search(r'Aggregate Amount Beneficially Owned[^0-9]{0,60}([\d,]{4,})',
                  _plain_text(text), re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _same_filer_name(block_name: str, edgar_name: str) -> bool:
    """A reporting person's name and EDGAR's name for the filer mean the same
    party: the same words once the legal form is dropped, in any order —
    EDGAR writes a person 'BUFFETT WARREN E', the block 'Warren E. Buffett'."""
    a, b = normalize_entity_name(block_name).split(), normalize_entity_name(edgar_name).split()
    return bool(a) and sorted(a) == sorted(b)


#: Item 5 of a pre-2024 text cover, up to Item 6: "…ceased to be the
#: beneficial owner of more than five percent…, check the following [X]."
_ITEM5_TEXT = re.compile(r"Item\s*5\b.{0,80}?(?:5|five)\s*(?:%|percent)\s*or\s*less(.{0,700}?)Item\s*6\b",
                         re.IGNORECASE | re.DOTALL)
#: the box ticked: "[X]", "☒", "x ." — or Wingdings' check glyphs as Latin-1
#: decodes them ("ý", "þ"; Fir Tree's 13G/A of 2019); "[ ]", "[_]" and a bare
#: "check the following." are not ticked
_TICKED = re.compile(r"check\s+the\s+following[\s:.]*(?:box)?[\s:.]*[\[(]?\s*(?:x|☒|☑|✓|✔|■|ý|þ)(?![a-z])",
                     re.IGNORECASE)
#: or said outright, without the "If": "This statement is being filed to
#: report the fact that … has ceased to be …" (T. Rowe Price's covers)
_STATED = re.compile(r"(?<![Ii]f )\bthis\s+statement\s+is\s+being\s+filed\s+to\s+report\s+the\s+fact\s+that.{0,160}?"
                     r"(?:has|have)\s*(?:\(have\)\s*)?ceased", re.IGNORECASE)


def _parse_final_report_from_text(text: str) -> bool | None:
    """True when a text/HTML 13G cover ticks Item 5 (the filer's last report,
    findings item 35); None when the box is empty or not found."""
    import html as _html
    flat = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", text or "")))
    m = _ITEM5_TEXT.search(flat)
    return True if m and (_TICKED.search(m.group(1)) or _STATED.search(m.group(1))) else None


def _final_since(final, event_date: str | None, file_date: str | None) -> str | None:
    """The day a final report speaks for: a 13D's stated day of ceasing, else
    the cover's date of event, else the filing date; None when not final."""
    if not final:
        return None
    return final if isinstance(final, str) else (event_date or file_date)
