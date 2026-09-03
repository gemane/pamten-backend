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
- [ ] **Registration and headquarters are different facts.** `country`/`address` is where
      a company is registered, `hq_*` where it is run. Never coalesce them — the map's
      Registered/Headquarters switch exists precisely because they differ.
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
- [ ] **A stated register is a hard identifier — mint it through `gleif_reference`.**
      `make_register_id(code, number)` (placeholder RAs excluded, zero-normalization for
      the audited US registers where sources demonstrably disagree on padding);
      `sole_register_for_country` for country-only statements; `register_for_place` for
      sub-national ones — **audited countries only**: the raw exactly-one rule would have
      stamped Bavarian HRB numbers onto a Foundations Directory. And read *every* field
      the source might put the register in: real filings had it in `legal_authority` with
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
- [ ] **Classify people with `is_person_name`** and know it is a heuristic. Registers list
      corporate nominees as officers, and `is_nominee` marks holders of record who are not
      beneficial owners.

## 4. Provenance, freshness, and the country

- [ ] **Every fact carries its source.** `source_id` on nodes and edges, and
      `record_claim` for the per-source assertion — that is what makes a conflict
      inspectable later instead of a mystery.
- [ ] **`credibility_score` is written on the edge**, not assumed from the source name.
- [ ] **Instant sources stamp the target** with `set_scrape_target`, or the freshness gate
      cannot tell a scraped company from an untouched one and will re-scrape forever.
- [ ] **Wrap the run in `record_run`** so it appears in `GET /scraper/runs`. That log, not
      the Render logs, is how a failed scrape is noticed.
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
- [ ] **Back off on 429/5xx**, and respect `Retry-After` when it is sent. (A source using `sec_edgar._get`/`_get_text` inherits this — one capped retry — since the 13F work.)
- [ ] **Trust measured behaviour over documented behaviour.** EDGAR's full-text search
      documents 10 results per page and returns ~100, relevance-ordered where date order
      is needed; GLEIF's thumbnail sizes 400 anything off-bucket. Probe the real API once
      and pin what it actually does in a test, with the doc's claim in a comment.
- [ ] **Prefer complete snapshots over delta replay** when a source offers both. Every
      GLEIF publish is a full snapshot back to 2018 — point-in-time state is a download,
      not a fragile chain of thousands of increments.
- [ ] **Bulk imports take the import lock** (`ImportState key='import-lock'`) so two
      dataset loads cannot interleave, and batch their writes — the dev database sits
      behind a 60-second proxy timeout.

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
- [ ] **Mocked suites must not reach a database.** When code under a mocked test grows a
      query, stub `app.db.arcadedb.run_sql` — a suite that quietly hits a real server is
      no longer testing what it claims, and repeated failed auth locks ArcadeDB out.
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

## 9. Documentation

- [ ] `KNOWN_SOURCES` entry: label, url, description, kind, credibility, quality.
- [ ] [`data-model.md`](data-model.md) if the source adds a property or a node type.
- [ ] [`api-reference.md`](api-reference.md) if it adds or changes an endpoint.
- [ ] A deep-dive under `docs/` if the source needed real research to understand — see
      [`sec_edgar_scraper.md`](sec_edgar_scraper.md) for the shape.
- [ ] The README's source list, so the catalogue and the code agree.
