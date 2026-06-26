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
| `https://data.sec.gov/submissions/CIK{cik}.json` | Company filing index (recent filings metadata) |
| `https://www.sec.gov/cgi-bin/browse-edgar?output=atom` | SC 13D/13G filing list per company (Atom feed) |
| `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/` | Individual filing documents |
| `https://efts.sec.gov/LATEST/search-index` | Full-text search (fallback for company CIK lookup only) |

**Rate limit:** 10 requests/second. The scraper sleeps 0.12 s between every request.

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
- **SC 13G/13D covers >5% stakes only.** Smaller institutional positions
  are not disclosed in these forms (they may appear in 13F quarterly reports,
  which are filed by the investor, not the company, and require a separate lookup).
- **Amendment deduplication is conservative.** The Atom feed with `count=30`
  may not include all historic amendments; only the most recent filing per
  investor is retained.
- **Stake percentages for the 6th+ investor are not fetched** (capped at 5 to
  limit HTTP request count). The `stake_percent` field will be `null` for those.
- **Form 3/4 titles change over time.** The scraper reads the most recent
  Form 3/4 per insider, so a person who changed roles (e.g. VP → CEO) will
  show their current title, not their title at a given point in time.
