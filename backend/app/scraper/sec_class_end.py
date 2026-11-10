"""When the shares a Schedule 13D/G statement is about ceased to exist (findings item 99).

A 13D/G statement says "I hold N % of this issuer's common stock" and stays the
holder's word until a newer statement replaces it. After a takeover nobody files
again: the shares are gone, the acquirer owns the company, and the duty to file
ends with the registration. So silence proves nothing either way (Roche's 100 %
of Foundation Medicine, 2018, is still true; SoftBank's 83.8 % of Sprint ended
when Sprint merged into T-Mobile on 2020-04-01), and the graph kept every such
statement live — Sprint's, LinkedIn's (Microsoft, 2016), NV Energy's (Berkshire
Hathaway Energy, 2013) holders added up as current owners.

The evidence that a class ended is on the issuer's own EDGAR list:

- **Form 25-NSE under Rule 12d2-2(a)** — the exchange removes the class because
  it was redeemed, retired or exchanged into other securities (a merger: Sprint's
  "Common Stock", 2020-04-01, 12d2-2(a)(3)). Rule 12d2-2(b) is a removal for
  failing the listing rules; the stock lives on over the counter, so it is no end.
- **Form 15 for a common/ordinary class with at most two holders of record** —
  the acquirer (and the depository) are all that is left (Foundation Medicine's
  15-12B, "Two"; Dreyer's 15-12G, "-0-"). A Form 15 with "fewer than 300"
  holders is a company going dark: the shares still exist, so it is no end.

The form must name an equity class: a Form 15 or 25 for notes, preferred stock,
warrants or a savings plan (Sprint's 2020 Form 15 covered only its notes;
WesBanco's 2025 one its preferred) says nothing about the common stock.

Two guards keep a class end that is no end of the holdings:

- **A successor issuer.** An 8-K12B / 8-K12G3 within 30 days registers a successor
  that took the class over — a holding-company reorganisation converts every
  share 1:1 (ONEOK, 2026-09; the holders keep their stake) and Sirius XM's 2024
  merger ran through it too. The forms cannot tell those apart, so neither ends
  here; the issuer's own proxy table (findings item 86) can.
- **Still a 13D/G subject.** A new 13D or 13G filed on the issuer after the end,
  or an amendment more than 60 days after it — and after the next February 20,
  when the annual 13G/As (the holders' exits) are in — means its shares are still held and reported (a multi-class
  issuer that retired one class: Constellation Brands' Class B, 2022; Liberty
  Media's Braves tracking stock, 2023; one iShares fund of hundreds).

A fund trust (it files N-CEN / N-CSR / N-PORT) is skipped: a 25-NSE retires one
of its hundreds of series, and a statement does not say which one it is about.

Nothing here writes: `class_ends` answers what EDGAR says, and
`sec_writer.close_at_class_end` applies it.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, timedelta

from app.scraper import sec_edgar

log = logging.getLogger(__name__)

#: a 13D/G on the issuer filed this many days after the class end — or up to
#: the next SUBJECT_GRACE_FEB, whichever is later — still counts as the holders'
#: exit filings (a final 13G/A after the merger), not as proof the issuer is
#: still held. The annual 13G/A is due 45 days after the year's end: Akero's
#: holders filed theirs on 2026-02-10…17 after its 25-NSE of 2025-12-09.
SUBJECT_GRACE_DAYS = 60
SUBJECT_GRACE_FEB = "02-20"
#: a successor registration (8-K12B / 8-K12G3) this close to the end means the
#: class was taken over by a successor issuer, not ended
SUCCESSOR_WINDOW_DAYS = 30

_FORM15 = ("15-12B", "15-12G", "15-15D")
#: the exchange's notice only: a Form 25 the issuer files itself is a voluntary
#: withdrawal (Rule 12d2-2(c) — an exchange switch), never an end
_FORM25 = ("25-NSE",)
_SUBJECT = re.compile(r"^(SC |SCHEDULE )?13[DG](/A)?$")
#: forms only an investment company files: a fund trust lists hundreds of series,
#: and a 25-NSE retires one fund (iShares Trust, 2026: ten ETFs) — no end of the
#: trust's other series, and a statement does not say which series it is about
_FUND_FORMS = re.compile(r"^(N-CEN|N-CSRS?|N-PORT|NPORT-P|N-Q|N-1A|485BPOS)")

# what is NOT the voting equity a 13D/G is about, removed before looking for it
_NOT_EQUITY = re.compile(
    r"depositary shares,? each representing [^,;]*preferred[^,;]*"
    r"|(depositary shares[^,;]*?)?preferred (stock|shares|securities|units?)[^,;]*"
    r"|redeemable[^,;]*units?[^,;]*"
    r"|\bunits?,? each consisting of[^;]*"
    r"|warrants? to (purchase|acquire)[^,;]*|rights? to purchase[^,;]*|purchase rights"
    r"|plan interests[^,;]*|interests under the [^,;]*plan[^,;]*"
    r"|\bnotes?\b[^,;]*|debentures?[^,;]*|debt securities[^,;]*",
    re.I)
_EQUITY = re.compile(
    r"common|ordinary shares?|american depositary|\bads\b|limited partner"
    r"|shares of beneficial interest|\bshares\b|all securities",
    re.I)
_WARRANT_FIRST = re.compile(r"\s*(redeemable |public )?(warrants?|rights?)\b", re.I)
_HOLDER_WORDS = {"zero": 0, "none": 0, "nil": 0, "one": 1, "two": 2}


def is_equity_class(text: str | None) -> bool:
    """True when the class a Form 15/25 names includes the issuer's voting
    equity: common or ordinary shares, ADSs, LP units. A list that names the
    common stock beside a preferred series counts ("Common Stock; Depositary
    Shares, each representing a 1/40th … Preferred"); one of only notes,
    preferred stock, warrants, plan interests or a SPAC's units (a share and
    a warrant, split into their parts while the shares live on) does not."""
    if not text or _WARRANT_FIRST.match(text):
        return False
    return bool(_EQUITY.search(_NOT_EQUITY.sub("", text)))


def few_holders(text: str | None) -> bool:
    """True when a Form 15's "approximate number of holders of record" is at
    most two ("one", "Two", "-0-", "None", "1*"): only the acquirer is left.
    Anything else — "fewer than 300", "approximately 150" — is going dark."""
    if not text:
        return False
    t = text.lower().split(":", 1)[-1].replace("-", "").strip(" :.\xa0*")
    m = re.match(r"(\d+|zero|none|nil|one|two)\b", t)
    if not m:
        return False
    v = m.group(1)
    return (int(v) if v.isdigit() else _HOLDER_WORDS[v]) <= 2


def class_ceased(rule: str | None) -> bool:
    """True for a 25-NSE under Rule 12d2-2(a): the class was redeemed, retired
    or exchanged. 12d2-2(b) (delisted for the listing rules) and (c) (the
    issuer's own withdrawal) leave the shares in existence."""
    return bool(rule) and "12d2-2(a)" in rule.replace(" ", "")


def form15_fields(text: str) -> tuple[str | None, str | None]:
    """(the class it covers, the holders of record) from a Form 15's text:
    the class sits between the address block and "(Title of each class …";
    the holders after "number of holders of record"."""
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))
    low = t.lower()
    cls = None
    i = low.find("(title of each class")
    if i > 0:
        head = t[:i]
        j = head.lower().rfind("offices)")
        cls = (head[j + 8:] if j >= 0 else head[-160:]).strip(" -\xa0") or None
    holders = None
    k = low.find("number of holders of record")
    if k > 0:
        holders = t[k + 27:k + 120].strip()
    return cls, holders


def form25_fields(xml: str) -> tuple[str | None, str | None]:
    """(the class, the rule) from a 25-NSE's primary_doc.xml."""
    c = re.search(r"<descriptionClassSecurity>(.*?)</descriptionClassSecurity>", xml, re.S)
    r = re.search(r"<ruleProvision>(.*?)</ruleProvision>", xml, re.S)
    return (html.unescape(c.group(1).strip()) if c else None), (r.group(1).strip() if r else None)


def _days(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _grace_until(day: str) -> str:
    """The last day a 13D/G on the issuer still reads as an exit filing."""
    feb = f"{day[:4]}-{SUBJECT_GRACE_FEB}"
    if feb <= day:
        feb = f"{int(day[:4]) + 1}-{SUBJECT_GRACE_FEB}"
    return max(_days(day, SUBJECT_GRACE_DAYS), feb)


def class_ends(cik: str, submissions: dict | None = None) -> list[dict]:
    """The days an equity class of `cik` ended, oldest first, as EDGAR states
    them: [{"date", "form", "class", "url"}] — only ends that pass both guards
    (see the module docstring). One request for the submissions (unless
    given) and one per Form 15 / 25 found; a document that cannot be read is
    skipped, an unreadable submissions file raises (the caller decides)."""
    subs = submissions if submissions is not None else sec_edgar._submissions(cik)
    recent = (subs.get("filings") or {}).get("recent") or {}
    rows = list(zip(recent.get("form") or [], recent.get("filingDate") or [],
                    recent.get("accessionNumber") or [], recent.get("primaryDocument") or []))
    if not rows:
        return []
    if any(_FUND_FORMS.match(f) for f, _, _, _ in rows):
        return []
    successors = sorted(d for f, d, _, _ in rows if f.startswith(("8-K12B", "8-K12G3")))
    # an amendment is how a holder files its exit after a merger, so it is
    # judged after the grace; a NEW statement means a new holder of the issuer
    amendments = sorted(d for f, d, _, _ in rows if _SUBJECT.match(f) and f.endswith("/A"))
    originals = sorted(d for f, d, _, _ in rows if _SUBJECT.match(f) and not f.endswith("/A"))
    base = f"{sec_edgar.ARCHIVES_URL}/{sec_edgar._cik_int(cik)}"
    ends = []
    for form, day, acc, doc in rows:
        if form not in _FORM15 and form not in _FORM25:
            continue
        folder = f"{base}/{acc.replace('-', '')}"
        try:
            if form in _FORM15:
                url = f"{folder}/{doc}"
                cls, holders = form15_fields(sec_edgar._get_text(url))
                ended = is_equity_class(cls) and few_holders(holders)
            else:
                url = f"{folder}/primary_doc.xml"
                cls, rule = form25_fields(sec_edgar._get_text(url))
                ended = is_equity_class(cls) and class_ceased(rule)
        except Exception as exc:  # noqa: BLE001 — one unreadable form is no end
            log.warning("SEC: could not read %s %s of CIK %s: %s", form, acc, cik, exc)
            continue
        if not ended:
            continue
        if any(_days(day, -SUCCESSOR_WINDOW_DAYS) <= d <= _days(day, SUCCESSOR_WINDOW_DAYS)
               for d in successors):
            log.info("SEC: CIK %s %s %s — a successor issuer took the class over; no end", cik, form, day)
            continue
        if any(d > _grace_until(day) for d in amendments) or any(d > day for d in originals):
            log.info("SEC: CIK %s %s %s — 13D/G filed on it since; no end", cik, form, day)
            continue
        ends.append({"date": day, "form": form, "class": cls,
                     "url": f"{folder}/{acc}-index.htm"})
    return sorted(ends, key=lambda e: e["date"])
