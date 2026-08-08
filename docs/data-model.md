# Data model

Owlgraph stores an ownership graph in ArcadeDB. Nodes are vertices; ownership
and roles are edges. Location is **not** a node — an entity carries its own HQ
(`hq_address`, `hq_city`, `hq_country`, `hq_lat`, `hq_lng`, `hq_locations[]`) and
its registered address, so the map reads one record instead of traversing.

## Nodes

| Label | Key properties |
|---|---|
| `Entity` | `id`, `name`, `name_normalized`, `type` (company/brand/holding/government/foundation/fund/nonprofit — inferred from Wikidata P31 instance-of and GLEIF legal form), `country`, `countries`, `founded`, `revenue`, `employees` (+ `employees_as_of` year, from Wikidata P1128), `wikidata_id`, `sec_cik`, `lei_id`, `companies_house_id`, `registered_address` (normalized GLEIF registered office — corroborates same-company dedup), `address` (human-readable GLEIF legal address for display), `legal_form` (ISO 20275 ELF name, e.g. "Private Limited Company" — resolved from the LEI-CDF ELF code via the bundled GLEIF ELF list), `registration_authority` + `registration_number` (gleif.org's "Registered At" — register name resolved from the RA code via the bundled GLEIF RA list, plus the entity's id there), `hq_lat`/`hq_lng`/`hq_city`/`hq_country`, `source_id`, `source_statement_ids[]` (BODS statement ids that declared the entity — accumulated for id-less parties collapsed under one name key, so per-statement provenance survives the collapse), `aliases[]` (other names — Wikidata skos:altLabel and SEC EDGAR `formerNames`), `search_text` (FULL_TEXT-indexed: name + description + aliases), `is_nominee` (name-detected nominee/custodian — holder of record, not a beneficial owner; `manage.py flag-nominees` backfills existing) |
| `Person` | `id`, `full_name`, `first_name`, `last_name`, `alias[]`, `nationality`, `birth_date`, `birth_place`, `wikidata_id`, `sec_cik`, `wikipedia_url` |
| `Source` | `id`, `name`, `url`, `type`, `credibility_score`; for peers also `verified`, `key_id` |
| `User` | `id`, `email`, `password_hash`, `role` (admin/contributor/viewer) |
| `ScraperSource` | `name`, `enabled`, `description` |
| `MergeLog` | `id`, `kind` (`person`/`entity`), `keep_id`, `keep_name`, `dup_id`, `dup_name`, `at`, `count` — merge history, deduped by (keep, dup_name) so a re-scraped duplicate bumps `count` rather than adding a row. `kind` keeps the two logs apart |
| `Peer` | `id`, `name`, `base_url`, `credibility_score`, `auth_token`, `public_key`, `enabled` — a trusted federation peer |
| `Claim` | `claim_key` (UNIQUE), `kind` (owns/role/succession), `from_id`, `to_id`, `source_id`, `stake_percent`, `voting_power_pct`, `ownership_type`, `role`, `since`, `until`, `source_url`, `source_date`, `credibility_score`, `first_seen_at`, `last_seen_at` — **what one source asserts about one relationship**; see below |
| `ScrapeRun` | `id`, `source`, `target`, `status` (running/ok/failed), `started_at`, `finished_at`, `total`, `error` — the scrape run log (capped) |

## Relationships

| Pattern | Properties |
|---|---|
| `(Entity\|Person)-[:OWNS]->(Entity)` | `stake_percent` (economic holding), `voting_power_pct` (voting rights — kept separate from the stake; from BODS `votingRights` interests and DEF 14A proxies), `interest_types[]` (BODS interest kinds behind the edge: shareholding/votingRights/appointmentOfBoard/…), `direct_or_indirect` (from GLEIF RR-CDF: `direct` = directly-consolidated parent, `indirect` = ultimate parent), `ownership_type` (see the vocabulary below), `since`, `until`, `source_id`, `source_url`, `source_date`. Free float / >100% conflicts aren't stored — the full-profile endpoint derives them from the disclosed stakes on read (`ownership` summary) |
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
ultimate parents) from RR-CDF; `gleif-succession` imports mergers from LEI-CDF.
This replaces the OpenOwnership GLEIF BODS export (`bods-gleif`), which was frozen
at 2025‑03.

Once the full copy is loaded, `manage.py gleif-update` applies GLEIF's published
**delta files** (only records changed since the last publish) as a fast daily
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
