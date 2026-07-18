# Data model

Pamten stores an ownership graph in ArcadeDB. Nodes are vertices; ownership,
roles, and locations are edges.

## Nodes

| Label | Key properties |
|---|---|
| `Entity` | `id`, `name`, `name_normalized`, `type` (company/brand/holding), `country`, `countries`, `founded`, `revenue`, `wikidata_id`, `sec_cik`, `lei_id`, `companies_house_id`, `hq_lat`/`hq_lng`/`hq_city`/`hq_country`, `source_id` |
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
| `(Entity\|Person)-[:OWNS]->(Entity)` | `stake_percent`, `voting_power_pct`, `ownership_type`, `since`, `until`, `source_id`, `source_url`, `source_date` |
| `(Person)-[:HAS_ROLE]->(Entity)` | `role`, `since`, `until`, `source_id`, `source_url`, `source_date` |
| `(Person)-[:RELATED_TO]->(Person)` | `relation`, `source_id` |
| `(Person)-[:NOT_DUPLICATE]->(Person)` | `at` — marks two people confirmed to be *different* (keep-separate) |
| `(Entity)-[:DUAL_LISTED_WITH]->(Entity)` | links share classes of a dual-listed company |
| `(Entity)-[:HEADQUARTERED_IN\|REGISTERED_IN\|OPERATES_IN]->(Location)` | — |

`until = null` means the relationship is currently active.  
`ownership_type`: `full`, `majority`, `minority`, `controlling`, `passive`, `active`, `partnership`

Vertex/edge types and lookup indexes are created idempotently on startup and via
`python manage.py init-schema` (see the README's *Deployment → Schema & indexes*).
