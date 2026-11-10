# Data sources

What each source contributes and how it is read. Deep dives: [`sec_edgar_scraper.md`](sec_edgar_scraper.md) (SEC EDGAR), [`data-model.md`](data-model.md) (GLEIF sourcing, source tiers), [`scraper-plugin-guide.md`](scraper-plugin-guide.md) and [`new-source-checklist.md`](new-source-checklist.md) (adding one).

Wikidata, SEC EDGAR, and OpenCorporates run per-company via `/scraper/run-all`;
BODS (GLEIF / UK PSC) is a separate bulk dataset import. Each source has an
independent on/off toggle (`/scraper/sources`).

🧩 **Adding a source:** [`docs/scraper-plugin-guide.md`](scraper-plugin-guide.md) is a step-by-step guide (API module → source toggle → config flags → runner → endpoints → dedup) with a pre-deploy checklist. [`docs/new-source-checklist.md`](new-source-checklist.md) covers the judgement calls around it — what to take from a source, how its records find the companies already in the graph, and the mistakes already made here.

## Wikidata
Imports corporate ownership data via SPARQL. For a company it fetches subsidiaries, owners, parent and executives, and reads the item's LEI (P1278) and SEC CIK (P5531) — **as a pointer, never stored** (2026-10-29). They find the company's register node, so the item enriches the GLEIF or EDGAR node instead of drawing a second copy; and where the graph lacks the company, the register writes it from the pointer — GLEIF's API by LEI, EDGAR's submissions by CIK, under the register's own name, id and source (`app.scraper.register_pointer`). A related company (subsidiary, owner, successor) no register knows is not written. A Wikidata item is an organisation, not a legal entity, and its ids can belong to another legal entity than the node: BlackRock's Q219635 still states the pre-2024 parent's CIK and LEI, the writer stamped that CIK on the listed BlackRock, and it was renamed "BlackRock Finance, Inc." while its own filings drew a second node. So a node whose register LEI or CIK is not among the item's gets **nothing** of the item (logged; the run reports `conflicts`, and `status: conflict` when it is the searched company). That holds for a node the item's own ids found; a node found by the **name** alone whose LEI or CIK is not the item's is another company of that name, and the item's ids go to their register — refused as before only when no register knows them (J.P. Morgan Securities LLC and PLC share the key "jp morgan securities", and the second of JPMorgan Chase's two items was lost until 2026-11-01, findings item 30). A node found by the name alone that holds a register id, none of them the item's, is the item's company only on its register's word: the item's ids are asked first with that node as the candidate GLEIF or EDGAR may join, and the register's own node takes the item when it refuses; the name's node stands only when no register knows the ids. Taken on the name, Liberty Media's Q1516562 (the Nevada company's LEI) went to the Delaware filer of the same name that a Berkshire 13G had written first (findings item 50, 2026-11-04). A malformed value is dropped. For the same reason it writes **no address and no entity type** — no country, headquarters, coordinates or `type` (2026-10-27): the registers state them, and an item often describes a group rather than the legal entity (Unilever's Q157062 states both the UK and the Netherlands). It then recursively expands **down the subsidiary tree** to `depth` levels (0–3, default 2; owners/executives are recorded but not recursed). Every subsidiary the item lists is read and written (only those a register knows), in a fixed order — identified first, current before ended, then by QID — and up to 15 of them per node are followed one level further, so cost grows ~exponentially with depth. Until 2026-10-31 the 15 capped the list itself, in SPARQL's row order, which is not fixed: Alcon, Nestlé's 20th of 23 subsidiaries, was read on one run and not the next, and Siemens and Volkswagen lost 8 of 11 identified subsidiaries (findings item 24) — `depth=1` is fast (the company + its immediate relations), `depth=3` can mean thousands of Wikidata calls and hours for a large conglomerate (and risks rate-limiting). Only affects the Wikidata scrape, not the BODS bulk import. Controlled by `SCRAPER_ENABLED`.

- Reads the item **by the id the node already holds** before any name search (2026-10-31): the node's own QID, else the one item stating its CIK (`haswbstatement:P5531`), else its LEI (P1278) — still only when that item looks like a company. EDGAR's spellings ("Wells Fargo & Company/Mn", "ONEOK INC /NEW/") found nothing by label, and 14 companies Wikidata files under the node's own CIK came back `no_results` every rebuild (findings item 7). On a first scrape the node, and its CIK, often come from the SEC step that runs after Wikidata, so a Wikidata run that found nothing is repeated once after the other sources when they gave the node an id it lacked (Toast, Inc., findings item 27). Otherwise:
- Searches Wikidata by company name, picks the best-matching entity
- When the name as written finds no company, asks again **without the legal form and EDGAR's tag** ("Lone Pine Capital Llc" → Lone Pine Capital) — held to a name's standard: the item's own label must reduce to the same words, at least two of them, and exactly one company may qualify ("Inter & Co, Inc." reduces to "inter", which Inter Milan answers). The writer still refuses an item whose LEI or CIK contradicts the node's
- A person the graph already holds with a QID is read by it — only when no other person has that name (either word order); Wikidata hardly states CIKs on people, so the QID is the only id asked by
- Reads the day a company ceased (P576, at the statement's precision) as `dissolved_on` and closes what it still held — see *An entity has an end* in [`docs/data-model.md`](data-model.md)
- Writes to the DB using upsert — safe to re-run, no duplicates
- Follows at most 15 subsidiaries one level down per entity (all are written), and caps at 3 CEOs
- 400 ms delay between requests (Wikidata rate limit)
- An action-API error answer (HTTP 200 with an `error` body — `ratelimited`, `maxlag`, …) raises `WikidataApiError` instead of reading as "no hits", so run-all logs the row `failed` (2026-10-29)
- **The other way round:** `manage.py wikidata-suggestions --out DIR` reports what GLEIF and EDGAR would correct on Wikidata (LEI P1278, CIK P5531) for every node with an item — `missing` (add the node's register id; `high` confidence only when the register also agrees on the country or on the item's other id, not on the name alone), `differs` (a stale id or another legal entity's; it names whose), `unknown` (no register knows the stated id) and `several` (information). It also reports the items the scraper **refuses** — an item stating a register id of a node whose other register id it contradicts, which the scraper writes nothing of, not even the item id (BlackRock's Q219635 on the 2026-10-29 rebuild): found by asking WDQS which items state the LEIs and CIKs of the nodes without an item, tagged `link = refused`, never confident. It writes a CSV of every finding and a QuickStatements batch of the confident additions, each referenced by the register's record page and the day it was read. Read-only on both sides: a person reviews and submits (`app.scraper.wikidata_suggestions`)

## SEC EDGAR
Imports investor data from SC 13D/13G ownership filings, executive data from Form 3/4 XML, the filer's SIC code as its industry (shown as a NACE Rev. 2 sector tree — see *Industry classification* in [`docs/data-model.md`](data-model.md)), statutory subsidiary lists from the 10-K's Exhibit 21 (`manage.py sec-ex21`), acquisitions from the acquirer's own 10-K/10-Q XBRL facts — the closing date as a stated start and the price paid (`manage.py sec-acquisitions`) — private-company boards from Form D (`manage.py sec-formd`) — all manual-first — and, in every company scrape, a foreign issuer's ADS CUSIP from its depositary's Form F-6 filings, which the securities bridge turns into the issuer's LEI. A 13D/G holding ends when the shares it is about ceased to exist — a merger's Form 25-NSE, a takeover's Form 15 — read from the issuer's own list (`manage.py close-ended-classes`; `until_reason = class_ended`). A 13D/G holding of 5 % or more also ends when the issuer's newest proxy ownership table no longer names the holder (`manage.py close-absent-from-table`; `until_reason = not_in_issuer_table`). Controlled by `SCRAPER_SEC_EDGAR_ENABLED`. With `SEC_CACHE_DIR` set, every filing fetched is kept gzipped on disk and served from there on the next rebuild, `--force` or second pass — the EDGAR golden copy (`manage.py sec-cache stats`). With the object store configured the same filings are also kept in the shared bucket, so Render (no disk) and every other machine read one common copy.

**Two directions.** Those filings name the company as the *subject* — who owns it, who its insiders are. An institutional investor has none: Vanguard is privately held and isn't a listed issuer, so scraping it by name finds nothing however often you try. What it has is ~3,400 filings it makes **about others**, so the scraper also reads the filer side and writes `OWNS` edges pointing *out* of the company:

```bash
python manage.py sec-holdings 0002100119 --limit 200
python manage.py sec-holdings 0002100119 --succeeds 0000102909   # record a handover
```

A normal company scrape does this too, capped at `HOLDINGS_SCRAPE_LIMIT` subjects; a company that files no 13D/13G costs one extra JSON read and nothing more. EDGAR splits a prolific filer's index into `filings.recent` plus archive pages — Vanguard's old CIK has 13, reaching back to 1999 — so the archive is read lazily, newest page first and only while the fetch budget lasts. That matters for closing a position: an exit amendment sits in `recent` while the percentage it ends was disclosed years earlier, so without the archive the scraper sees an exit for a stake it has no record of. Walking back is slow by nature (measured: ~700 s to reach five closed holdings behind the ~1,800 exit amendments), so raise `--limit` deliberately. Modern filings are structured XML (`primary_doc.xml`), so the subject's CIK, its name and the percentage are read as fields rather than scraped out of HTML; a pre-2025 "SC 13G" has no XML and is read from its index page (subject + CIK) and the filer's own cover page, graded `form` (findings item 44). A joint 13G filed under one reporting person's CIK and amended under another's is one statement: the newest wins, and an older filing whose filer the newer one names as a reporting person is skipped (Berkshire and Buffett on Sirius XM, findings item 93).

**A 0% amendment is an exit, not a gap.** When a filer drops below the 5% threshold it amends to 0%, so the last real percentage is written with `until` set to the amendment date — history rather than a current position. This is not hypothetical: Vanguard moved its reporting from `VANGUARD GROUP INC` (CIK 0000102909) to `VANGUARD CAPITAL MANAGEMENT LLC` (0002100119) in spring 2026, closing ~1,800 positions with 0% amendments on the way out. Keeping only the newest filing per company would report that the old entity owns nothing — true, and useless. `--succeeds` records that handover as a `SUCCEEDED_BY` edge; it is given explicitly because inferring a corporate relationship from filing patterns would be a guess written into the graph as fact.

**Group structure from the 13F cover page.** A fund group files one 13F per manager, and the cover page names the group's *other* managers with their CIKs — ten for Vanguard, including the entity that took over its 13G reporting. That is an authoritative statement of group membership from the filer itself, and it costs one document fetch, so a SEC scrape records it as `(:Entity)-[:RELATED_TO {relation:'affiliate'}]->(:Entity)` with a link back to the filing.

Deliberately **not** an `OWNS` edge: "reports 13F holdings alongside" is the whole of what the form establishes, and writing it as ownership would invent a fact. It is also not `SUCCEEDED_BY` — a filer that hands its reporting to an affiliate has not been replaced, and Vanguard Group Inc went on filing after the change.

Note that the CIK a user searches for may be the retired one — which is exactly why holdings are keyed on CIK rather than name.

Company lookup uses a three-vector strategy to avoid false matches:

1. **`company_tickers.json`** — instant lookup for all US-listed companies
2. **`browse-edgar` name index** — EDGAR's registered company-name search: 10-K and 20-F filers by the closest name, then any registrant of exactly that name whatever it files (a 13D/G holder, a Form 4 owner, a 13F manager); a company that is no registrant at all (Heineken, Saudi Aramco) stays unfound here, preventing false positives from full-text matches
3. **EFTS full-text search** — last resort, guarded by a name-similarity check (SequenceMatcher ratio ≥ 0.55 after stripping legal suffixes)

Investor names are classified as Person or Entity using heuristics that recognise common legal suffixes including European forms (S.A.R.L., GmbH, S.A., N.V., AG, etc.).

📄 **Deep dive:** [`docs/sec_edgar_scraper.md`](sec_edgar_scraper.md) — research and implementation notes (which EDGAR APIs, CIK resolution, 13D/13G & Form 3/4 parsing, per-company request budgets).

## OpenCorporates
Requires a paid API key (`OPENCORPORATES_API_KEY`). Disabled by default.

## Bulk ownership datasets (GLEIF & UK)
Beneficial-ownership data is loaded in bulk from current, authoritative sources —
**GLEIF** (Global LEI, corporate ownership worldwide, CC0) and the UK **Companies
House** register (people with significant control + the company register, Open
Government Licence). Neither has an environment switch: their importers run from
the CLI only (behind the master `SCRAPER_ENABLED`, which the CLI sets), and the
per-company GLEIF lookup (an LEI Wikidata points at) follows the GLEIF switch in the
source catalogue.

Unlike the per-company scrapers above, these are **bulk dataset imports**, not name
lookups — so they are *not* part of `run-all`. Because the source files are multi-GB,
they run from the **CLI** (`manage.py gleif-lei-cdf` / `gleif-rr` / `gleif-succession`
/ `gleif-repex` / `ch-psc` / `ch-company-data`) in a tmux session on the server, not
over HTTP. Both
sources still appear in `/scraper/sources` with independent on/off toggles, and
`/scraper/bods/status` reports their enabled state.

`ch-company-data` also stores each UK company's **SIC codes** as the register files
them and turns them into industry statements, so every UK company — subsidiaries
included — gets its sector in the NACE Rev. 2 tree (the UK code is the NACE class
plus a digit). See *Industry classification* in [`docs/data-model.md`](data-model.md).

> These replaced the OpenOwnership **BODS** exports (GLEIF + UK PSC), which were
> frozen at 2025-03. The BODS importer and its `/scraper/bods/*/run` endpoints have
> been removed; only the CLI importers above remain.

**Daily GLEIF refresh (delta).** Re-running the full ~3.4M-record load every day is
wasteful, so once the full copy is loaded, `manage.py gleif-update` rides on top of
it: it fetches GLEIF's published **delta files** (only what changed since the last
publish — ~14k entities, ~2k relationships and ~3k reporting exceptions) and applies
them in seconds/minutes.
It is *retirement-aware* — a relationship that goes non-ACTIVE has its `OWNS` edge
**closed** (`until` set to the relationship's end date), and a dissolved LEI is
**marked** (`active=false`) and **dated** (`dissolved_on` from the record's COMPLETED
dissolution / merger / absorption / breakup event, with how it ended in `end_kind` —
`merged` / `split` / `dissolved`, the word its ended holdings and seats and the timeline use —
and its liquidation / bankruptcy / insolvency / arrangement events are kept as dated states
(`gleif_events`: timeline rows, the panel's Status) — the full load and the pointer alike —
`heal-ends-at-dissolution` then closes what it held; held back on a node that is an EDGAR
filer's while the filer files on, see *GLEIF sourcing* in
[`docs/data-model.md`](data-model.md)), never deleted (GLEIF never deletes; merges flow through
`SUCCEEDED_BY`). A relationship record GLEIF no longer maintains (registration
status **LAPSED** — a fifth of all records, a quarter of the ownership edges) is
drawn **stale**: dimmed, dated by the record's last update, never closed; the full
load and the delta both set and clear the mark.
Where the record states the consolidated share (49,527 of ~260k records, mostly
100 %) it becomes the edge's `stake_percent`, bounded at the reader (a figure
outside 0–100 is refused and logged, never stored), on the edge and its claim alike. How far GLEIF checked each relationship record
(`ValidationSources` / `ValidationDocuments` — 42 % of the ownership records are
entity-supplied only) rides on the edge, its claim and the Sources panel as
provenance, never as a credibility change. Writes are idempotent (re-applying a delta never duplicates), so no
`--bulk-load` and no whole-DB dedup. `~/scripts/cron-gleif-update.sh` (flock + log →
a `gleif-update` ScrapeRun) is the one crontab line for a daily run.

**The securities bridge** (`manage.py gleif-isin-bridge [--apply]`, findings item 71) joins an
EDGAR issuer to its LEI through GLEIF's daily LEI-ISIN file (CC0, from ANNA's numbering
agencies): the issuer's CUSIP becomes its US ISIN, the file names the ISIN's issuer LEI — never
joining a pair one of which owns the other (a predecessor's retired security, item 94). A dry
run by default, behind the GLEIF source switch. `--apply` runs after every daily GLEIF delta
(`ops.sh gleif-update`, `~/scripts/cron-gleif-update.sh`) and in the rebuild's step 7 — see
docs/deduplication.md.

The delta rides **on top of** the full load, so `gleif-update` refuses to run until a
full load has baselined the graph (the full LEI-CDF import stamps a marker;
`wipe-source --source GLEIF` or dropping the database clears it, forcing a fresh full
load before deltas resume) — it never builds a partial
graph from deltas alone.

**A subset load does not count as that baseline.** A delta carries every record
GLEIF changed *worldwide*, so applying one whole to a curated test database
(`test-import.sh`, or any `--only-file` / `--limit` / `--jurisdiction` import) does
not refresh it — it imports the rest of the world into it: one night added 226,902
entity records and 18,720 edges to a 488-entity subset. The marker therefore
records a **scope**, and only `full` means "apply everything".

A subset still gets its delta, in **`--only-existing`** mode: records for companies
already in the database are applied, records for everything else are counted as
`not_here` and ignored. So a curated database stays current *and* keeps exercising
the delta path nightly, without growing. It is selected automatically from the
baseline scope; `--only-existing` / `--no-only-existing` forces either mode.

Both endpoints must be present for a relationship, and both ends for a succession
edge — the RR importer creates its endpoint nodes, so one relationship between two
unknown companies would otherwise drag both of them in. The trade-off is that a
genuinely new subsidiary of a tracked company is skipped too, so a subset drifts
slightly from GLEIF's truth; re-run `test-import.sh` against fresh golden copies to
resync.

The default `--interval auto` is **gap-aware**: it checkpoints the last GLEIF publish
it applied (an `ImportState` node) and, on each run, picks the smallest delta window
that still covers the gap since then — `LastDay` normally, escalating to `LastWeek` /
`LastMonth` if the box was off for a few days, so a missed run self-heals on the next
one. A gap wider than ~30 days can't be covered by a delta, so the run **fails loudly**
(telling you to full-reload) rather than silently under-applying. Pass an explicit
`--interval LastDay|LastWeek|LastMonth` to override.

**Bootstrap order:** run `full-import.sh` (loads the full copy + stamps the baseline
marker), then let the daily `gleif-update` cron take over — the first run cold-starts
`LastMonth` (reconciles up to a month), then settles into nightly `LastDay`.

UK PSC has no clean delta feed — Companies House publishes a daily *full* snapshot and
no deltas at all — so `manage.py ch-psc-update` computes one locally, by digesting
today's snapshot and diffing it against the last one applied. Cessations, corrections
that reopen a closed holding, and withdrawn records are all handled; re-running against
an unchanged snapshot is a no-op. **Deliberately not scheduled** — it is run by hand
until it has proved itself over several real snapshots. See *Refreshing it* in
[`docs/data-model.md`](data-model.md). A PSC notified in the register's first
year (6 April 2016 to 20 April 2017) by a company incorporated before the register is
a lower bound, not a start — the company declared it on its first confirmation
statement — and `ch-company-data`, which brings the incorporation date, marks those
edges at its end (`since_basis = register_first_year`). Both importers store the
registered office only (`address` with its matching key `address_normalized`, `reg_street`/`reg_city`/`reg_postcode`)
and nothing under `hq_*`: the register does not say where a company is run, and the
headquarters pin comes from a source that does (GLEIF, EDGAR).
