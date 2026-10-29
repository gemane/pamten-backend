# Adding a source: what to take, and what to be careful of

[`scraper-plugin-guide.md`](scraper-plugin-guide.md) is the *mechanics* — the module, the
flags, the runner, the registry. This is the part that is not mechanical: what a source
should contribute, how its records find the companies already in the graph, and the
mistakes this project has actually made.

Work down it in order. The early sections decide whether the later ones are worth doing.

## How to use this as a build brief

This document is written to hand to an AI (or a new contributor) as the complete
specification for "add source X". The contract:

1. **Read [`scraper-plugin-guide.md`](scraper-plugin-guide.md) first** for the mechanics
   (module layout, `ScraperSpec`/`register()`, flags, the runner). This checklist is the
   judgment layer on top.
2. **Every checkbox is verifiable.** Tick nothing on faith — each has a command, a test,
   or a file to point at. The verification loop for the whole build:
   ```
   cd backend && venv/bin/python -m pytest tests -q --ignore=tests/integration
   ARCADEDB_IT_URL=http://localhost:2480 ARCADEDB_IT_USERNAME=root \
     ARCADEDB_IT_PASSWORD='...' venv/bin/python -m pytest tests/integration -q
   ```
   (Local ArcadeDB for the IT suite: one docker container — see the ops docs.)
3. **Deliverable**: one PR into `develop` (never stacked on another branch), containing the
   scraper, its tests, its docs updates, and the `KNOWN_SOURCES` entry — with the PR body
   stating test counts and which mutation checks were run. Ship it **manual-first**: a
   `manage.py` command or a registry `run`, no cron entry and no `run-all` wiring in the
   same change. Promotion to automatic comes later, deliberately, once the pipeline has
   run in anger (the 13F ingest is the precedent: shipped manual, promoted to a
   contributor-only endpoint with a freshness gate only after it proved itself).
4. **After merge, verify live**: trigger one real run against dev, check
   `GET /scraper/runs` for the outcome, and open the enriched company in the panel. A
   green suite is not the finish line; the deployed scrape writing real data is.

---

## 1. Before writing any code

- [ ] **The data licence permits use and redistribution.** MIT/Apache-compatible or an
      open government licence. A source that forbids redistribution cannot go in the graph
      at all — the graph is published, and federation ships it to peers.
- [ ] **Attribution requirements are recorded**, in `KNOWN_SOURCES` and in
      `scraper-plugin-guide.md`'s licence section. Some licences (ODbL, CC-BY) require it
      on display, not just in a file.
- [ ] **Personal data is justified before it is stored.** Officers and beneficial owners
      are natural persons. Take what identity resolution genuinely needs and no more, and
      if the fields differ from what is already stored — a full birth date, a home address
      — say so in `pamten-legal` *in the same change*. The legal pages name the fields and
      why each is necessary; a source that quietly adds one makes them wrong.
- [ ] **You can say what this source is authoritative for.** "Company registrations in
      Ireland" is an answer. "Company data" is not, and it is how a low-quality source ends
      up overwriting a register.
- [ ] **Its `credibility` and `quality` band are decided** (`sources.py`): `statutory` >
      `official` > `aggregated` > `community`. The number decides who wins a name conflict,
      so pick it against the sources already there rather than in isolation.

## 2. What to take

Map the source onto the model in [`data-model.md`](data-model.md) before writing the
parser. Anything that does not map is either a new property (document it) or noise.

- [ ] **Identifiers first** — LEI, CIK, Companies House number, Wikidata QID. These are
      what make the record findable and mergeable later; a company with a name and nothing
      else is a duplicate waiting to happen.
- [ ] **The industry code, if the source assigns one** — EDGAR's SIC, a register's
      NACE/SIC/NAF — as the source writes it, into an `IndustryCode` statement
      (`app.industry.record`; a bulk importer stores the cells and syncs after the
      flush, like `ch-company-data`). A new scheme is a bundle entry (its code list
      and its NACE mapping, `scripts/build_industry_bundle.py`) before any writer.
      The tree the reader sees is NACE Rev. 2 only; the source's code stays beside it.
      Lessons from the first two schemes (BE #416, 2026-10-24):
      - **Serve every statement with its own derived path**, not the principal's alone.
        The reader wanted every branch (Wienerberger's bricks AND its wholesale code) the
        same day; `profile_for` gives each statement a `tree`.
      - **Keep the source's own title beside the derived node.** A crosswalk often stops
        high — EDGAR's 7370 reaches section J only — and the SEC's "Services-Computer
        Programming, Data Processing, Etc." is what the reader hangs under it.
      - **A catch-all maps to nothing by design** (7389 "services NEC", 0000) and many
        filers carry no code at all (13D/G investors, trusts: 82 of 189 CIKs on dev). A
        backfill reports *candidates*, *stated* and *placed* as three numbers; "107 of
        189" is not a bug.
- [ ] **Ownership** — `stake_percent`, `ownership_type`, and `since`/`until` where the
      source states them. Bands ("more than 25%") are common: store what is stated, do not
      invent a midpoint.
- [ ] **A reported "ownership" percentage may be power, not property.** Registers of
      *beneficial* ownership (SEC 13D/G, and the UBO/PSC family) count shares a holder can
      vote OR sell, so a party to a voting agreement reports the whole pooled bloc: Altria's
      13D/A states 51.9 % of AB InBev, and 8.1 % is its own. Read the power rows (sole/shared
      voting and dispositive) and judge by their SHAPE, not by how many parties filed: votes
      beyond everything the holder can dispose of are somebody else's shares → that figure
      is `voting_power_pct`, never `stake_percent`. Altria files alone, and the co-filer
      test alone made it AB InBev's majority owner. And never *derive* other holders'
      voting from someone's bloc: pooled votes dilute nobody; only extra votes per share
      (a dual-class issuer) do, and then only the source's own class votes say by how much.
- [ ] **One holding, two figures: of record versus beneficial.** A source can state the
      same holding in two documents that answer different questions. Musk's Form 4 counts
      what is registered to him (1,123 M Tesla shares, 28.4 % — "controlling" by the 20 %
      line); his 13G/A of the same day counts what he beneficially owns (699.6 M, 19.9 %),
      the 423.7 M restricted award shares disclaimed under an irrevocable proxy. Both are
      faithful; only one is the stake. Decide which document wins AT THE WRITE SITE
      (`sec_writer.stated_by_13dg`: a 13D/G statement is the whole stake, the Form 4
      speaks only for insiders who file none, an exited 13D/G no longer speaks), never
      by the order of the loops — "last writer wins" made him a 28 % controlling holder
      and flipped the figure on every run.
- [ ] **Store the count beside the percentage, and say whose denominator it is.** A stake
      is `shares / shares_outstanding`, and only the count belongs to the holder; the total
      moves with every issue and buy-back. Bevco's last 13D/A (2020) said 5.9 % of AB InBev;
      the same 102,862,718 shares are 5.2 % of the 2026 total. When the same source has a
      newer total for the SAME class, restate older holdings against it and record where it
      came from (`denominator_date`), never across a split (a total that moved more than
      2×), and compare share classes by what they are, not by their wording (ADSs "each
      representing one ordinary share" are the ordinary shares).
      **The count must be the whole holding** before anything divides it again: a filer
      alone holds its sole AND its shared rows (SoftBank's Alibaba stake is 3.8 M sole
      + 627 M through subsidiaries; the sole row alone became 0.02 %) — and never
      MORE than its stated aggregate: Musk's Tesla 13G/A lists the same 699,580,882
      shares as sole AND shared, and the sum stored twice his holding. Row 9 is the
      union of the power rows; a sum above it is the same shares counted twice. Check
      that `shares ÷` its own total gives back the filed percentage, and don't
      recalculate a row where it doesn't.
      **A denominator needs a date, and a count needs its class.** A total that is the
      latest value of a series the issuer stopped keeping is not its current count:
      Berkshire's plain XBRL series ended in 2011 and its last value, 941,481 Class A
      shares, turned Warren Buffett's 12 M Class B count into 1,274 %. Reject a total
      older than ~15 months before the day you divide, read the cover instead, and
      divide a class only by the count of that class — never by another's, never by
      an unclassed total on a multi-class issuer. And take a filing's *last* row per
      security, not its largest value: the rows are in order and each states the
      position after it.
- [ ] **A series can be stopped.** The newest value of a series is not its current value
      until you have checked how old it is. Berkshire's plain `dei:EntityCommonStockSharesOutstanding`
      series ended in April 2011, when the issuer moved to per-class reporting with a
      dimension — which the aggregated XBRL endpoints (companyfacts, companyconcept) drop
      entirely. A fetcher that takes "the latest value" was handing out a fifteen-year-old
      count. Date every value you divide by, refuse one older than the thing it measures
      allows (15 months for a quarterly filer), and read the primary document instead.
- [ ] **An inline-XBRL figure is scaled.** `ix:nonFraction` shows the number as printed
      and states its power of ten in `scale` (`scale="6"`: millions). Toast's cover
      "514" read as 514 shares made a director 693.77 % of Class A, and the database's
      stake rule then failed the whole company scrape, twice. Multiply by 10^`scale`,
      accept a decimal display ("13.18" billion), and refuse any computed share of a class
      above 100 % as a wrong denominator, not a holding.
- [ ] **A dimensional XBRL fact is three things: a member, a concept and a context.** The
      acquirer's business combinations (`sec-acquisitions`) are tagged as an axis member
      (`goog:WizIncMember`) on a consideration fact whose CONTEXT period is the closing day —
      the date is never a fact of its own, so read the context, and only a one-day period
      (`instant`, or `startDate = endDate`) is a date. The member's QName is not a name
      (read the labels — `MetaLinks.json` sits beside EVERY inline filing; the `_lab.xml`
      linkbase only beside Workiva-filed ones, Donnelley keeps it in the zip; strip
      "[Member]" and a trailing *Acquisition*; unescape entities) and not stable across
      filings (`IntersectMember` becomes `IntersectAcquisitionMember` a quarter later) —
      dedupe by the name. A member is not always one company: a basket ("A Company And B
      Company", a comma list), an aggregate ("Other Acquisitions"), or assets ("Fiber-To-
      The-Home") — rules in `sec_acquisitions.clean_label`, found by sweeping all 144 dev
      filers, not by reading one. The concept decides what happened: a consideration
      *transferred* closed; an agreed or *expected* price (`PaymentsToAcquireBusinessesGross`,
      `…PriceOfAcquisitionExpected`) is still pending — Alphabet's FY2025 10-K tagged Wiz a
      year before it closed. A context with a second axis is a breakdown of the deal, not the
      deal. And none of this reaches the aggregated endpoints (companyfacts, frames) — read
      the instance document.
- [ ] **A form's second table can hold real shares.** A Form 4's derivative table is where
      options, units, rights and notes live — and also where a convertible class of common
      stock is reported (Berkshire's Class A, convertible into B, so Buffett's 188,290 A
      shares sit in Table II). Read it for plain common-stock titles and skip the rest; do
      not let the table's name decide what a row is.
- [ ] **A plausibility bound at the writer.** A stake above 100 %, or a count above the
      denominator it was divided by, is a parser error, not a fact: refuse the row, log it
      with the source URL, and count such refusals in the quality report. The 1,274 %
      stake above was stored without complaint and found by a reader's eye months later.
      (Not yet implemented in `sec_writer` as of 2026-10-10 — the item stands as the rule
      for every writer, and as the open task. Implemented for GLEIF's stated shares on
      2026-10-13: `gleif_rr._stake` refuses a figure outside (0, 100] — 284 of 49,527 on
      the golden copy — and the import counts them `implausible`.)
- [ ] **Registration and headquarters are different facts.** `country`/`address` is where
      a company is registered, `hq_*` where it is run. Never coalesce them — the map's
      Registered/Headquarters switch exists precisely because they differ. And never
      COPY one into the other to give a pin something to draw: both Companies House
      importers wrote the registered office under `hq_*` until 2026-10-18 (there was no
      registered pin yet), which captioned an accountant's or agent's door "Headquarters"
      and overwrote the headquarters GLEIF had put on a merged node. A source that states
      only one of the two places leaves the other empty; "we do not know" is a fact too.
- [ ] **A knowledge base writes no address and no type.** Wikidata writes no `country`,
      `countries`, `hq_*`, `hq_locations` or `type`, neither on create nor on update
      (2026-10-27): the registers state them, and an item often describes a group, not
      the legal entity. Unilever's Q157062 states the United Kingdom and the Netherlands
      in arbitrary order; the Wikidata update wrote NL over the GB that GLEIF and
      Companies House state for UNILEVER PLC. A crowd-edited source enriches with what
      no register states (description, employees, revenue, logo, website) and never
      outranks a register on a fact the register owns.
- [ ] **Keep the finest grain the source states — do not collapse it to the country.**
      Exhibit 21 states "Florida, USA"; storing only `country: US` threw Florida away, and
      a subsidiary's panel showed "United States" with no real location (the reported bug).
      Map to BOTH `country` and `jurisdiction_code` (ISO 3166-2, e.g. `US-FL`) so the panel
      can show "Registered in: Florida". The UI only names some families (US/CA/GB/AE/KN),
      so emit codes for those; elsewhere a region is administrative, not a domicile choice.
- [ ] **Address parts stay separate** (`*_street`, `*_city`, `*_postcode`, `*_country`).
      Geocoding is a structured query; re-parsing an assembled string is guesswork, and
      every country writes an address differently. Check it on a source that hands the
      parts over: both Companies House importers joined premises, lines and postcode into
      one display string and wrote the city alone until 2026-10-18 — 203k of 219k
      corporate PSCs and every register row carried a postcode the graph never stored.
      Write only the parts the record has (a bulk SET of null blanks what another source
      put on a merged node). Build them with `app.addresses.parts(kind, street=…, city=…,
      postcode=…, country=…, full=…)` and never spell a field: the registry knows the
      names, derives the matching key, drops what a kind does not store, and writes a
      part as None only where you name it in `empty`. A test counts the spellings per
      file (`tests/test_addresses.py`), so a new one fails.
- [ ] **Keep values as the source gave them.** Do not strip a legitimate `C/O …` line or
      "normalise" a name to make a downstream tool happy — fix the tool-facing side. A
      stored value that no source ever said is a lie the graph cannot walk back.
- [ ] **One fact, one field — never a scalar beside its list.** `nationality` sat beside
      `nationalities` and `country` beside `countries`; three writers filled three
      different halves (the scalar, an empty string in the scalar, the list) and every
      reader needed a fallback (2026-10-25; `founded` / `founded_date` was the same disease
      the day before).
      Write the list (`app.field_twins.merge_*` folds a single value in); where a scalar
      must stay as a filter key (`country`), the list always contains it.
- [ ] **What no source stated is absent — never `''`, never `[]`; a flag that means "not
      asserted" is `false` by declaration.** The SEC and PSC person writers wrote
      `description: ''` for 1,378 people and nobody noticed for a year, because a reader
      with `or ""` hides it; 196 empty `nationalities` lists hid the same way. Leave the
      key out (the writers since 2026-10-25). A flag is the one shape that
      must NOT be absent: since ArcadeDB 26.10.1 a `<>` on a null is false and an unset
      `stale` dropped 1,274 edges out of every filter — so every flag meaning "not
      asserted" is listed in `schema.FLAGS` and declared `DEFAULT false`. The exception is deliberate and
      documented: `Entity.active` stays three-valued, because null means no register has
      said whether the company exists, which is a different fact from "not ceased".
- [ ] **Dates carry their real precision.** UK PSC publishes month and year only; storing
      a fabricated day makes two different people look like the same one.
- [ ] **An entity's end is a fact the source may state — read it.** Wikidata's P576,
      GLEIF's `EntityExpirationDate`, Companies House's `CompanyStatus`/`DissolutionDate`
      all existed for years before anything read them (2026-10-25), so a dissolved
      company read as live, kept open holdings and seats, and time travel hid it before
      its incorporation but never after its end. Write the date to `dissolved_on` (the
      source's precision, fill-only), `active = false` beside it, a register's own
      status wording to `company_status`; a per-company writer then calls
      `app.dissolution.end_at_dissolution`, a bulk importer leaves it to
      `manage.py heal-ends-at-dissolution --apply`, which every bulk chain runs after
      the dedup (the post-load item in section 7). Until 2026-10-27 no chain did, and
      dev's Altaba, dissolved in 2019, still held 104 open edges. **An edge written LATER
      onto a company already known dissolved** is closed by the scrape scope
      (`graph_writer._end_dissolved_after_scrape`, after the dedup) — the 2026-10-27
      rebuild replayed the SEC scrapes after the chain's step and Altaba had 105 open
      edges again. **A new OWNS / HAS_ROLE writer calls `_record_edge_parties` first**:
      the scope learns which companies an edge landed on from that call, not from the
      upserts — the Exhibit 21 runner only resolves its parent, so until 2026-10-29 the
      next rebuild still left Altaba's 78 Exhibit 21 holdings open (the conformance test
      lists every edge writer). A writer that matches only open edges must also match the spell the
      rule closed, when the filing is dated inside it, or every re-read draws a second
      edge (`sec_writer._dissolution_closed`; seats do this through `_same_seat`).
      A knowledge base never writes `active = true`: null means no register has said.
- [ ] **A new edge property goes into `edge_schema.OWNS_PROPS` first.** `owns_props()`
      raises on unknown keywords by design, and the merge paths derive their recreate
      lists from the schema — a property added to a writer but not the schema is silently
      stripped by the next merge. Check the sources endpoint's `_COLS` too: it is a
      sibling of the same vocabulary.
- [ ] **A new number, flag or list is declared in `schema._TYPED_PROPERTIES`** (DOUBLE /
      INTEGER / BOOLEAN / LIST — dates stay STRING, they keep a source's precision).
      Undeclared, a value is stored in whatever type the writer's JSON carried, and
      `stake_percent` sat as DOUBLE on 974 edges and INTEGER on 20 until 2026-10-24.
      Declared, the server coerces every write and REFUSES one it cannot convert, so
      send a convertible value or none. Choose the type for what a source may *state*,
      not what it usually states: share counts are DOUBLE because a Form 4 reports
      562,701.427 shares and a LONG declaration is refused over it. Changing a type
      later is not a one-line edit: the bootstrap drops and redeclares an unindexed
      property whose server type differs (ArcadeDB has no `ALTER PROPERTY … TYPE`),
      but the stored rows keep their old type, and a converted row is coerced straight
      back while a stale declaration stands (47 rows on dev looped that way for a day).
- [ ] **Every property the writer sets is in the schema's ledger.** ArcadeDB accepts an
      undeclared property, so nothing stops a typo — the ledger does (`app/db/schema.py`:
      a string into `_STRING_PROPERTIES`, a number, flag or list into `_TYPED_PROPERTIES`
      with its type; the conformance test in `tests/test_schema.py` holds every edge and
      claim property against it). After the first import, `manage.py schema-audit` names
      what the data carries that the ledger does not (2026-10-25).
- [ ] **The database refuses what breaks a rule — add the value to the enum, not a
      workaround.** `Entity.type`, `OWNS.ownership_type`, `read_from` and a claim's `kind`
      are restricted at the server to the vocabularies built from `EntityType`,
      `OwnershipType`, `READ_GRADES` and `KIND_*`; `country` must be ISO-2; a percentage
      0–100; every date `YYYY-MM-DD` (or the year / year-month a source states — the
      shape, not the calendar) and every timestamp ISO with an offset; a claim must
      name its source (`schema.constraints()`, 2026-10-25). A writer that sends anything
      else fails the scrape with "does not match the regular expression" — which is the
      point. A new category goes into the enum (one place; the rule follows), a non-ISO
      country into `country_registered`, never into `country`; a new date field goes
      into `schema._DATES` (or `_TIMESTAMPS`) so it carries the rule too.
- [ ] **A placeholder in a field is not a value.** Form 4's title box says "See Remarks"
      when the title is in the remarks box; five dev seats carried the placeholder as
      their role until 2026-10-25 (`sec_edgar._officer_role` now reads the remarks — only
      when they read as a title, since a sentence there can describe a post at another
      company — and falls back to "Officer" rather than ever storing the pointer). Look for the
      source's own "see elsewhere" / "not applicable" / "other" tokens before storing
      a free-text field as a fact.
- [ ] **If the source's records are filings, stamp `filing_type`** ("13G/A", "13F", "RR",
      "PSC") — the source names the register, this names the *rulebook* the fact lives
      under, which is what a reader needs to judge it.
- [ ] **Stamp `read_from` on every value the writer records** — how surely it
      was read, not how good the source is: `field` (a named field of XML/JSON),
      `table` (a cell under a header the filer wrote), `form` (a numbered item of a
      regulator's form found in its text), `layout` (inferred from the page:
      indentation, a heading, a carried header), `prose` (items written as text
      lines), `narrative` (a fact picked out of a sentence — the grade for any
      news-like text, should one ever be admitted). Grade per VALUE and inside one document where the readers differ (a
      13D/G is XML since 2024-12-18 and a text cover page before; an Exhibit 21 row
      is a table cell, its parent an indentation), and give an edge the weakest of
      its parts (`edge_schema.weakest_reading`). The grade orders equally credible
      claims and never lifts a source over a more credible one — and it is never an
      argument for admitting a weaker source: a low grade names the parser to
      improve next (`manage.py quality-report` counts edges per source and grade).
      **The reader's row carries the grade, not the writer's call.** A writer that
      passes `row.get("read_from")` writes null when one reader forgets it: the
      filer-side 13D/G rows (`fetch_filer_holdings`) had none, so the company
      scrape wrote 681 ungraded edges while `run_sec_holdings` hard-coded `field`
      for the same rows. A test per reader asserts the key on its rows.
- [ ] **Display-only extras follow the established conventions.** A website goes through
      `normalize_url` (http(s) only, reject rather than repair — the value becomes an
      `<a href>`); an image becomes a direct `upload.wikimedia.org` thumb via
      `commons_thumb_url` (never `Special:FilePath` — its redirect broke every logo on
      mobile); both are fill-if-missing across sources and never search tokens. And check
      multi-valuedness before assuming: "the official website" was 100+ regional
      storefronts on Wikidata, and "the logo" was four.
- [ ] **Decide `kind` and `depth_aware`.** `instant` = query-driven, runs on a user's
      click; `bulk` = a whole dataset on a schedule. Only `depth_aware` sources re-run on
      the depth-2 pass.

## 3. Identity and duplicates

This is where a new source does the most damage, because a wrong merge is much harder to
undo than a missing one. See [`deduplication.md`](deduplication.md) for the model.

- [ ] **Node ids are slug-safe: no slashes, ever.** Ids travel as URL path parameters and
      the ASGI server percent-decodes the path *before* routing, so an id containing `/`
      makes its page unreachable. Never embed an API self-link verbatim; mint
      `prefix:{part}:{part}` from its stable components (the `chpsc:` ids did it wrong,
      shipped, and needed a routing workaround plus a data migration). **And key the id
      on the source's hard id when the writer has one** (`app.node_ids.mint_node_id`:
      `lei:`, `wd:Q…`, `sec:{cik}`, your register's own prefix): a uuid is re-minted by
      every rebuild, and every shared link, pin and flag that named the node broke with
      it — 2,225 dev nodes carried a QID or a CIK under a uuid until 2026-10-26. A
      name-only node keeps a uuid; a malformed value is never keyed on. **A writer that
      stamps a hard id onto an existing node re-keys it** (`node_ids.key_on_learning`, the
      return value is the id from then on): an Exhibit 21 row or a 13D party created by
      name and later met with its CIK kept its uuid once the one-off heal was removed
      (2 after the 2026-10-27 rebuild). A new stamping site calls it too.
- [ ] **Match on a hard identifier when the source has one.** Shared LEI/CIK/CH number is
      proof; a shared name is a hint.
- [ ] **A name is replaced by credibility, never by arrival order.** Write `name_credibility`
      with the name, and let an existing node take the incoming name only when the source
      is at least as credible AND the name is the source's own for that id (EDGAR's name for
      the CIK, the register's for its number) — not the spelling another filing uses for
      someone else. The SEC writer kept whatever name it found, so 23 filers showed the
      Wikidata label of whichever source ran first (2026-10-27 rebuild). Keep the replaced
      name as an alias.
- [ ] **Read by the id the database already holds before searching by name.** A per-company
      reader resolves the query to its node first and, when the node carries the source's id,
      fetches by that id; the name search is the fallback. The SEC scrape searched by name
      only and missed SpaceX (a trade name) and Nestlé (no 10-K/20-F) although both nodes held
      the right CIK (2026-10-27); Exhibit 21, acquisitions and 13F already read by the number.
- [ ] **When only a name proposes a pair, let the source's own documents settle it — and
      read them.** An LEI-only node and a CIK-only filer of one name share no hard id; the
      filer's own filings stating the LEI do (`sec_lei`, 2026-10-27). A search hit says only
      that the code occurs in some document of that filer. A guarantor's prospectus carries
      the issuer's LEI, and one document can be filed under two CIKs. So validate the
      identifier's checksum, take a lone LEI only from a document the filer filed alone,
      and otherwise require the name to label the code. A miss is "unknown", never "different".
- [ ] **When the source NAMES the register, use the name before any guess.**
      `register_for_name` matches the filer's words against every name GLEIF lists for the
      country's registers (local names, organisation names, sites) — it keys funds and
      German companies, which no country rule can. Measure a source's phrasings before
      adding aliases: `manage.py audit-psc-registers` is the model — coverage per rule and
      the unkeyed phrasings per country, each alias line citing its count.
- [ ] **A stated register is a hard identifier — mint it through `gleif_reference`.**
      `make_register_id(code, number)` (placeholder RAs excluded, zero-normalization for
      the audited US registers where sources demonstrably disagree on padding);
      `sole_register_for_country` for country-only statements; `register_for_place` for
      sub-national ones — **audited countries only**: the raw exactly-one rule would have
      stamped Bavarian HRB numbers onto a Foundations Directory; `register_for_number_format`
      where the NUMBER's format names the register (Japan lists four registers, but a
      dashed `0104-01-056795` is a Legal Affairs Bureau company registration number and
      nothing else — SoftBank Group's PSC record stayed a name-only twin of its LEI node
      without it); `general_register_for_country` for the 67 countries where the GLEIF
      audit (`manage.py audit-registers`, `data/general_registers.json`) shows one
      register holding ≥ 90% of the country's companies — re-run the audit, never add a
      country by hand. Mint through `make_register_id` and nothing else: it folds the
      registers GLEIF keys one national number under (Swiss UID, French SIREN) and fixes
      one spelling, so keys agree wherever they are minted. And read *every* field the
      source might put the register in: real filings had it in `legal_authority` with
      `place_registered: "N/A"`.
- [ ] **Registers move; don't fight history.** A current-key mismatch is not evidence of a
      different company — Tesla's Delaware pair lives in `former_register_ids` now, the
      dedup matches held-vs-holds, and a refresh that sees a registration change must
      preserve the outgoing pair (see `import_lei_cdf_delta`), not overwrite it.
- [ ] **A former name comes with its date, if the source has one.** Registers record renames
      dated (Companies House `PreviousName_n.CONDATE`, EDGAR `formerNames[].to`); keep the
      date as an `app.former_names` entry (`"NAME|YYYY-MM-DD"`, the last day the old name
      applied) in `former_names` beside the plain name in `aliases`, so the timeline can
      say "Renamed" under the right year. Companies House dropped its dates for a year
      before anyone asked why the register showed a name the graph did not. A source
      that knows the name but not the date (GLEIF's previous legal names) stores it
      undated, or leaves it in `other_names` — never invents a date.
- [ ] **A record's `name` may be a display string — read the parts when the source has
      them.** Companies House prints an individual PSC's `name` as title + forenames +
      surname ("Mr Michael Charles Saunders") and puts the parts in `name_elements`;
      taking `name` made `first_name = "Mr"` for 90 % of 2.78M people, so the dedup's
      name tokens never matched a Wikidata or SEC twin — for a year. Build a person from
      forename / middle / surname (`companies_house_psc._person_name`), keep honorifics
      out, and count the display strings that start with a title before trusting one.
      And read every value a `natures_of_control`-style vocabulary can take over the
      whole file: the overseas-entity register's `more-than-25-percent` has no band, and
      an LLP's `right-to-share-surplus-assets` is its shares — both read as "no stake".
- [ ] **Use the existing writers** — `_upsert_entity_by_name`, `_upsert_person_by_name`,
      `resolve_entity_id`. They already implement match-by-id-then-name-then-normalised,
      and the credibility rule that stops a weak source renaming a company.
- [ ] **A thing that is not a legal person is not an Entity — give it its own vertex
      type.** A 13D filing group spent a year as an Entity of type `voting_group`, and
      every query that assumes an entity is a company had to fence it off: eight did
      (`not_a_voting_group`), the rest forgot — the 38 groups were the only entities
      without a nominee flag, were counted in the entity totals, and were offered to
      every heal that walks the entities. Since 2026-10-26 it is a `VotingGroup` with a
      `MEMBER_OF` edge, and nothing that walks the entities meets one. For a new kind
      of node (an agreement, a trust arrangement, a fund share class): a vertex type in
      `schema._INDEXES`, its name in `anchors.NODE_LABELS` (every site that reads a
      label off an edge coerces through `label_or_entity`), `owner_label=` on the OWNS
      writer when it can own, and the entity routes open it by a labelled fallback read
      — never an unlabelled scan. Keep `type` on the node as the API's node kind: the
      client draws by it, and it is what let the frontend stay unchanged.
- [ ] **Run the whole scrape inside one `@_with_autodedup` scope.** The person auto-merge
      only groups duplicates touched *together* in one scope, so per-source scopes leave
      cross-source pairs unmerged — Wikidata "Larry Page" beside SEC "Page Lawrence" is the
      bug that taught us this.
      **The decorator goes on the entry point, and a helper added later goes ABOVE it.**
      `@_with_autodedup` sat on `_upsert_affiliate` and `_same_filer` for months, two
      helpers inserted between it and `run_scrape_sec_edgar` / `run_sec_ex21`, so the
      standalone SEC scrape and EX-21 run had no scope at all;
      `tests/scraper/test_scrape_scope.py` now names every `run_*` that must open one.
- [ ] **A code field never carries words.** `country` (and every other ISO-coded
      field) holds the code or nothing; a filer's text that resolves to no code is kept
      as written in a field of its own (`country_registered` for PSCs). "Not
      Specified/Other" sat in `country` on 271 of 3M PSC records, where a country filter
      found nothing and a flag drew nonsense.
- [ ] **Expect re-runs to recreate duplicates** and dedup afterwards. Scraping the same
      company twice from two sources is the normal case, not an error.
- [ ] **Never write past a merge.** A merged node leaves a `MergedId` forwarding row and
      by-id reads follow it; resolve before writing rather than resurrecting the loser.
      The quiet twin of this: an enrichment addressed to a *constructed* id
      (`UPDATE … WHERE id = 'gb-coh:{number}'`) is a silent no-op on the node that
      holds the number under another id — the survivor of a merge, or a GLEIF node
      that never had a twin. `ch-company-data` missed 207 of dev's 238 UK companies
      that way for a year, unnoticed until the SIC codes were the first field nothing
      else supplied (2026-10-24). Look the identifier up and address every holder;
      count the rows that went to one, so the log shows the path is alive.
- [ ] **One edge per pair.** Re-asserting an existing relationship updates it; it does not
      add a second one.
- [ ] **A new node is stamped `created_at`.** Every CREATE carries it; an UPSERT writes
      `created_at = COALESCE(created_at, :now)` so a re-import is not a birth. The
      bulk writer does this for every node it flushes; a writer with its own CREATE
      adds it by hand — `tests/test_created_at.py` lists every creation site and
      fails on one that forgets.
- [ ] **Structured "person" fields still carry companies.** Form D's relatedPersonInfo
      has firstName/lastName — and fund filings put their GP LLC in them, with a literal
      `N/A` first name. Field structure is not a person guarantee: strip the filler,
      then apply the entity-suffix veto before minting anyone.
- [ ] **One document can describe several people.** A joint SC 13D/G has one cover
      page per reporting person, back to back; a parser that takes the first match
      reads the first person's type and stake for whoever the filer of record is
      (Cia. Bozano, a company, became a Person owning Júlio Bozano's 10.4%). Cut the
      document to the filer's own section before reading per-person fields
      (`_cover_page_for`), and keep the veto list's legal forms international —
      `Cia.`/`Companhia`/`Ltda`/`S/A` were missing.
- [ ] **Booleans come in more than one spelling.** EDGAR's Form 3/4 relationship flags
      are `1` in older filings and `true` in newer ones; reading only `1` silently dropped
      Apple's new CEO. Accept every spelling the source has ever used, and test with a
      real recent document, not one written from the schema.
- [ ] **A source that never states an ending still needs one.** Insiders file nothing when
      they leave, so a roles list only grows. Close seats only from an explicit statement
      (a "Former …" Form 4, an 8-K Item 5.02 naming a person we already list) — never from
      silence, and never by minting the person the statement names.
- [ ] **Classify people with `is_person_name`** and know it is a heuristic. Registers list
      corporate nominees as officers, and `is_nominee` marks holders of record who are not
      beneficial owners.

## 4. Provenance, freshness, and the country

- [ ] **Every fact carries its source.** `source_id` on nodes and edges, and
      `record_claim` for the per-source assertion — that is what makes a conflict
      inspectable later instead of a mystery.
- [ ] **`credibility_score` is written on the edge**, not assumed from the source name.
- [ ] **A register's own "how far we checked this" is provenance, not credibility.**
      GLEIF states a corroboration level per record (`ValidationSources`); on entities
      98.7 % are fully corroborated and a small `name_credibility` penalty is fair, but
      on relationships 42 % (109,060 of 260,337 active consolidation records) are
      ENTITY_SUPPLIED_ONLY, and the same penalty would push them below the official
      tier into the community rules (six-month staleness, outranked by Wikidata).
      Measure the distribution before scaling anything; keep the level on the edge,
      the claim and the Sources row (`validation_sources` / `validation_documents`) and
      let a reader — or `answer_rank`, within the tier — weigh it.
- [ ] **Share the pair's edge; never look up only your own.** Find the pair's active
      OWNS edge whoever drew it, and apply `app/scraper/owns_merge.py`: take the answer
      over only when you `outrank` its holder, combine `since`, keep the other source's
      structure, and never close or reopen an edge another source holds. Matching
      "my own edge" (by `source_id`, a marker, a record link) drew a second edge
      beside every pair another source had — 129 on dev, each deleted by the next
      dedup and redrawn by the next scrape.
- [ ] **A printed page is not a table.** A filing tool splits one list into one
      HTML table per page, and only the first carries the header row — Chubb's
      ownership column was silently lost on ten of eleven pages. Carry a header
      over to the header-less tables that follow it, and keep a content check so
      a different table does not inherit it. A header word inside a value
      ("United States" contains "state", "ERICO Global Company" contains
      "Company") is not a header: a cell that maps to a place is data.
- [ ] **A continued page can shift its columns.** The header-less tables that
      follow the first page need not keep its cell positions: Western Union's
      second page dropped the spacer columns, so the carried header read the name
      cell as the jurisdiction and 58 of 102 rows were lost. Read a continued
      table both ways — by cell index and by the header's column grid — keep
      whichever reads more places, and let a tie keep the index.
- [ ] **Layout is data only when it is unambiguous.** Exhibit 21 filers draw the
      group tree by indentation or "Subsidiaries of X" headings — and the same
      signals appear where there is no tree (a uniform hanging indent, a page
      heading repeated on every page, a heading naming the filer). Read a tree
      only under strict acceptance rules with a flat fallback, stamp the basis on
      what you inferred (`structure_basis`), and test on a real sample: 62 exhibits
      found every shape above.
- [ ] **Match a name as strictly as its writer was careless.** A heading
      ("subsidiaries of WISeKey") and a parent column ("Seaspan Holdco III") come
      from the same filer with different care: a loose match on headings made the
      row "WISeKey SA" its own parent and put ADS-TEC's PLC under its Inc.; a strict
      match on columns lost Atlas's real parents. Match a heading on legal-form
      spelling only (Ltd = Limited, Corp = Corporation …), keep the loose unique
      match for columns, and treat a namesake with another legal form (Covestor,
      Inc. / Covestor Limited) as another company.
- [ ] **A list is not a start date.** A document that lists holdings *as of* a date (an
      Exhibit 21, a 13F, a register snapshot) gives an as-of `source_date`, never a
      `since`: storing the filing date as the start made News Corp's FY2026 Exhibit 21
      "acquire" 200 subsidiaries in 2026. Where older lists exist, their unbroken run
      gives a lower bound — store it with `since_basis` saying so.
- [ ] **Make it work for time travel.** The graph and the panel can be shown *as of*
      any year (`/search/entity/{id}/full-profile?as_of=`), and they can only be as
      right as the dates a source writes. Before shipping, check every edge the source
      writes against these rules:
      - **A start**: `since` only when the source states when the relationship began
        (GLEIF, PSC, a first 13D). A lower bound gets `since_basis`. An as-of list
        gets only `source_date` (see the item above). The profile treats each one
        differently: a stated start hides the edge before it, while a lower bound or
        a bare `source_date` marks it as "not documented for that year". **Look at
        the register's first year, not only its first day**: a new register is filled
        by existing companies on their next periodic filing, so a "notified" date
        inside that year on a company that predates the register is the filing's day,
        not the start (UK PSC: 54,815 records on 2016-07-01 — the confirmation
        statement's first day — against a handful on 2016-04-06; marked
        `register_first_year` since 2026-10-18). Telling them from real starts needs
        the incorporation date, which may come from a *later* import — then the rule
        runs at the end of that import, and the refresh must keep the mark when a
        touched record restates the same day.
      - **An end**: when the source says a holding or role is over, close the edge
        with `until` (+ `until_reason`), never delete it. A deleted edge is missing
        from every past year too. A 0 % amendment is an exit, not a 0 % holding.
      - **"As of" vs "said on"**: the day the facts were true (13D/G *date of event*
        → `event_date`, a register's snapshot date) is a different fact from the day
        they were published (`source_date`). Store both; never use one as the other.
        And neither is the *start*: the PSC importer wrote `notified_on` as
        `source_date` for two years, so a 2016 appointment confirmed by every
        snapshot since read as 2016 evidence (fixed 2026-10-18 — the snapshot date).
      - **Precision**: a month-only or year-only date stays that way (`2023-04-00`,
        a bare year). A made-up day moves an edge across a year boundary.
      - **A snapshot source forgets**: a golden copy or register dump shows today,
        and what ended is simply gone from it. Look for the source's archive of
        old snapshots (GLEIF keeps every publish since 2018, `gleif-rr-history`
        diffs one a month); deltas usually only cover the last days.
      - **A register record has a status of its own — read it.** Beside the fact
        (GLEIF's `RelationshipStatus`: does the relationship hold) the record says
        whether anyone still maintains it (`Registration.RegistrationStatus`).
        LAPSED = "not renewed by its NextRenewalDate": the ACTIVE on it is the last
        word, not a current one — Metaswitch "consolidated by Microsoft" since a
        record last updated 2023-06-02, sold in 2025 — and it was a fifth of all
        GLEIF relationship records, a quarter of the ownership edges, read as
        current for a year. Draw such an edge `stale` (dimmed, dated by the
        record's last update), never closed; ANNULLED/DUPLICATE records are no
        statement at all. Count the statuses over the whole file before deciding
        (`~/.cache/gleif-audit/scan.py` is the shape), and stamp them on EVERY
        path that writes the source — the delta had carried the entity status
        for months while the full load did not.
      - **An archived snapshot can be broken, and records flicker.** Diffing
        snapshots turns every missing record into an ending, so check two things
        before believing one. A **dip** (smaller than the month before, back the
        month after) is a broken publish: GLEIF's 2023-08-01 file had 281k records
        between 399k and 400k and would have ended 120k relationships. A real
        clean-up stays down (2019-09, −10 %), so "any shrink" is the wrong test,
        and a fixed floor latches: after one real drop it skips everything after.
        And a record that vanishes and returns with the **same stated start** was
        lost by the file, not ended: 63,273 of 85,001 GLEIF gaps. Join those.
      - **A stated date can be a placeholder.** Before trusting a source's start
        dates, count how often they equal another date on the record: the record's
        own registration day, the identifier's registration day, the founding
        day, the accounting-period start. Then look at the most frequent days.
        GLEIF: 10.7 % of starts are just the child's LEI registration day
        (Barclays Bank "since 2012", owned since 1985); 1 January and 31 December
        pile up; 2017-10-02 alone has 1,725. A placeholder becomes a lower bound
        (`since_basis`), never a deletion, and the claim keeps the date as stated.
      - **A source can refute itself.** Its other records may contradict a date:
        a company GLEIF lists as the top of a tree has no parent, so "Microsoft
        owns Activision since 2001" falls to King.com naming Activision its
        ultimate parent until 2026. The claim keeps what the source asserted; the
        edge stops believing it, and no merge or delta may bring it back. Measure
        the false positives before shipping (Barclays Bank: its subsidiaries were
        wrong, not its parent record) and choose the failure that weakens a fact
        rather than one that asserts a false one.
      - **Stake history comes for free — above 5 %.** Every claim write keeps the
        statement it overwrites when a holding at or above 5 % changes (data-model,
        *Stake history*); you need do nothing but pass the statement's date
        (`event_date`, else `source_date`). Below 5 % an edge still holds only the
        latest numbers, so a past year shows today's stake. If the source publishes
        history (amendments, annual lists, archived snapshots), say in the source doc
        what is kept and what is lost. **A run must state a holding once**: two
        documents of one source written into the one claim per (pair, source) in a
        single run — the 13G/A, then the Form 4 — recorded a 28 → 20 → 28 flap as
        history on every run, a ping-pong that had been silent before history
        existed (since 2026-10-26 an SEC claim is keyed by filing family, so those
        two are two claims and two series; the rule stands for two documents of
        one family). Wrap the runner's write phase in `one_statement_per_run()`
        (`app.stake_history`): the stored claim is read once, the rows are computed
        at the end against the run's LAST write. Any runner that can write a claim
        twice per run needs it. And a statement dated BEFORE the one it replaces
        writes no replaced row — history is ordered by `as_of`, not by when you ran.
      - **The timeline words what you write.** The panel's Timeline tells
        transitions (a start, an end, the day a holding stopped being confirmed),
        and every sentence it shows comes from a code on the edge:
        - An end is worded by `until_reason`. The known codes are
          `gleif_inactive`, `gleif_dropped`, `ceased`, `withdrawn` and `dissolution` (a 13D/G
          end is worded by its `filing_type`); anything else reads "the source
          records the holding as ended". Reuse a code that fits; a new one gets
          its sentence in the frontend in the same change (`END_REASONS` in
          `TimelinePanel.tsx` + `timeline.endReason` in every locale). Never word
          an end as a sale unless the source says it was one.
        - A dimmed edge is four fields, not one: `stale = true` plus
          `stale_reason`, `stale_since` and `stale_source_url`. Without
          `stale_since` the row gets a badge but no dated "No longer confirmed"
          entry. A new `stale_reason` code gets its sentence in
          `timeline.reason`, as above.
        - Every path that confirms the pair again clears all four with
          `STALE_CLEAR` / `STALE_CLEAR_CYPHER` (`edge_schema.py`), never
          `stale = false` alone — un-dim paths sit in several writers; grep
          them all.
        - A stated price goes in `value_usd` beside the stated `since`; that
          is what reads "Acquired … for $X bn".
      - **Test it**: one IT that writes the source's edges and reads the profile
        with an `as_of` before, inside and after the dated range.
- [ ] **A source link needs its identifier.** A URL template filled with an empty
      number is a page that does not exist (`…/company/`), and it looks like a bug in
      the panel, not in the data. Build the link only when the number is known, fall
      back to the number inside the node id (`gb-coh:05299520`), and otherwise store
      no URL at all (`industry.ch_company_url`). Test the empty case.
- [ ] **Instant sources stamp the target** with `set_scrape_target`, or the freshness gate
      cannot tell a scraped company from an untouched one and will re-scrape forever.
- [ ] **Wrap the run in `record_run`** so it appears in `GET /scraper/runs`. That log, not
      the Render logs, is how a failed scrape is noticed. It yields a **dict** — set
      `run["status"]`/`run["note"]`/`run["total"]` on it; `run.result = …` is an attribute
      on a dict and dies at runtime while every mocked test stays green.
      A dispatcher that **catches** a source's exception (run-all does, so one source
      can't sink the rest) must still say so on its row: set `status = "failed"` and
      name the source in `note`, or the run log reads `ok` over an error — Toast's SEC
      scrape failed inside run-all on 2026-10-28 and showed nowhere.
- [ ] **Gate re-runs by the source's own clock, not a TTL.** 13Fs are due 45 days after
      quarter end, so its gate is "has a new deadline passed since the last run" — it
      opens by itself the day after a deadline and never blocks a genuinely new period the
      way a fixed TTL would. Stamp the gate date **only on a completed run** (a crash must
      not count as fresh), give it a `force` escape hatch, and shrink refresh windows to
      since-last-run — the already-ingested period re-read changes nothing.
- [ ] **A heavy scrape is an explicit, role-gated action.** One 13F run is ~100 fetches
      (one per holder — the data lives filer-side); that is a contributor endpoint a
      person invokes, never a side effect of opening a panel.
- [ ] **Honour the `country` argument** — `run(query, depth, country)`. Ask the source to
      filter if its API can (Wikidata's `haswbstatement:P17`, OpenCorporates'
      `jurisdiction_code`); otherwise check the single match with
      `country_match.matches_requested` **before the first write**. A match that states no
      country is rejected when a country was asked for. See the plugin guide for why
      EDGAR's own filter is the wrong field.

- [ ] **A source that speaks in several document families about one pair gets one claim
      per family.** A claim is keyed on (kind, pair, source) — and, for an ownership
      claim, the SEC filing family (`claims.filing_family`: insider, 13dg, 13f, ex21,
      ex81, acquisition). Before 2026-10-26 a Form 4, a 13G and a 13F about one pair
      shared one claim, which held whichever was read last, so the Sources panel showed
      one line where the register had made three statements. A new family is one
      pattern in `_SEC_FAMILIES`; a re-read of a family updates its own claim, so
      amendments never pile up. A source with one document kind needs nothing — the family is read off
      the filing type's text, and anything that is not an SEC form has none.
- [ ] **A pass that fills an edge fills its claim.** A GLEIF stake heal (2026-10) put the record's
      share on 154 edges and left their claims saying "none", so the panel showed GLEIF
      contradicting the edge it supported. A fresh write builds edge and claim from one
      property bag (`_BatchWriter._claim` reads the merged bag — pin that with a test);
      a pass after the import has no bag and must write both, and its integration test
      asserts both.

## 5. Being a good client

- [ ] **Rate limit and identify yourself** — `REQUEST_DELAY` after every request, a real
      `User-Agent` with a contact address. Registers block anonymous scrapers, and rightly.
- [ ] **Reuse one pooled HTTP client.** A per-request client re-resolves DNS every time,
      and on this host that made SEC scraping look broken: every fresh connection spent
      ~6s failing over a dead IPv6 route to sec.gov. `curl` looked fine and will mislead
      you — see the host IPv6 note in the ops docs.
- [ ] **Cache what is immutable, never what is live.** A filing keyed by accession never
      changes and can be kept forever (`sec_cache`); a submissions feed, a search result or
      a daily index grows, and caching it is how a scraper silently stops seeing new records.
      Decide per URL family, in one function, and test both sides of the line.
- [ ] **Back off on 429/5xx**, and respect `Retry-After` when it is sent. (A source using `sec_edgar._get`/`_get_text` inherits this — one capped retry — since the 13F work.)
- [ ] **Probe ~10 real filers before writing a parser, and span the size band** — mega-caps
      publish the cleanest documents, so a parser probed only on them is probed on the
      easy third. The small/mid-cap Exhibit 21 probes are what surfaced a zero-width-space
      filler column (truthy! read as the jurisdiction of all 62 rows), jurisdictions fused
      with legal forms ("Kentucky limited liability company"), ALL-CAPS section-header rows,
      and long-form country names ("The People's Republic of China", curly apostrophe
      included). Capture the cleanest AND the most hostile payload as unit fixtures.
      **A cell's paragraphs are not one string**: join `<p>`/`<br>` with a space, never
      nothing, and check whether paired cells list several records by position
      (HubSpot: ten companies in one row; 14 of 15 lost, names glued). Before changing a
      reader, diff its output over every cached payload, and read what changed. (And
      check helper table shapes before using them: `_US_STATES` maps code→name; the name
      lookup is `_US_STATE_NAMES`.)
- [ ] **Measure the whole population, not the companies you happen to have.** One
      example (AB InBev) produced a rule that worked for 2 of 1,012 20-F filers; the
      measurement over all of them found the real levers (19 % incorporated by
      reference to an earlier filing) and the junk the rules let in (a date column
      read as places, a note's city column). The bulk indexes (EDGAR's quarterly
      `form.idx`) make the whole population a short download; measure through the
      real code path, and count quality (unmapped values) along with coverage.
- [ ] **Look at every "nothing found" result, not just the failures.** A status that
      means "the source has nothing" hides parser misses: Exhibit 21 said
      `no_exhibit` for 11 of the 135 SEC filers in the dev graph (filename patterns,
      a list without a table, a list inside the 20-F's notes) and none of them was
      an error. Open each empty result once and check the source really is empty;
      give a different reason its own status (`no_annual_filing`).
- [ ] **Ask a document what it is before guessing from its name.** Every EDGAR
      document opens with the type its filer declared (`<TYPE>EX-2.1`); a 20-F's
      Exhibit 2.1, the description of securities, is named
      `exhibit21descriptionofsecu.htm` and matched the Exhibit 21 filename pattern
      for 28 of 86 filers. When a source states what a file is, read that first and
      keep the filename as the fallback.
- [ ] **A suffix or abbreviation rule needs a word boundary, and a test with the
      values that merely END in it.** The US-suffix pattern (`US`/`USA` at the end of
      a jurisdiction) had none, so "Mauritius", "Cyprus" and "Belarus" mapped to the
      United States for months, and the country is written fill-only, so a fixed
      re-scrape does not correct the stored value. Run a mapping over every distinct
      value the source produces (`jurisdiction_country` over all places of a sweep)
      and look at what lands where.
- [ ] **Group the unread results by layout before writing a rule — and write
      none for a layout one to five filers use.** The 61 real subsidiary lists that
      read nothing fell into a handful of layouts; a rule per layout read 51 of
      them, measured on the very filings the rules were written against. On filers
      they had never seen (2025 20-Fs, random 10-Ks) the general rules held (722 of
      739 entries right), while the few-filer readers (names without a place, a
      table without a place column, a scanned page's text layer) got 13 % of 926
      entries fully right and mostly hid a gap in the main reader — removed.
- [ ] **A reader that picks names out of single lines sees no context — check what
      the context does to its output.** The one-per-line Exhibit 21 reader ("X
      Limited, a Hong Kong company") was right line by line, yet a combined 10-K's
      lines are the parent group's list (Duke Energy Ohio's exhibit names its own
      parent and siblings) and "(a subsidiary of Idaho Power Company)" was dropped,
      so the rows came out flat under the filer. Read whole documents of the
      layout, not lines, before keeping such a reader; this one was removed although
      it read 7 % of the lists of a random 10-K sample.
- [ ] **Before fixing a "regression", check its premise across the whole sample.**
      One Lavoro row read as "a second company of the same name was lost"; across
      1,062 filings the same pattern was as often one company with a branch abroad,
      and the fix writes branches as companies. Look at every instance the fix
      would touch, not the one that prompted it — then decide knowingly (the user
      chose the split: what the filer listed, one node per country).
- [ ] **Count what a structural rule does across the whole sample, not on the
      filer that prompted it.** "A heading names the parent" was right for the
      filers it was written for; across 1,889 filers 1,166 of 1,196 heading
      parents were the filer under another name, and a later rule (an unknown
      parent drops the stake) turned them into 190 lost stakes. Tally every
      value the rule produces against an independent check (here: is the named
      company in the list?) before building on it.
- [ ] **A rule keyed on another rule's output inherits its errors — and a
      measured loss stays out of the shared database.** "An unresolved parent
      drops the stake" was right on its own; fed the heading rule's 1,166 false
      parents it became 190 lost stakes. Before keying a rule on a value another
      reader produced, tally that value's error rate; and when the measurement of a
      merged change shows it losing data, hold the write (the Exhibit 21 rescrape
      waited for three fix PRs) instead of letting the fix race the damage.
- [ ] **Measure on filings the rules were not written against, and read every
      name.** A count that went up says nothing about precision: check each entry
      of each gained or changed list against the document. Comparing versions on
      the same HTTP responses (a disk cache) keeps the runs honest and cheap.
- [ ] **Then sweep EVERY eligible company in the dev graph before merging** — the 59-filer
      Ex-21 sweep found what ten hand-picked probes still missed: tables whose second
      column is a *location* ("Charlotte, NC" — not a jurisdiction; Bank of America has
      both columns and only a header row tells them apart), an *ownership percentage*
      column (a real stake, worth capturing), spacer-column layouts, and an exhibit
      NUMBERING trap (AB InBev's `dex215.htm` is exhibit 2.15, not 21.5 — filename alone
      cannot decide; try candidates until one parses, and gate headerless tables on
      "most jurisdictions actually map" so a securities listing can't masquerade as a
      subsidiary list). Read tables header-aware — column meaning comes from what the
      filer CALLS it, not from position. And when a bad early run wrote junk, clean it
      up in the same session (delete edges, then degree-0 unidentified nodes — via SQL:
      ArcadeDB Cypher silently ignores `size((b)--())` predicates).
- [ ] **Ask whose document it is.** One filing can belong to several
      registrants (a utility group's combined 10-K, a REIT and its operating
      partnership): the document is reachable from each of their ids, and it
      describes one of them. EDGAR's SGML header names them all; a document that
      lists the filer among its own rows is not the filer's own, and the filer
      keeps only the branch the document draws under its row (NSTAR Electric:
      40 → 1, Entergy Texas: 66 → 0). Know what the rule costs before shipping
      it: a REIT's operating partnership is one flat row of the 388-row list it
      really holds (Vornado), so it reads nothing — 544 entries over three
      partnerships, accepted knowingly. The sentence above Vornado's table
      ("subsidiaries of both the Trust and the L.P.") is the signal a later rule
      could read.
- [ ] **Trust measured behaviour over documented behaviour.** EDGAR's full-text search
      documents 10 results per page and returns ~100, relevance-ordered where date order
      is needed; GLEIF's thumbnail sizes 400 anything off-bucket. Probe the real API once
      and pin what it actually does in a test, with the doc's claim in a comment.
- [ ] **Prefer complete snapshots over delta replay** when a source offers both. Every
      GLEIF publish is a full snapshot back to 2018 — point-in-time state is a download,
      not a fragile chain of thousands of increments.
- [ ] **A rule that needs facts from two imports runs after each of them.** The GLEIF
      registration-day rule compares an edge (relationship import) with a company's
      LEI registration date (company import). The full import loads companies first,
      the test import loads a family's companies after its relationships, so a pass
      only at the end of the relationship import labelled 0 edges on the test DB.
      Run the pass at the end of both imports (it is idempotent), and test the
      rule against the order the test import actually uses.
- [ ] **Bulk imports take the import lock** (`ImportState key='import-lock'`) so two
      dataset loads cannot interleave, and batch their writes — the dev database sits
      behind a 60-second proxy timeout.
- [ ] **In ArcadeDB SQL an edge's endpoint is `@out` / `@in`, not `out` / `in`.**
      `SELECT out.id …` answers null for every edge without an error, so a lookup
      by endpoint silently matches nothing; `@out.id` works. Verify a new edge
      query against a real ArcadeDB, where a mock would have returned what you
      expected.
- [ ] **Every Cypher anchor by id names its label** — `(n:Entity {id: $id})`, never
      `(n {id: $id})`. Without the label ArcadeDB cannot use the per-type id index and
      scans every vertex type: invisible on the dev graph, >400 s on the full import
      (28M vertices), where one such anchor in the profile's voting-groups query turned
      every company page into a timeout. When you do not know whether an id is a
      company or a person, ask `app.db.anchors.node_label()` (two indexed reads) or
      carry `labels(n)[0]` back from the read that found the node. A source-scanning
      test (`tests/test_cypher_anchors.py`) fails the suite on any unlabelled anchor.
- [ ] **Walk a big type through `app.db.paging.iter_id_pages`, never by `@rid > last`
      or `SKIP`.** Both of those are a full scan per page — fine on the dev subset,
      hours on 14M rows. The pager reads bounded ranges of the `id` index (a lower
      AND an upper bound on every page: ArcadeDB 26.7.3 crashes on a one-sided
      ascending range over a big index) and stops only on an empty page (LIMIT counts
      stale index entries, so a short page is not the last one). Measured: 28M
      vertices in 12 min. Pin any new query's plan with `explain_sql()` in an
      integration test — `FETCH FROM INDEX`, never `FETCH FROM TYPE`.

## 6. Failing safely

- [ ] **A `<>` on a nullable property needs an `IS NULL OR` beside it.** ArcadeDB 26.10.1
      made comparisons three-valued, as SQL has them: `type <> 'voting_group'` no longer
      matches a row with no type (26.7.3 matched it), and five readers silently dropped the
      1,298 typeless entities of the dev graph. That fence is gone — a voting group is its
      own vertex type since 2026-10-26, which is the better fix: a thing that is not an
      entity should not be one — but the rule stays for any negative test on a property
      that may be absent: write it as `(p IS NULL OR p <> …)`.

- [ ] **Not found returns `None`/an empty result, never an exception.** Auth problems
      raise `PermissionError` with a message that says which flag or key is wrong.
- [ ] **One source failing must not sink the others.** The dispatchers already catch per
      source — which is exactly why a signature or import error can pass as success. If
      your source ran and wrote nothing, make sure the run log says why.
- [ ] **Re-running is safe.** Same input, same graph: upserts rather than inserts, no
      duplicated edges, no double-counted stakes. Check the CLOSED edges after a re-run,
      not only the active ones: a bulk `CREATE EDGE` importer recreates every ceased
      holding beside its old copy, and the post-import dedupe folded only active twins
      until 2026-10-18 (8 of 29 PSC edges on dev doubled, same appointment link and end
      date). It now groups closed edges by end date as well.
- [ ] **A re-run says what the source says NOW.** After the dedupe, read the survivors'
      `source_date`, `source_url` and stake against the new input — not just the edge
      count. The dedupe keeps the OLD edge whenever it carries something the new one
      lacks (a larger stake, GLEIF's `direct` marker), and until 2026-10-18 the fold then
      ranked the source's two statements equal and kept the incumbent's: three PSC pairs
      on dev still cited the 2016 snapshot after a 2026 re-import, and a changed stake
      would have stayed old the same way. `owns_merge.supersedes` now lets one source's
      newer statement replace its older one; the tie rule between sources is unchanged.
- [ ] **Nothing is written before the record is judged.** Country checks, name checks and
      similarity thresholds all belong *before* the first upsert, so a rejection leaves no
      half-built entity behind.

## 7. Switches and rollout

- [ ] **`SCRAPER_<SOURCE>_ENABLED` defaults to `False`** in `config.py`, and the source
      appears in `KNOWN_SOURCES` with its DB toggle. Three gates: master switch, env flag,
      DB toggle.
- [ ] **API keys come from `settings`**, never a literal, and `.env.example` documents
      every new variable.
- [ ] **`/scraper/status` reports the new flag**, so "is it on?" is answerable without a
      deploy.
- [ ] **Run it once on dev and read what it wrote to `StakeHistory`.** The flap above
      passed every unit test, mutant and integration test; the first real run on dev
      showed it as three history rows for one holder in one run. After a new source's
      first run, list its rows (`SELECT FROM StakeHistory WHERE source_id = …`): more
      than one row per pair from one run, or a `previous_percent` that equals an older
      row's figure, is the writer stating a holding twice. Fix the writer; the rows are
      then deleted and the source re-run (the delete is a dev write — the user's word).
- [ ] **When a vocabulary shrinks, the old rows block the rule.** ArcadeDB refuses to
      declare a REGEXP while a stored row breaks it, and the bootstrap declares rules at
      every start: when `voting_group` left `Entity.type`, the rule sat in `init-schema`'s
      `failed` list until the 38 rows moved. Name the blocked rule in the PR body, since
      the merge deploys itself.
- [ ] **No committed heal before production** (2026-10-27). Production starts from a
      fresh import with the current code, so a repair of what older code wrote is needed
      on dev only, once: a throwaway script, run on the user's word — or dev stays as it
      is (its data may be dropped). After the production start a heal ships with its
      change, runs once, and the next PR removes it. 33 one-off `manage.py` heals were
      removed on 2026-10-27.
- [ ] **A step after a load goes into all four chains.** What the finished graph needs
      (merge across sources, close what a dissolution ended, mark shortcuts, search
      index, geocode) runs in two full imports — dev `~/scripts/steps/finish-import.sh`,
      production `deploy/ops.sh import` — and the parts a daily delta can change run
      again in two nightly updates — dev `~/scripts/cron-gleif-update.sh`, production
      `ops.sh gleif-update`. Adding a step to one means checking the other three:
      production's GLEIF update never re-marked shortcuts, which the dev cron always
      did (found 2026-10-27). `backend/tests/test_deploy_files.py` pins the production
      order; the scripts repo has no tests, so read its two scripts.
- [ ] **`register()` validates against the catalogue** — the `KNOWN_SOURCES` entry must
      exist, the kind must match, and the settings flag must be real (Pydantic rejects
      unknown fields, so a test cannot monkeypatch a flag that was never declared). Patch
      `get_source_enabled` at its canonical home (`app.scraper.scraper_registry`), not at
      an importer's alias — a test patching the wrong module reaches the real database.

## 8. Tests

- [ ] **Unit tests mock the network** — fixtures captured from real payloads, not invented
      ones. Cover: not-found, auth failure, the happy path, and the person/entity split.
- [ ] **Anything that writes gets an integration test against a real ArcadeDB.** The
      mocked suite has passed while the Cypher was broken more than once; dialect and
      result-shape bugs only surface against the real engine.
- [ ] **Never assert equality on `search_text`.** It has a FULL_TEXT index, so
      `WHERE search_text = 'Apple Operations…'` behaves as a *token* match and counted
      three "Apple" rows where one exact row was meant. Count on `name` or an id.
- [ ] **Mocked suites must not reach a database.** When code under a mocked test grows a
      query, stub `app.db.arcadedb.run_sql` — a suite that quietly hits a real server is
      no longer testing what it claims, and repeated failed auth locks ArcadeDB out.
      A shared helper called from a mocked module must use that module's
      `run_command` (pass it in), not import its own. **Prove it** by running
      the unit suite with the local ArcadeDB stopped: any test that still needs a
      database fails loudly instead of writing quietly.
- [ ] **Drive the WRITE path, not just the mapper.** A pure mapper computing a field
      proves nothing about storage: the PSC mapper carried `register_id` for weeks while
      the writer's parameter list silently dropped it, and every mapper unit test stayed
      green. At least one integration test must run record → writer → real database →
      read the fields back.
- [ ] **Fixtures that enumerate edge properties assert against the schema.** The
      edge-parity tests enforce `set(sample) == set(OWNS_PROPS)`, so adding a property
      updates the fixtures — that friction is the feature.
- [ ] **Placeholder values use reserved domains** — `example.com`, `.test`. A made-up
      "real-looking" address has bounced actual email.
- [ ] **A fixture must pass the reader's own gates, like a real document.** The
      combined-filing tests were written with four rows and the indent-tree
      acceptance gate rejected them. The fix is a fixture with enough realistic
      rows (a padding branch), never a loosened gate or a test-only flag. And when
      several fixes leave one base at once, give each its tests in a new file and
      its doc bullets in different places, so the PRs merge in any order (two
      edits to one "Open" bullet conflicted).
- [ ] **Mutation-check the silent failures.** Break the country filter, the dedup key, the
      credibility comparison: if the suite still passes, the test is decorative. The
      failures worth this treatment are the ones that look like success. When judging a
      mutant, compare pass/fail **counts**, never whole summary lines (they embed the
      runtime and match nothing, marking every mutant "killed") — and a mutant nothing
      kills sometimes means the code has a redundant guard to delete, not a missing test.
      Run mutants with `PYTHONDONTWRITEBYTECODE=1` and `__pycache__` cleared: a
      mutant of the same length (`if since:` → `if False:`), written and restored
      within one second, survives in the bytecode cache and fakes every later
      result. Run the unmutated suite again at the end. A mutant that loops
      forever needs a per-run `timeout`, and stopping a hung run means killing
      its exact PID, never a `pkill -f` pattern (it matched the shell itself).

- [ ] **Preview with real identifiers.** A seeded preview with a made-up company number
      sent the register link to a 404 and the first review round was spent proving the
      link format right (Wienerberger Ltd is 05299520, not 01010101). Seed the real
      number of anything the panel links to, and restart the preview backend after a
      backend change — the frontend's fallbacks hid a field the old process did not
      serve, and the reviewer saw "still showing the name".

## 9. Documentation

- [ ] `KNOWN_SOURCES` entry: **id** (the Source node's stable id — part of the data, the same on every instance), type, label, url, description, kind, credibility, quality.
- [ ] [`data-model.md`](data-model.md) if the source adds a property or a node type.
- [ ] [`api-reference.md`](api-reference.md) if it adds or changes an endpoint.
- [ ] A deep-dive under `docs/` if the source needed real research to understand — see
      [`sec_edgar_scraper.md`](sec_edgar_scraper.md) for the shape.
- [ ] The README's source list, so the catalogue and the code agree.
