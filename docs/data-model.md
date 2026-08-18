# Data model

Owlgraph stores an ownership graph in ArcadeDB. Nodes are vertices; ownership
and roles are edges. Location is **not** a node — an entity carries its own HQ
(`hq_address`, `hq_city`, `hq_country`, `hq_lat`, `hq_lng`, `hq_locations[]`) and
its registered address (`address`, `reg_lat`, `reg_lng`), so the map reads one
record instead of traversing. Both are geocoded, because where a company is run
and where it is registered are different places and the map can show either.

## Nodes

| Label | Key properties |
|---|---|
| `Entity` | `id`, `name`, `name_normalized`, `type` (company/brand/holding/government/foundation/fund/nonprofit — inferred from Wikidata P31 instance-of and GLEIF legal form), `country`, `countries`, `founded`, `revenue`, `employees` (+ `employees_as_of` year, from Wikidata P1128), `wikidata_id`, `sec_cik`, `lei_id`, `companies_house_id`, `registered_address` (normalized GLEIF registered office — corroborates same-company dedup), `address` (human-readable GLEIF legal address for display), `legal_form` (ISO 20275 ELF name, e.g. "Private Limited Company" — resolved from the LEI-CDF ELF code via the bundled GLEIF ELF list), `registration_authority` + `registration_number` (gleif.org's "Registered At" — register name resolved from the RA code via the bundled GLEIF RA list, plus the entity's id there), `jurisdiction_code` (ISO 3166-2 legal jurisdiction where a source gives one, e.g. `US-DE` — see *How countries are represented* below; sparse, ~1% of GLEIF records), `hq_lat`/`hq_lng`/`hq_city`/`hq_country`/`hq_street`/`hq_postcode` (where it is **run**), `reg_lat`/`reg_lng`/`reg_geo_precision`/`reg_street`/`reg_city`/`reg_postcode` (where it is **registered** — geocoded from `address`; the two differ exactly where it matters, e.g. an agent's office on Grand Cayman versus a London headquarters, and the map's Registered/Headquarters switch draws one or the other; the `_street`/`_city`/`_postcode` parts are kept **as the source gave them** so geocoding is a structured query rather than an attempt to re-parse an assembled string — every country writes an address differently, and picking the city out of a comma-separated line is guesswork), `source_id`, `source_statement_ids[]` (BODS statement ids that declared the entity — accumulated for id-less parties collapsed under one name key, so per-statement provenance survives the collapse), `aliases[]` (other names — Wikidata skos:altLabel and SEC EDGAR `formerNames`), `search_text` (FULL_TEXT-indexed: name + description + aliases), `is_nominee` (name-detected nominee/custodian — holder of record, not a beneficial owner; `manage.py flag-nominees` backfills existing), `validation_sources` (GLEIF's own statement of how far it checked the record — `FULLY_CORROBORATED` / `PARTIALLY_CORROBORATED` / `ENTITY_SUPPLIED_ONLY` / `PENDING`; scales `name_credibility`, see *How far GLEIF checked a record* below), `no_direct_parent_reason` / `no_ultimate_parent_reason` (+ `_reference`) (why the company reports no parent, from GLEIF's reporting-exceptions file — see *Why a company reports no parent* below) |
| `GeoCache` | `query` (the cleaned address, UNIQUE), `lat`, `lng`, `precision`, `checked_at` — an address→coordinate cache so a shared registered-agent building is geocoded once rather than once per company registered there (24 dev companies share one Wilmington address). Misses are cached too, with the date, and retried after 30 days: re-asking Nominatim about an address OpenStreetMap does not have is the most wasteful thing the geocoder can do, and a permanent "no" would be a lie. |
| `ScrapeMiss` | `key` (normalised company name + `|` + ISO-2 country, or an empty country for an unrestricted search — UNIQUE), `missed_at` — an on-demand search that found nothing. The freshness gate protects the sources by looking at the *company* (`last_scraped_at`, `scrape_depth`), and a search that found nothing has no company to hang that on, so every repeat used to ask every source the same hopeless question again. Honoured for `SCRAPER_ONDEMAND_COOLDOWN_HOURS`, cleared by a later successful scrape of the same name+country, and expired rows are deleted when next looked up. The country is part of the key because France having no "Alphabet" says nothing about Germany. |
| `SearchDemand` | `key` (normalised query + `|` + ISO-2 country, UNIQUE), `query`, `country`, `searches`, `zero_results`, `selected`, `first_seen`, `last_seen` — one row per question asked, never per person asking. `zero_results` is the useful one: demand the graph could not answer, in demand order. Pruned after 365 idle days |
| `UsageCounter` | `key` (an allow-listed event name, UNIQUE), `count`, `first_seen`, `last_seen` — feature usage and clicked result positions (`result.rank.3`). The key space is closed because a public endpoint feeds it |
| `EndpointStat` | `key` (`METHOD route-template status-class latency-band`, UNIQUE), `count`, `last_seen` — request health. Keyed on the route **template**, never the path, so no record id is ever a key |
| `Person` | `id`, `full_name`, `first_name`, `last_name`, `alias[]`, `nationality` (**ISO-3166-1 alpha-2**, e.g. `GB` — Companies House records a demonym like "British", normalised on import; a value that cannot be recognised is kept verbatim rather than blanked, and `manage.py normalize-nationalities` reports the residue), `birth_date` (**month and year only** where the source publishes no more, which is the case for UK PSC), `birth_place`, `wikidata_id`, `sec_cik`, `wikipedia_url` |
| `Source` | `id`, `name`, `url`, `type`, `credibility_score`; for peers also `verified`, `key_id` |
| `User` | `id`, `email`, `password_hash`, `role` (admin/contributor/viewer), `language` (UI language at registration — emails to this person are written in it) |
| `ScraperSource` | `name`, `enabled`, `description` |
| `MergeLog` | `id`, `kind` (`person`/`entity`), `keep_id`, `keep_name`, `dup_id`, `dup_name`, `at`, `count` — merge history, deduped by (keep, dup_name) so a re-scraped duplicate bumps `count` rather than adding a row. `kind` keeps the two logs apart |
| `Peer` | `id`, `name`, `base_url`, `credibility_score`, `auth_token`, `public_key`, `enabled` — a trusted federation peer |
| `Claim` | `claim_key` (UNIQUE), `kind` (owns/role/succession), `from_id`, `to_id`, `source_id`, `stake_percent`, `voting_power_pct`, `ownership_type`, `role`, `since`, `until`, `source_url`, `source_date`, `credibility_score`, `first_seen_at`, `last_seen_at` — **what one source asserts about one relationship**; see below |
| `ScrapeRun` | `id`, `source`, `target`, `status` (running/ok/failed), `started_at`, `finished_at`, `total`, `error` — the scrape run log (capped) |

## Relationships

| Pattern | Properties |
|---|---|
| `(Entity\|Person)-[:OWNS]->(Entity)` | `stake_percent` (economic holding), `voting_power_pct` (voting rights — kept separate from the stake; from BODS `votingRights` interests and DEF 14A proxies), `interest_types[]` (BODS interest kinds behind the edge: shareholding/votingRights/appointmentOfBoard/…), `direct_or_indirect` (from GLEIF RR-CDF: `direct` = directly-consolidated parent, `indirect` = ultimate parent), `also_ultimate` / `ultimate_since` / `ultimate_until` (see *One edge per pair* below), `shortcut` (set by `manage.py mark-shortcuts`: this indirect edge duplicates a path of direct edges. Edges marked `true` are excluded from the full-profile owners/subsidiaries lists **and their counts**, so the panel and the graph agree; absent means unproven and it is kept), `ownership_type` (see the vocabulary below), `since`, `until`, `source_id`, `source_url`, `source_date`. Free float / >100% conflicts aren't stored — the full-profile endpoint derives them from the disclosed stakes on read (`ownership` summary) |
| `(Person)-[:HAS_ROLE]->(Entity)` | `role`, `since`, `until`, `source_id`, `source_url`, `source_date` |
| `(Person)-[:RELATED_TO]->(Person)` | `relation`, `source_id` |
| `(Person)-[:NOT_DUPLICATE]->(Person)`<br>`(Entity)-[:NOT_DUPLICATE]->(Entity)` | `at` — marks two nodes confirmed to be *different* (keep-separate). The entity dedup checks these **per pair**, not per group: a third same-named company must not drag a node someone explicitly separated into a destructive auto-merge |
| `(Entity)-[:DUAL_LISTED_WITH]->(Entity)` | links share classes of a dual-listed company |
| `(Entity)-[:SUCCEEDED_BY]->(Entity)` | corporate succession/rename, directed predecessor → successor (e.g. Twitter → X Corp.); from Wikidata P1366/P1365 (with `since` from the P585 qualifier) and from GLEIF LEI-CDF (MERGED/DUPLICATE → `SuccessorLEI`, keyed `lei:{LEI}`). `since`, `source_id`, `source_url`, `source_date` |

`until = null` means the relationship is currently active.  
`ownership_type` is a closed vocabulary, defined once as `OwnershipType` in
[`app/models/relationship.py`](../backend/app/models/relationship.py) and derived by
`derive_ownership_type` in [`app/scraper/mapper.py`](../backend/app/scraper/mapper.py):

| Value | Meaning |
|---|---|
| `full` | >= 99% — essentially wholly owned |
| `majority` | > 50% — outright control |
| `controlling` | >= 20% — significant blocking minority |
| `minority` | > 0% — passive stake |
| `unknown` | owner known, stake undisclosed — the most common case, and deliberately not guessed |

Values arriving from outside are coerced onto this set (`coerce_ownership_type`),
and a moderator pin is validated against it at the API boundary. `free_float` is
**not** a stored value: the widely-held remainder is derived on read as
`free_float_pct`, because nobody holds the free float.

Vertex/edge types and lookup indexes are created idempotently on startup and via
`python manage.py init-schema` (see the README's *Deployment → Schema & indexes*).

## Claims: per-source provenance

An `OWNS` edge holds **one** answer — the value traversals read. Several sources
routinely assert the same relationship with different numbers (GLEIF and
Companies House will disagree about a stake, and both are right about their own
register). Those used to be lost: the second writer overwrote the first, and the
Sources panel reconstructed attribution by guessing from which identifier fields
happened to be populated.

A `Claim` records what **one source** said. The edge is unchanged — still the
single, fast, current-best answer — and the claims sit beside it as the evidence:

```
(:Entity)-[:OWNS {stake_percent: 60}]->(:Entity)      <- traversals read this
(:Claim {kind:'owns', from_id, to_id, stake_percent: 60, source_id:'gleif'})
(:Claim {kind:'owns', from_id, to_id, stake_percent: 75, source_id:'ch-psc'})
```

- **Keyed** on `claim_key` = digest of (kind, from_id, to_id, source_id), UNIQUE.
  A source re-asserting the same relationship updates its own row, so re-imports
  are idempotent here even though the edges still need a dedup pass. The parts
  are length-prefixed before hashing, so an id containing the separator cannot
  make two different claims collide.
- **Written by the edge writers themselves** — `_BatchWriter.owns/role/succeeded_by`
  for bulk imports and `record_claim` in the incremental scrapers — so an importer
  cannot record an edge and forget the evidence.
- **Which claim wins**: highest `credibility_score`, ties broken by the most
  recent `source_date` (`best_claim` in [`app/claims.py`](../backend/app/claims.py)).
  A credible "owns, amount undisclosed" deliberately beats a weak source's number.
- **Read** by the Sources panel via `to_id` — everything asserted *about* an
  entity. Claims about the subsidiaries it owns carry `from_id` = that entity and
  are never selected, so the panel cannot flood with one row per subsidiary.

An entity's own record provenance is a different question, answered from its hard
identifiers (`_entity_own_source_rows`) — claims describe relationships, not nodes.

## GLEIF sourcing

GLEIF data comes from the **GLEIF golden copy** (current, daily), keyed `lei:{LEI}`:
`manage.py gleif-lei-cdf` imports the **entities** (name, country, legal address,
legal-form type) from LEI-CDF; `gleif-rr` imports the **relationships** (direct/
ultimate parents) from RR-CDF; `gleif-succession` imports mergers from LEI-CDF;
`gleif-repex` imports the **reasons a company gives for reporting no parent**.
This replaces the OpenOwnership GLEIF BODS export (`bods-gleif`), which was frozen
at 2025‑03.

Worth knowing about the provenance, because it shapes how much weight the data
carries: **GLEIF collects almost none of it itself.** An entity applies for an LEI
to an accredited LOU (~40 of them — LSEG, Bloomberg, GS1, WM Datenservice …) and
supplies its own reference data; the LOU validates it against the national
business register where one exists, and the record names that register in
`RegistrationAuthority`. So the real upstream source is ~200 national registers,
and GLEIF is the normalising layer over them. Level 1 (who is who) is
registry-corroborated and reliable; Level 2 (who owns whom) is **accounting
consolidation, not shareholding** — no percentages — and can be declined, which is
what the reporting exceptions record.

### How far GLEIF checked a record

Every LEI record carries `ValidationSources`, GLEIF's own statement of whether an
LOU corroborated it against the register or simply took the entity's word for it.
It is the only per-record quality signal in the file, and it scales the
credibility we stamp on the entity rather than every GLEIF record scoring the
source's flat 92:

| `ValidationSources` | `name_credibility` | meaning |
|---|---|---|
| `FULLY_CORROBORATED` | 92 | an LOU checked it against the business register |
| `PARTIALLY_CORROBORATED` | 88 | some of it, or against a source short of the register |
| `ENTITY_SUPPLIED_ONLY` | 82 | nobody checked; the entity said so |
| `PENDING` | 82 | validation unfinished — the same evidential state |
| absent / unrecognised | 92 | no penalty for a field we could not read |

That score decides which source's name survives a conflict and which claim wins
in [`claims.py`](../backend/app/claims.py), so without it a self-declared name
outranked a registry-checked one from elsewhere on the strength of its source
alone. The deductions are deliberately small: a company's own statement of its own
legal name is still better evidence than a Wikidata label (usually the common name
rather than the registered one), so an uncorroborated GLEIF record stays above the
community sources at 80 while losing to a corroborated one. On a day's delta of
18,166 records: 98.7% fully corroborated, 0.8% entity-supplied, 0.4% partial.

### Why a company reports no parent

GLEIF's commonest Level 2 answer is silence, and it has two very different
meanings. Either nobody has ever said anything — absence of evidence — or the
company filed a **reporting exception**: a published declaration that it is
declining to name a parent, and why. `manage.py gleif-repex` imports the second,
from the golden copy's third section (`repex`), onto
`no_direct_parent_reason` / `no_ultimate_parent_reason` (the two are separate
questions and a company can answer them differently; most filers state both).

| reason | what it tells an ownership map |
|---|---|
| `NATURAL_PERSONS` | the trail leads to people, not another company — exactly where GLEIF stops and UK PSC / SEC take over |
| `NON_CONSOLIDATING` | a parent exists but does not consolidate the accounts, so GLEIF's accounting-based Level 2 would never carry it |
| `NO_LEI` | the parent is real and outside the system; sometimes named in `ExceptionReference`, kept as `…_reference` |
| `NO_KNOWN_PERSON` | nobody controls it |
| `NON_PUBLIC`, `CONSENT_NOT_OBTAINED`, `LEGAL_OBSTACLES`, `DISCLOSURE_DETRIMENTAL`, … | a parent exists and is being withheld |

The importer **never creates a node**: writes are `UPDATE … WHERE id` with no
`UPSERT`, because the file describes hundreds of thousands of companies and a
statement about one this database does not carry must land nowhere rather than
mint a node whose only content is "has no parent, because". Reasons are stored as
GLEIF's own enum values; the UI turns them into prose.

### How countries are represented, and one grouping we deliberately do not apply

Countries are ISO 3166-1 alpha-2. Where a source records a sub-national legal
jurisdiction, GLEIF gives it as ISO 3166-2 (`US-DE`). ISO's distinction happens to
be exactly the one that matters here:

| | code | why |
|---|---|---|
| Delaware | `US-DE` | a **subdivision** — Delaware genuinely is part of the US |
| Cayman Islands | `KY` | its **own country** — a British Overseas Territory, *not* part of the UK |

Cayman, and likewise Jersey `JE`, Guernsey `GG`, Isle of Man `IM`, BVI `VG`,
Bermuda `BM` and Gibraltar `GI`, are under UK sovereignty but are separate
jurisdictions with their own legislatures, company law and tax regimes. **Do not
fold them into `GB`.** It is legally wrong, and it would erase the very fact that
makes them worth showing: a company registers in the Caymans precisely *because*
it is not the UK. Barclays Capital (Cayman) is `KY` by jurisdiction and `GB` by
headquarters, and collapsing the two would turn an offshore structure into "a
British company".

**A future lens, not a data change.** "The UK and its dependencies" is a genuinely
useful analytical grouping — the UK has repeatedly been pressed to make its
Overseas Territories adopt public beneficial-ownership registers, so *how much of
this structure sits in the British orbit* is a real question. If that is ever
offered, it must be a **curated list layered over the ISO codes** and labelled as
such, never a remapping of the underlying data. ISO will not supply it, because
legally it is not true.

Which countries actually carry subdivisions is narrow, and measured rather than
assumed — from 250,000 LEI records: **US 90%** (Delaware dominant), **CA 74%**,
**KN 40%** (Nevis), **AE 27%** (Dubai, Abu Dhabi), **MY 19%** (Labuan), **GB 0.3%**.
China, India, Switzerland, Germany and Australia record none: their registers are
regionally administered, but the region is not a choice of legal domicile, so
there is nothing analogous to Delaware to capture.

### Filling a missing country

`manage.py backfill-countries` fills `country` where it is blank, from Wikidata
(batched P17, falling back to the headquarters' country) and SEC EDGAR. It only
ever fills a blank — an existing country is never overwritten.

Worth knowing why it was needed: subsidiaries and owners are written as *stubs*
when some other company is scraped, with no country of their own, so a company
that only ever appeared as an owner had none at all. BlackRock and The Vanguard
Group were both missing from the map for that reason. The Wikidata scraper now
fetches those countries during the scrape, so new stubs arrive with one.

For SEC filers the order is deliberate: **state of incorporation first, business
address only as a fallback.** EDGAR's business address is where the *filing* comes
from — DEUTSCHE BANK AKTIENGESELLSCHAFT lists New York — so trusting it would move
German banks to the United States. A filer with only a US address and no stated
incorporation is left blank, because a wrong country is worse than none.

The live scrapers now set a country themselves, so the pass is a repair for older
records rather than a routine step: the Wikidata scraper fetches related companies'
countries during a scrape, and the SEC scraper reads the filer's from EDGAR.

Jurisdiction and headquarters are kept in **separate fields throughout**. Wikidata's
P17 is a legal domicile and goes to `country`; the country of its P159 headquarters
goes to `hq_country`. Coalescing them is tempting when one is missing, and it is
what the Registered/Headquarters switch on the map exists to avoid — an early
version of the backfill did coalesce, and wrote Morgan, Grenfell & Co.'s London
headquarters into its jurisdiction field, which the source never claimed.

### One edge per pair

Whenever a company's direct parent is also the top of its tree, GLEIF states the
pair **twice** — once `IS_DIRECTLY_CONSOLIDATED_BY` and once
`IS_ULTIMATELY_CONSOLIDATED_BY`. On the full golden copy that is **88,839 of
257,651** consolidation relationships (35%).

Those are two statements about one holding, so `gleif-rr` folds them into a single
OWNS edge rather than writing one each. Two parallel edges between the same nodes
used to mean the graph drew one and hid the other, the profile listed the owner
twice, and `mark-shortcuts` had to *prove* the second redundant before anything
could filter it.

Nothing is discarded:

| Property | Meaning |
|---|---|
| `direct_or_indirect` | `direct` when GLEIF stated the direct relationship — the more specific claim wins |
| `also_ultimate` | `true` when the ultimate relationship was stated for the same pair, i.e. this direct parent is also the top of the tree |
| `ultimate_since` / `ultimate_until` | the ultimate relationship's period, kept only when it **differs** from the direct one (6.9% of folded pairs — a parent can become the direct consolidator years before an intermediate holding dissolves and makes it the ultimate one) |

An ultimate record whose pair has no direct record keeps `direct_or_indirect =
indirect` and is *not* rewritten as direct: GLEIF never stated a direct holding
there, and that edge is often the only route to the company.

`mark-shortcuts` still runs, but only the genuinely multi-hop shortcuts are left
for it to prove.

The daily `gleif-update` delta maintains the same invariant: it looks its edges up
by **pair**, not by marker, so an ultimate-parent record for a folded pair updates
that edge instead of creating a parallel one. Retiring one of a folded edge's two
relationships does not close the edge — the other still stands, so the edge either
drops `also_ultimate` (the ultimate link ended) or reverts to `indirect` with its
own period (the direct holding ended).

Once the full copy is loaded, `manage.py gleif-update` applies GLEIF's published
**delta files** for all three sections — entities, relationships and reporting
exceptions (only records changed since the last publish) — as a fast daily
refresh — see [`app/scraper/gleif_incremental.py`](../backend/app/scraper/gleif_incremental.py).
It is **retirement-aware**: a relationship whose `RelationshipStatus` becomes
non-ACTIVE has its `OWNS` edge *closed* (`until` = the relationship period's
`EndDate`), and an entity whose `EntityStatus` is `INACTIVE` is flagged
`active=false` with its `gleif_registration_status` recorded — neither is ever
deleted (GLEIF never deletes; merges keep flowing through `SUCCEEDED_BY`). All
writes are idempotent — nodes UPSERT by id (batched) and edges are matched by
endpoints+marker before create — so re-applying a delta can't duplicate, hence no
`--bulk-load` and no whole-DB dedup. The default `--interval auto` is **gap-aware**:
it checkpoints the last publish it applied (an `ImportState` node) and picks the
smallest delta window (`LastDay`/`LastWeek`/`LastMonth`) that covers the gap since
then, so a few missed daily runs self-heal on the next one; a gap past ~30 days
can't be covered by a delta and fails loudly (→ full reload). UK PSC has no
equivalent clean delta feed
(Companies House republishes a daily *full* snapshot), so its incremental refresh
is deferred to a separate design.

## UK PSC sourcing

UK beneficial ownership comes from the **Companies House PSC snapshot** (current,
published daily), imported by `manage.py ch-psc`. Each line is one PSC controlling
a company identified only by its **company number**, so the controlled company is a
node keyed `gb-coh:{number}` — this import ensures it exists but leaves it un-named.
Company **names, addresses, incorporation dates and former names** are filled in by
the companion `manage.py ch-company-data` importer from the Companies House
**BasicCompanyData** product (the full UK register, monthly). That importer is a
pure *enrichment* pass — it only `UPDATE`s companies already in the graph (matched
on `gb-coh:{number}`), so the 5.6M-row register never creates isolated company
nodes; former names land in `aliases` + `search_text`. Individuals become `Person`
nodes, corporate/legal PSCs become
`Entity` nodes (keyed on their own UK company number where present). `natures_of_control`
map to `stake_percent` (ownership-of-shares band floor), `voting_power_pct` (voting-rights
band floor) and `ownership_type` (`controlling` when they carry voting / appointment /
significant-influence rights). This replaces the OpenOwnership UK PSC BODS export
(`bods-uk-psc`), which was frozen at 2025‑03.
