# SEC EDGAR Scraper — Research & Implementation Notes

## Overview

The SEC EDGAR scraper collects two types of data for US-listed companies:

1. **Large shareholders** — investors who filed SC 13D or SC 13G disclosures (>5% ownership)
2. **Executives and directors** — officers and board members from Form 3/4 insider reports

No API key is required. All endpoints are public. The SEC requires a descriptive
`User-Agent` header identifying the application and a contact email.

---

## APIs Used

| Endpoint | Purpose |
|---|---|
| `https://www.sec.gov/files/company_tickers.json` | CIK lookup for all listed companies |
| `https://data.sec.gov/submissions/CIK{cik}.json` | Company filing index (recent filings metadata) + `formerNames` |
| `https://www.sec.gov/cgi-bin/browse-edgar?output=atom` | SC 13D/13G filing list per company (Atom feed) |
| `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/` | Individual filing documents |
| `https://efts.sec.gov/LATEST/search-index` | Full-text search (fallback for company CIK lookup only) |

**Rate limit:** 10 requests/second. The scraper sleeps 0.12 s between every request.

## Former names → aliases

The submissions JSON carries `formerNames` (prior registered names under the **same
CIK** — e.g. *Meta Platforms, Inc.* ⟵ "Facebook Inc"). EDGAR has no distinct-entity
successor link, so these are a rename of one legal entity, not a `SUCCEEDED_BY`
edge. `fetch_former_names(cik)` reads them and `_upsert_entity_by_name` folds them
into the entity's `aliases` + `search_text` (merged, never clobbering existing
aliases), so the old name is searchable ("Facebook" → Meta Platforms).

---

## Finding a Company's CIK

Every EDGAR entity has a Central Index Key (CIK) — a zero-padded 10-digit integer.
It is required to locate filings.

**Step 1 — tickers file (preferred):**
`company_tickers.json` maps ~10,000 listed companies to their CIK, ticker, and
registered name. It is a flat JSON object keyed by an arbitrary integer index:

```json
{
  "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
  "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
  ...
}
```

Matching is done by normalising both the query and the `title` field: lowercase,
strip punctuation and legal suffixes (Inc, Corp, LLC, etc.), then compare.
Exact matches win; prefix matches are the fallback.

**Step 2 — EFTS full-text search (fallback):**
For companies not in the tickers file (private companies, foreign names), the
EFTS endpoint can search 10-K or DEF 14A filings:

```
GET https://efts.sec.gov/LATEST/search-index?q="Company+Name"&forms=10-K
```

**Note:** This endpoint is unreliable for SC 13G/13D searches — it returned HTTP
500 and long timeouts during development. It is only used for CIK lookup (10-K
and DEF 14A filings), where it is more stable.

---

## Large Shareholders — SC 13D / SC 13G

### What these forms are

Any investor who acquires more than 5% of a public company's shares must file
with the SEC within 10 days:

- **SC 13G** — passive investor (no intent to influence management). Typically
  institutional investors: mutual funds, index funds, ETFs.
- **SC 13D** — active investor (may seek board seats, push for changes).
  Typically activist funds or founders with large stakes.
- **/A suffix** — amendment to a previous filing (e.g. SC 13G/A).

Both forms have a standardised cover page. **Item 13** is mandatory and states
the percentage of shares beneficially owned.

### The form rename (and the two years it cost us)

EDGAR's browse `type=` is a **prefix match**. The December-2024 beneficial-ownership
modernization renamed the schedules from `SC 13G` to `SCHEDULE 13G`, and the two sets
are disjoint — so a request for `type=SC 13` silently stopped returning anything filed
after early 2024. Measured on Apple (CIK 320193): `SC 13` gave a newest filing of
**2024-02-14** while `SCHEDULE 13` gave 2026-04-29.

We now ask for **`type=SC`**, which spans both eras. The `SC TO-*` / `SC 14*` noise it
also matches is dropped by the `"13" not in form_type` test before any request is spent.

### Structured filings (schema X0202)

The same modernization made these filings machine-readable:
`{ARCHIVES_URL}/{cik}/{accession}/primary_doc.xml`. The **subject's** CIK works in that
path, so it is built from the Atom feed alone — no index-page fetch, which makes the
modern path *cheaper* than the legacy one (one request per filing instead of two).

**There are two schemas, one per schedule**, and they spell the same facts differently:

| | Schedule 13D | Schedule 13G |
|---|---|---|
| person container | `reportingPersonInfo` | `coverPageHeaderReportingPersonDetails` |
| issuer CIK | `issuerCIK` | `issuerCik` |
| percent | `percentOfClass` | `classPercent` |
| aggregate | `aggregateAmountOwned` | `reportingPersonBeneficiallyOwned…Shares` |
| person CIK | `reportingPersonCIK` | absent — name only |

`_parse_13dg_xml` picks the layout by **which container is present**, not by the form
string or namespace URI, so a schema bump that keeps the shape keeps working. Written
against 13G names alone — as `_parse_holding_filing` originally was — a reader returns
`None` for every 13D without saying so.

Two traps worth naming:

* **Scope every per-person read to that person's element.** The same tag recurs once per
  reporting person *and* again under `items` with a different value. Wellington's Nasdaq
  13G/A carries 5.4 / 5.4 / 5.4 / 5.1 on the cover and 5.36 below it; a document-wide
  `.//` lookup hands all four members 5.4.
* **Absent is not zero.** `_split_stake` reads "no sole-dispositive row" and "sole
  dispositive of nothing" as different facts, so `_xml_num` returns `None`, not 0.

Numbers appear both as `0` and as `1598232.00` — parse through `float()`.

The XML has no shares-outstanding field. Prefer the figure the filer states in
`commentContent` / `comments`; derive `aggregate ÷ percent` only as a fallback, since
`percentOfClass` is often two significant figures.

### The counts, not only the quotient

A share count is what a filing *states*; a percentage is a division we perform
against a denominator that moves. So `shares` and `shares_outstanding` are stored
on the OWNS edge beside `stake_percent`, and the pop-up shows them, because a
quotient alone cannot be rechecked or recomputed.

What that makes visible, on AB InBev:

```
Altria   159,121,937 of 1,965,328,900  = 8.10%   (2025 filing)
Bevco    102,862,718 of 1,730,242,027  = 5.94%   (2020 filing)
```

Bevco's holding has not changed since 2020; it reads 5.9% only because it is
divided by a five-year-old total. Against the current one it is 5.23%. Without
the counts that is invisible — the two percentages look equally current.

`_shares_held` takes **dispositive** power, not voting: what a filer can sell is
what it owns. Sole where it has any, else the shares it disposes of jointly (BRC
can sell nothing alone, but the Stichting it co-owns holds 771,096,582 — a real
number where `stake_percent` must say `None`), and the reported aggregate only
when neither row is given. Zero is not a holding: absent stays absent, because a
nil position and an unstated one are different facts.

Form 3/4 states an insider's holding exactly, and that count is now kept too —
it previously decided whether to write an edge and was then discarded.

`voting_shares` is the count behind `voting_power_pct` — row 11, the number that
percentage is computed from. It carries the same caveat as the percentage and for
the same reason: it belongs to the **group** and is repeated verbatim by every
member (Wellington's four blocks all report one aggregate), so it may be shown but
**never summed** across owners. It is `None` for a lone filer, who has no bloc —
not zero, and not its own holding restated. On a voting group's own edge it is the
one place the number is not a repetition: there it is the group's, once.

### A percent of *what*

Every 13D/G percentage is a **percent of a class**, and a company with several
classes has several denominators. Grupo Televisa's filers report 22.3% of its
"Series A Shares; Series B Shares; Dividend Preferred Shares" beside 9.7% of its
"Certificados de Participacion Ordinarios (CPOs) and Global D Shares" — different
instruments, and adding them gave the company **115.9% of itself**.

The structured XML states it as `securitiesClassTitle`; pre-2024 covers carry it
next to the issuer ("… (Title of Class of Securities)"), which
`_parse_class_title_from_text` reads. Either way it is stored as `share_class` on
the OWNS edge, and `_ownership_summary` groups by it: when the named classes
disagree, `disclosed_pct` becomes **null** and `by_class` carries a total per
security, because no single number is true.

Titles are compared by their **component set** rather than as strings — filers
describe one security many ways (`Common Stock` vs `Common Stock, par value
$0.0001 per share`; `Series A Shares ("A Shares"), Series B Shares` vs
`Series A Shares; Series B Shares`). Where normalisation can't be sure (Televisa's
CPOs appear in both Spanish and English), it **over-splits deliberately**: an
extra bucket costs a slightly noisier breakdown, whereas a wrong merge resurrects
the bug.

### Group membership without prose

A filing group's members are structured, in two complementary places:

* **XML era:** one `reportingPersonInfo` / `coverPageHeaderReportingPersonDetails` block
  per member. 13D blocks carry CIKs; 13G blocks do not.
* **Pre-2024:** the SGML submission header lists `GROUP MEMBERS:`, one per line — read
  from `{accession}-index-headers.htm` (≈4 KB) rather than the full submission (287 KB
  for AB InBev's). Parse the raw lines: flattening whitespace first makes one match
  swallow the entire header as a single "name".

Both are returned as `group_members` and **not written to the graph**: membership is not
ownership, and the SGML names carry no CIK, so persisting them would mint duplicate
low-confidence nodes beside the ones their own filings already created.

**The limitation:** a filer who files alone and disclaims group membership appears in
neither source. Altria's AB InBev schedule is the example — it names its counterparties
(BEVCO and the Stichting) only in Item 6 prose. So the structured data gives the
families' side of that bloc completely and Altria's side not at all.

### Finding filings for a company

The EDGAR company browse endpoint with `output=atom` returns an Atom XML feed of
all SC 13 filings where a given company is the **subject/issuer**:

```
GET https://www.sec.gov/cgi-bin/browse-edgar
    ?action=getcompany
    &CIK={issuer_cik}
    &type=SC+13
    &owner=include
    &count=30
    &output=atom
```

The `owner=include` parameter is critical — without it, EDGAR only returns
filings made BY the company, not filings ABOUT it.

The feed is returned most-recent-first, which enables deduplication: process
entries in order and skip any investor already seen (SC 13G/A amendments share
the same investor but have different accession numbers).

**A filing reporting nothing is an exit, not a 0% holding.** When every power row
and the class percentage are zero, the filer has dropped below the 5% threshold —
the CIK is recorded in `closed_since` and deliberately *not* marked as seen, so
that investor's next (older) filing is still read and emitted with `until` set to
the zero filing's date. The position stays in the timeline instead of appearing as
a live "owns 0.0%" edge, and instead of vanishing as if it had never existed.

The Vanguard Group's January-2026 internal realignment is what surfaced this: its
subsidiaries now report beneficial ownership separately, so the parent files
amendments reporting 0 for every row, and eighteen of those went into the graph as
live 0.0% holdings. `fetch_filer_holdings` (the filer side) had always read a zero
this way; `fetch_ownership_filings` (the issuer side) never did — the same fact,
two code paths, one of them taught.

The test is `not pct and not voting`, not `not pct` alone: a 13D group member can
hold nothing individually while voting a real bloc (BRC reports a null stake beside
the Stichting's 52.3%), and reading that as an exit would delete the voting group.

**A holding below the precision floor has no percentage, not a zero one.** Every
stake is `shares / shares_outstanding` rounded to four decimals, and a real holding
can round to `0.0`: six Apple directors hold 1,139 shares each, which is 0.0000076%
of ~15bn. Stored as `0.0` that says "owns nothing" — false, and indistinguishable
from the exit above. `_pct_of` returns None in that case and the share count carries
the fact instead; `unknown_owners` in the ownership summary already accounts for
owners whose share cannot be put in a number. A genuine zero (nothing held) still
returns `0.0`, or the exit rule would stop firing. All three places that divided
shares by shares-outstanding now share that helper — they used to round
independently, which is the sibling-path shape this scraper keeps paying for.

### Key XML namespace gotcha

The Atom feed uses `xmlns="http://www.w3.org/2005/Atom"` as the **default**
namespace. Python's `xml.etree.ElementTree` requires all element lookups to use
the explicit namespace — including the inner elements of `<content type="text/xml">`,
which also inherit the Atom namespace even though they look like plain XML:

```python
ns = {"a": "http://www.w3.org/2005/Atom"}
content.find("a:filing-href", ns)   # correct
content.find("filing-href")          # always returns None
```

### The filing agent problem

The leading 10 digits of an EDGAR accession number are the **submitter's CIK**,
not necessarily the investor's CIK. Large investors routinely use filing agents
(Toppan Merrill, Donnelley, etc.) to submit their EDGAR documents. In those
cases the accession number starts with the agent's CIK, not the investor's.

Example: Bezos's SC 13G/A for Amazon has accession `0001104659-24-115906`.
CIK `0001104659` belongs to Toppan Merrill, not Bezos.

**Solution:** Fetch each filing's index page (the `filing-href` URL from the
Atom feed) and parse the HTML to find the `(Filed by)` section:

```html
<span class="companyName">BEZOS JEFFREY P (Filed by)
  CIK: <a href="...CIK=0001043298...">0001043298</a>
</span>
```

This reliably gives both the investor name and their real CIK.

### The wrong-subject problem (issuer verification)

On the structured path this is `_xml_issuer_matches`, and it is **two tiers**: reject only
when the stated `issuerCIK` differs from the company *and* the name fails token matching.
Neither alone is safe. The CIK is typed by the filer's agent — the same keyboard that
produced the Embraer mis-file — while the name can be years stale (Wellington's filing
carries the correct Nasdaq CIK beside "The NASDAQ OMX Group, Inc."), and a rename can
leave no shared token at all (a filing saying "Google Inc." against a company we know as
"Alphabet"). A filing that yields neither parsed XML nor a checkable cover page is
**dropped**, not admitted: on a modern index page there is no `SC 13`-typed `.htm` for the
legacy regex to find, so admitting the unverifiable would reopen the hole this closed.


The filing agent problem has an uglier sibling: **the metadata can name the
wrong company entirely.** A company's browse feed and submissions JSON list
filings it is *involved in* — including filings it FILED about a different
issuer. Worse, the agent can mis-fill the SGML header: Embraer's SC 13D/A about
**Eve Holding** carried `SUBJECT COMPANY: EMBRAER S.A.` in the header, so the
index page, the SGML header and the accession prefix all pointed at the wrong
company. The graph gained "Embraer Aircraft Holding owns 83% of Embraer"; the
real statement was 83% of Eve Holding. The company also appeared in its own
executive list as a "Director" via its Form 4s about Eve.

**Solution — verify against the document, not the metadata:**

* **SC 13D/G:** the cover page's `Xxx (Name of Issuer)` line is the only field
  on the filing that reliably states whose shares are reported. It is parsed
  from the primary document (fetched for every accepted filing — this also
  yields stake % and reporter type for all rows, not just the former top five)
  and compared by significant-token overlap against **every name the CIK has
  filed under** (current + EDGAR `formerNames` — a rename keeps the CIK, and a
  scrape of "Meta Platforms" must not throw away a cover saying "Facebook,
  Inc"). Tokens are diacritic-folded ("Nestlé" ≡ "Nestle") and legal-form noise
  (`S.A.`, `Inc`, `NV`, …) carries no identity. Only a *positive mismatch*
  rejects: an unparseable cover keeps the filing, and so does a name list with
  no identity left after noise removal.
* **Form 3/4:** exact, no fuzz — the XML states `issuer/issuerCik`, which is
  compared numerically (padding-proof) to the scraped CIK.

### Beneficial ownership is not a shareholding

SEC "beneficial ownership" is about **power, not property**: you beneficially
own a share if you can vote it *or* dispose of it. Members of a voting group
must each report the **whole group's** shares, so summing row-13 percentages
across the members counts the same shares once per member.

AB InBev summed to **109.9%** that way. Altria's SC 13D/A cover decomposes it:

```
Sole Voting Power                  0
Shared Voting Power    1,020,598,157   <- the Voting Agreement bloc
Sole Dispositive Power   159,121,937   <- what Altria can actually sell
Shared Dispositive Power           0
Percent of Class (row 13)     51.7%    <- the bloc, not Altria's stake
```

The bloc is Altria + Bevco (Santo Domingo family) + Stichting AB InBev (the
founding families, owned jointly by BRC and EPS). Their real holdings —
159,121,937 + 102,862,718 + 758,613,502 — sum to exactly the 1,020,598,157 all
three report.

`_own_stake_and_voting` therefore splits the cover into two numbers:

| cover shape | `stake_percent` | `voting_power_pct` |
|---|---|---|
| sole dispositive ≥ shared voting (lone filer) | row 13, unchanged | `None` |
| 0 < sole dispositive < shared voting (group member) | sole dispositive ÷ shares outstanding | row 13 (the bloc) |
| sole dispositive = 0 (holds only jointly) | `None` | row 13 |
| denominator not stated | `None` | row 13 |

Two deliberate `None`s. A purely joint holder like BRC — which can dispose of
nothing alone, its shares sitting in the Stichting it co-owns with EPS — would
read as "owns nothing" if given 0.0, the opposite of the truth. And keeping the
bloc figure as a stake when the denominator is unknown is precisely what
produced the >100% sums. Unknown is stated as unknown; `unknown_owners` in the
ownership summary already accounts for it.

**Consequence to expect:** a company controlled through a bloc will show a
*lower* disclosed total than before, not a higher one — AB InBev goes from an
impossible 109.9% to a conservative 13.95%, with the 51.7% bloc shown as voting
power. Attributing the group's holding to its members individually needs the
group modelled as a group; see the voting-group note in `deduplication.md`.

### Parsing stake percentages

The primary filing document (linked from the index page's document table) contains
the SC 13D/13G cover page. Item 13 states the ownership percentage in a
standardised sentence:

```
PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW 9    7.46%
```

Three complications:

1. **HTML encoding** — many filings are HTML with `&nbsp;` between words,
   which breaks naive whitespace-based regex. Strip HTML tags and decode
   entities with `html.unescape()` before matching.

2. **Digits in context** — patterns like `[^\d%]{0,300}?` to span from the
   label to the value fail when "ROW 9" appears in between (the digit 9 stops
   the match). Use `.{0,300}?` or match the exact standard phrase instead.

3. **File formats** — some filings (e.g. older BlackRock submissions) use plain
   `.txt` instead of `.htm`. The document table regex must accept both extensions.

Winning pattern that handles all three:

```python
r'percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+row\s+\d+\s+(\d{1,2}\.?\d*)\s*%'
```

---

## Quarterly Holdings — Form 13F

Every institutional manager over $100M files a quarterly report of what it
holds. Since 2013 the information table is structured XML — issuer name, CUSIP,
dollar value, share count, voting authority — so unlike the pre-2024 schedules
there is no prose to parse at all.

**Why it matters: 13D/G only exists above 5%.** Nvidia's 0.9% of SpaceX or
PIF's 1.2% can never appear in a schedule; every one of them is in somebody's
13F. `manage.py sec-13f <company>` asks the issuer-side question — who holds
this company — via EDGAR full-text search (`efts.sec.gov`), which indexes the
info-table documents themselves.

The rules the implementation lives by (`fetch_13f_holders` / `run_sec_13f`):

- **CUSIP is the join key, harvested rather than licensed.** Info-table rows
  identify issuers only by CUSIP plus a free-text name typed by each filing
  agent ("APPLE INC" / "Apple Inc"). The CUSIP comes from the 13D/G schedules
  we already parse (`issuerCusipNumber`) and from the first name-verified 13F
  rows, and is stamped on the entity fill-if-missing. One company legitimately
  has several CUSIPs (one per share class — SpaceX's filers report 84615Q103
  beside 69608A108), so a name-matching run must not lock onto the first CUSIP
  it sees, and CUSIP is **not** a dedup identifier.
- **Percentages are computed, never transcribed.** 13F reports counts and
  dollars; stake = shares ÷ shares outstanding through `_pct_of` with its
  precision floor. The denominator has two layers: the XBRL `companyconcept`
  endpoint (padded CIK), and — because a MULTI-class issuer's per-class cover
  facts are dimensioned and never reach the aggregated endpoints — a fallback
  that reads the newest 10-Q/10-K inline-XBRL cover and sums the classes.
  SpaceX's 13.18bn total (7.70bn Class A + 5.49bn Class B, stated to the share
  on its 10-Q cover) only exists via that fallback; it is where the press got
  the number too. Restricted to 10-Q/10-K deliberately: a 20-F filer's cover
  counts UNDERLYING shares while its 13F filers report ADRs/GDRs — Televisa's
  GDR bundles 585 of them, so that division is wrong by 585×. An issuer with
  neither keeps honest counts and no percentage.
- **Option rows are not holdings.** 20 of BNP's 21 real SpaceX rows carry
  `putCall` — shares *underlying* contracts, owned by nobody. Skipped, as are
  `PRN` (debt principal) rows.
- **votingAuthority is deliberately not written.** It states the manager's
  authority over its own held shares — a different fact from
  `voting_power_pct`, which is a bloc's share of the issuer's votes. Mapping
  one onto the other would mark every index fund as a voting bloc.
- **The search is windowed to the current period.** Full-text search is
  relevance-ordered, NOT date-ordered — unwindowed, a widely-held issuer's
  sample mixes filings from years apart, and the first Televisa run wrote
  "current" positions with as-of dates spanning 2022–2026. `--window-days`
  (default 135 — one quarter plus the 45-day deadline) keeps only filings
  about the current reporting period; `0` disables it for a deliberate
  historical read.
- **Truncation is explicit.** `--limit` (default 100) caps the filings read
  within the window. For SpaceX (91 filings) that is complete; for a mega-cap
  it is a sample, and the command prints fetched-vs-total so nobody mistakes
  one for the other. Amendments (13F-HR/A) restate the whole table, so only
  the newest accession per filer is read.
- **Every SEC-written edge and claim carries `filing_type`** — "13F", "13G/A",
  "Form 4" — via `_short_form()`, which collapses both eras' spellings
  ("SCHEDULE 13G/A" / "SC 13G/A" → "13G/A"). GLEIF RR edges carry "RR" and UK
  PSC "PSC" for the same reason: the source names the register, the filing
  type names the rulebook, and the Sources panel shows "SEC EDGAR · 13F".
- **Values are dollars as filed.** The SEC dropped report-in-thousands in
  2023, but some filers still do it; values are stored as the source gave them
  (source fidelity beats a guessed correction).

## Executives — Form 3 / Form 4

### What these forms are

Any officer, director, or 10%-or-greater shareholder of a public company must
report their trades and holdings:

- **Form 3** — initial statement of beneficial ownership (filed when someone
  first becomes an insider)
- **Form 4** — report of a change in ownership (filed within 2 business days
  of each trade)

Both forms are structured **XML** with a fixed schema. This is far more reliable
than scraping DEF 14A proxy HTML, which is narrative and inconsistently formatted.

### Fetching Form 3/4 filings

The company's submissions JSON lists all its recent filings:

```
GET https://data.sec.gov/submissions/CIK{cik}.json
```

Response structure (relevant fields):

```json
{
  "filings": {
    "recent": {
      "form":            ["4", "4", "3", "4/A", ...],
      "accessionNumber": ["0001234567-24-000001", ...],
      "primaryDocument": ["form4.xml", "xslF345X06/form4.xml", ...]
    }
  }
}
```

Filter for `form` values `3`, `4`, `3/A`, `4/A`. Deduplicate by the filer's
CIK (first 10 digits of accession, stripped of dashes) to get one entry per
insider — the most recent filing has their current title.

### Primary document path caveat

Some entries in `primaryDocument` are prefixed with an XSLT stylesheet path,
e.g. `xslF345X06/form4.xml`. When fetched, this path serves an HTML-rendered
version, not the raw XML. Strip any leading `something/` prefix to get the
actual filename:

```python
primary_doc = primary_doc.split("/")[-1] if "/" in primary_doc else primary_doc
```

### Archive URL

Form 3/4 filings are stored under the **issuer's CIK** in EDGAR Archives, even
when the accession number's leading digits are a filing agent's CIK:

```
https://www.sec.gov/Archives/edgar/data/{issuer_cik_int}/{accession_no_dashes}/{primary_doc}
```

Use the issuer CIK (the company being investigated), not the CIK embedded in the
accession number.

### XML parsing

Key fields in Form 3/4 XML:

```xml
<reportingOwner>
  <reportingOwnerId>
    <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
  </reportingOwnerId>
  <reportingOwnerRelationship>
    <isOfficer>1</isOfficer>
    <isDirector>0</isDirector>
    <officerTitle>Chief Executive Officer</officerTitle>
  </reportingOwnerRelationship>
</reportingOwner>
```

Names are stored as `LAST FIRST [MIDDLE]` in all caps. The scraper converts
them to `First [Middle] Last` in title case.

---

## Request Budget per Company

| Step | Requests |
|---|---|
| Tickers file (cached per process) | 1 (first company only) |
| Company browse Atom feed | 1 |
| Filing index pages (up to 20 investors) | up to 20 |
| Primary documents for stake % (top 5) | up to 5 |
| Submissions JSON for executives | 1 |
| Form 3/4 XML documents (up to 25 insiders) | up to 25 |
| **Total (typical)** | **~35–40** |

At 0.12 s per request this takes roughly 5–8 seconds per company, in addition
to the Wikidata scrape.

---

## Limitations

- **Only US public companies** are on EDGAR. Foreign-listed companies (Volkswagen,
  Samsung, Nestlé, Alibaba, etc.) require a different data source.
- **SC 13G/13D covers >5% stakes only.** Smaller institutional positions are
  disclosed in Form 13F instead — covered by `manage.py sec-13f`, see
  *Quarterly Holdings* above. 13F has its own limits: quarterly with a 45-day
  lag, institutional managers over $100M only (an insider's personal stake
  never appears), and only securities on the SEC's 13(f) list.
- **Amendment deduplication is conservative.** The Atom feed with `count=30`
  may not include all historic amendments; only the most recent filing per
  investor is retained.
- **Stake percentages for the 6th+ investor are not fetched** (capped at 5 to
  limit HTTP request count). The `stake_percent` field will be `null` for those.
- **Form 3/4 titles change over time.** The scraper reads the most recent
  Form 3/4 per insider, so a person who changed roles (e.g. VP → CEO) will
  show their current title, not their title at a given point in time.
