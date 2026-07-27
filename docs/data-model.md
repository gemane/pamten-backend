# Data model

Pamten stores an ownership graph in ArcadeDB. Nodes are vertices; ownership,
roles, and locations are edges.

## Nodes

| Label | Key properties |
|---|---|
| `Entity` | `id`, `name`, `name_normalized`, `type` (company/brand/holding/government/foundation/fund/nonprofit — inferred from Wikidata P31 instance-of and GLEIF legal form), `country`, `countries`, `founded`, `revenue`, `employees` (+ `employees_as_of` year, from Wikidata P1128), `wikidata_id`, `sec_cik`, `lei_id`, `companies_house_id`, `registered_address` (normalized GLEIF registered office — corroborates same-company dedup), `hq_lat`/`hq_lng`/`hq_city`/`hq_country`, `source_id`, `source_statement_ids[]` (BODS statement ids that declared the entity — accumulated for id-less parties collapsed under one name key, so per-statement provenance survives the collapse), `aliases[]` (other names — Wikidata skos:altLabel and SEC EDGAR `formerNames`), `search_text` (FULL_TEXT-indexed: name + description + aliases), `is_nominee` (name-detected nominee/custodian — holder of record, not a beneficial owner; `manage.py flag-nominees` backfills existing) |
| `Person` | `id`, `full_name`, `first_name`, `last_name`, `alias[]`, `nationality`, `birth_date`, `birth_place`, `wikidata_id`, `sec_cik`, `wikipedia_url` |
| `Location` | `id`, `city`, `country`, `latitude`, `longitude` |
| `Source` | `id`, `name`, `url`, `type`, `credibility_score`; for peers also `verified`, `key_id` |
| `User` | `id`, `email`, `password_hash`, `role` (admin/contributor/viewer) |
| `ScraperSource` | `name`, `enabled`, `description` |
| `MergeLog` | `id`, `keep_id`, `keep_name`, `dup_name`, `at`, `count` — history of person merges (deduped by keep+dup name) |
| `Peer` | `id`, `name`, `base_url`, `credibility_score`, `auth_token`, `public_key`, `enabled` — a trusted federation peer |
| `ScrapeRun` | `id`, `source`, `target`, `status` (running/ok/failed), `started_at`, `finished_at`, `total`, `error` — the scrape run log (capped) |

## Relationships

| Pattern | Properties |
|---|---|
| `(Entity\|Person)-[:OWNS]->(Entity)` | `stake_percent` (economic holding), `voting_power_pct` (voting rights — kept separate from the stake; from BODS `votingRights` interests and DEF 14A proxies), `interest_types[]` (BODS interest kinds behind the edge: shareholding/votingRights/appointmentOfBoard/…), `direct_or_indirect` (from GLEIF RR-CDF: `direct` = directly-consolidated parent, `indirect` = ultimate parent), `ownership_type` (full/majority/minority/controlling/partnership/free_float), `since`, `until`, `source_id`, `source_url`, `source_date`. Free float / >100% conflicts aren't stored — the full-profile endpoint derives them from the disclosed stakes on read (`ownership` summary) |
| `(Person)-[:HAS_ROLE]->(Entity)` | `role`, `since`, `until`, `source_id`, `source_url`, `source_date` |
| `(Person)-[:RELATED_TO]->(Person)` | `relation`, `source_id` |
| `(Person)-[:NOT_DUPLICATE]->(Person)` | `at` — marks two people confirmed to be *different* (keep-separate) |
| `(Entity)-[:DUAL_LISTED_WITH]->(Entity)` | links share classes of a dual-listed company |
| `(Entity)-[:SUCCEEDED_BY]->(Entity)` | corporate succession/rename, directed predecessor → successor (e.g. Twitter → X Corp.); from Wikidata P1366/P1365 (with `since` from the P585 qualifier) and from GLEIF LEI-CDF (MERGED/DUPLICATE → `SuccessorLEI`, keyed `lei:{LEI}`). `since`, `source_id`, `source_url`, `source_date` |
| `(Entity)-[:HEADQUARTERED_IN\|REGISTERED_IN\|OPERATES_IN]->(Location)` | — |

`until = null` means the relationship is currently active.  
`ownership_type`: `full`, `majority`, `minority`, `controlling`, `passive`, `active`, `partnership`

Vertex/edge types and lookup indexes are created idempotently on startup and via
`python manage.py init-schema` (see the README's *Deployment → Schema & indexes*).

## GLEIF sourcing

GLEIF data comes from the **GLEIF golden copy** (current, daily), keyed `lei:{LEI}`:
`manage.py gleif-lei-cdf` imports the **entities** (name, country, legal address,
legal-form type) from LEI-CDF; `gleif-rr` imports the **relationships** (direct/
ultimate parents) from RR-CDF; `gleif-succession` imports mergers from LEI-CDF.
This replaces the OpenOwnership GLEIF BODS export (`bods-gleif`), which was frozen
at 2025‑03. **UK PSC** (natural-person beneficial ownership) has no golden-copy
equivalent and still uses the BODS importer (`bods-uk-psc`).
