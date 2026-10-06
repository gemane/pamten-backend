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
      + 627 M through subsidiaries; the sole row alone became 0.02 %). Check that
      `shares ÷` its own total gives back the filed percentage, and don't recalculate
      a row where it doesn't.
- [ ] **Registration and headquarters are different facts.** `country`/`address` is where
      a company is registered, `hq_*` where it is run. Never coalesce them — the map's
      Registered/Headquarters switch exists precisely because they differ.
- [ ] **Keep the finest grain the source states — do not collapse it to the country.**
      Exhibit 21 states "Florida, USA"; storing only `country: US` threw Florida away, and
      a subsidiary's panel showed "United States" with no real location (the reported bug).
      Map to BOTH `country` and `jurisdiction_code` (ISO 3166-2, e.g. `US-FL`) so the panel
      can show "Registered in: Florida". The UI only names some families (US/CA/GB/AE/KN),
      so emit codes for those; elsewhere a region is administrative, not a domicile choice.
- [ ] **Address parts stay separate** (`*_street`, `*_city`, `*_postcode`, `*_country`).
      Geocoding is a structured query; re-parsing an assembled string is guesswork, and
      every country writes an address differently.
- [ ] **Keep values as the source gave them.** Do not strip a legitimate `C/O …` line or
      "normalise" a name to make a downstream tool happy — fix the tool-facing side. A
      stored value that no source ever said is a lie the graph cannot walk back.
- [ ] **Dates carry their real precision.** UK PSC publishes month and year only; storing
      a fabricated day makes two different people look like the same one.
- [ ] **A new edge property goes into `edge_schema.OWNS_PROPS` first.** `owns_props()`
      raises on unknown keywords by design, and the merge paths derive their recreate
      lists from the schema — a property added to a writer but not the schema is silently
      stripped by the next merge. Check the sources endpoint's `_COLS` too: it is a
      sibling of the same vocabulary.
- [ ] **If the source's records are filings, stamp `filing_type`** ("13G/A", "13F", "RR",
      "PSC") — the source names the register, this names the *rulebook* the fact lives
      under, which is what a reader needs to judge it.
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
      shipped, and needed a routing workaround plus a data migration).
- [ ] **Match on a hard identifier when the source has one.** Shared LEI/CIK/CH number is
      proof; a shared name is a hint.
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
- [ ] **Use the existing writers** — `_upsert_entity_by_name`, `_upsert_person_by_name`,
      `resolve_entity_id`. They already implement match-by-id-then-name-then-normalised,
      and the credibility rule that stops a weak source renaming a company.
- [ ] **Run the whole scrape inside one `@_with_autodedup` scope.** The person auto-merge
      only groups duplicates touched *together* in one scope, so per-source scopes leave
      cross-source pairs unmerged — Wikidata "Larry Page" beside SEC "Page Lawrence" is the
      bug that taught us this.
- [ ] **Expect re-runs to recreate duplicates** and dedup afterwards. Scraping the same
      company twice from two sources is the normal case, not an error.
- [ ] **Never write past a merge.** A merged node leaves a `MergedId` forwarding row and
      by-id reads follow it; resolve before writing rather than resurrecting the loser.
- [ ] **One edge per pair.** Re-asserting an existing relationship updates it; it does not
      add a second one.
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
- [ ] **Layout is data only when it is unambiguous.** Exhibit 21 filers draw the
      group tree by indentation or "Subsidiaries of X" headings — and the same
      signals appear where there is no tree (a uniform hanging indent, a page
      heading repeated on every page, a heading naming the filer). Read a tree
      only under strict acceptance rules with a flat fallback, stamp the basis on
      what you inferred (`structure_basis`), and test on a real sample: 62 exhibits
      found every shape above.
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
        a bare `source_date` marks it as "not documented for that year".
      - **An end**: when the source says a holding or role is over, close the edge
        with `until` (+ `until_reason`), never delete it. A deleted edge is missing
        from every past year too. A 0 % amendment is an exit, not a 0 % holding.
      - **"As of" vs "said on"**: the day the facts were true (13D/G *date of event*
        → `event_date`, a register's snapshot date) is a different fact from the day
        they were published (`source_date`). Store both; never use one as the other.
      - **Precision**: a month-only or year-only date stays that way (`2023-04-00`,
        a bare year). A made-up day moves an edge across a year boundary.
      - **A snapshot source forgets**: a golden copy or register dump shows today,
        and what ended is simply gone from it. Look for the source's archive of
        old snapshots (GLEIF keeps every publish since 2018, `gleif-rr-history`
        diffs one a month); deltas usually only cover the last days.
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
      - **What it can't do yet**: an edge holds the latest amendment's numbers, so a
        past year shows today's stake. If the source publishes history (amendments,
        annual lists, archived snapshots), say in the source doc what is kept and
        what is lost.
      - **Test it**: one IT that writes the source's edges and reads the profile
        with an `as_of` before, inside and after the dated range.
- [ ] **Instant sources stamp the target** with `set_scrape_target`, or the freshness gate
      cannot tell a scraped company from an untouched one and will re-scrape forever.
- [ ] **Wrap the run in `record_run`** so it appears in `GET /scraper/runs`. That log, not
      the Render logs, is how a failed scrape is noticed. It yields a **dict** — set
      `run["status"]`/`run["note"]`/`run["total"]` on it; `run.result = …` is an attribute
      on a dict and dies at runtime while every mocked test stays green.
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
      included). Capture the cleanest AND the most hostile payload as unit fixtures. (And
      check helper table shapes before using them: `_US_STATES` maps code→name; the name
      lookup is `_US_STATE_NAMES`.)
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

- [ ] **Not found returns `None`/an empty result, never an exception.** Auth problems
      raise `PermissionError` with a message that says which flag or key is wrong.
- [ ] **One source failing must not sink the others.** The dispatchers already catch per
      source — which is exactly why a signature or import error can pass as success. If
      your source ran and wrote nothing, make sure the run log says why.
- [ ] **Re-running is safe.** Same input, same graph: upserts rather than inserts, no
      duplicated edges, no double-counted stakes.
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

## 9. Documentation

- [ ] `KNOWN_SOURCES` entry: label, url, description, kind, credibility, quality.
- [ ] [`data-model.md`](data-model.md) if the source adds a property or a node type.
- [ ] [`api-reference.md`](api-reference.md) if it adds or changes an endpoint.
- [ ] A deep-dive under `docs/` if the source needed real research to understand — see
      [`sec_edgar_scraper.md`](sec_edgar_scraper.md) for the shape.
- [ ] The README's source list, so the catalogue and the code agree.
