# API reference

Full REST surface. Auth is JWT bearer (see the README's *Authentication*);
`contributor` = admin or contributor role. An interactive version is served at
`/docs` (Swagger) and `/redoc` on a running instance.

## Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create account (first → admin, rest → viewer) |
| POST | `/auth/login` | — | Returns JWT access token |
| GET | `/auth/me` | bearer | Current user info |

## Entities
| Method | Path | Description |
|---|---|---|
| GET | `/entities/` | List entities |
| GET | `/entities/by-country` | Entities grouped by ISO country code |
| GET | `/entities/{id}` | Single entity |
| POST | `/entities/` | Create entity |
| PUT | `/entities/{id}` | Update entity |
| DELETE | `/entities/{id}` | Delete entity |

## Persons
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/persons/{id}` | — | Single person |
| POST | `/persons/` | contributor | Create person |
| GET | `/persons/duplicates` | contributor | Suggest likely-duplicate people (see [Duplicate persons](../README.md#duplicate-persons)) |
| POST | `/persons/deduplicate` | contributor | Auto-merge high-confidence duplicates (`apply=false` = dry run) |
| POST | `/persons/merge` | contributor | Fold a duplicate person into the one to keep |
| POST | `/persons/keep-separate` | contributor | Mark a group as confirmed-different (stops being suggested) |
| DELETE | `/persons/keep-separate` | contributor | Undo a keep-separate |
| GET | `/persons/kept-separate` | contributor | List confirmed-distinct pairs |
| GET | `/persons/merge-log` | contributor | History of merges (the "already merged" list) |

## Search
| Method | Path | Description |
|---|---|---|
| GET | `/search/?q=` | Full-text search across entities and persons |
| GET | `/search/entity/{id}/full-profile` | Entity with owners, subsidiaries, executives, HQ |
| GET | `/search/person/{id}/full-profile` | Person with positions, holdings, place of birth |
| GET | `/search/geographic` | Entities grouped by country for map view |

## Sources (provenance)
| Method | Path | Description |
|---|---|---|
| GET | `/sources/entity/{id}` | Sources behind an entity's facts (from its edges + node) |
| GET | `/sources/person/{id}` | Sources behind a person's roles/ownership |

## Relationships
| Method | Path | Description |
|---|---|---|
| POST | `/relationships/owns` | Create OWNS edge |
| POST | `/relationships/owns/close` | Set `until` date (end ownership) |
| POST | `/relationships/roles` | Create HAS_ROLE edge |
| POST | `/relationships/roles/close` | End a role |
| POST | `/relationships/related-to` | Create RELATED_TO edge between persons |
| GET | `/relationships/ownership-tree/{id}` | Recursive ownership tree (depth param, max 10) |
| GET | `/relationships/owners/{id}` | Current active owners of an entity |
| GET | `/relationships/history/{id}` | Full history: ownership in/out + executive roles |

## Scraper
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/scraper/status` | — | Master + per-source flag states (incl. `autodedup_enabled`) |
| GET | `/scraper/runs` | contributor | Recent scrape run log — status, counts, failures (see [Scrape run log](../README.md#scrape-run-log)) |
| POST | `/scraper/run` | admin | Run a Wikidata scrape by company name |
| POST | `/scraper/sec-edgar/run` | admin | Run an SEC EDGAR scrape by company name |
| POST | `/scraper/open-corporates/run` | admin | Run an OpenCorporates scrape by company name |
| POST | `/scraper/run-all` | admin | Run all enabled scrapers for a company (then auto-dedup) |
| POST | `/scraper/geocode` | contributor | Backfill HQ coordinates via Nominatim (needs `GEOCODING_ENABLED`) |
| POST | `/scraper/bods/gleif/run` | contributor | Import GLEIF beneficial-ownership data (BODS) |
| POST | `/scraper/bods/uk-psc/run` | contributor | Import UK PSC beneficial-ownership data (BODS) |
| POST | `/scraper/bods/run-all` | contributor | Run both BODS imports |
| GET | `/scraper/sources` | — | Per-source toggle states |
| PATCH | `/scraper/sources/{name}/toggle` | admin | Flip a source on/off |
| DELETE | `/scraper/company` | admin | Delete a company and all its related nodes |

## Federation
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/federation/status` | contributor | Whether federation is on, plus this instance's publish counts |
| GET | `/federation/export` | contributor | This instance's ownership snapshot (signed if a key is set) |
| GET | `/federation/public-key` | contributor | This instance's signing public key + `key_id` |
| GET | `/federation/peers` | contributor | List trusted peers (tokens/keys never returned) |
| POST | `/federation/peers` | admin | Register a trusted peer |
| DELETE | `/federation/peers/{id}` | admin | Remove a trusted peer |
| POST | `/federation/peers/{id}/pull` | admin | Pull a peer's snapshot, verify, import, reconcile |

## Maintenance / advanced
One-off migrations and lower-level tools, mostly for operators. The person-merge
endpoints under [Persons](#persons) supersede the legacy scraper ones below.

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/scraper/proxy-statement/run` | contributor | Parse a company's latest DEF 14A proxy and return per-person voting power (read-only) |
| POST | `/scraper/proxy-statement/write` | contributor | Fetch the latest DEF 14A and write `voting_power_pct` onto OWNS edges (`entity_id` overrides name lookup) |
| POST | `/scraper/deduplicate-edges` | admin | Collapse duplicate active OWNS edges, keeping the most informative |
| POST | `/scraper/deduplicate-entities` | admin | Merge Entity duplicates sharing an LEI / Companies House number, migrating their edges. Batched via `?limit=` (default 300); returns `remaining` — call until 0 |
| POST | `/scraper/deduplicate-persons` | admin | Legacy: merge reversed-name Person duplicates (use `/persons/deduplicate`) |
| POST | `/scraper/migrate-ownership-types` | admin | One-time migration deriving canonical `ownership_type` values |
| POST | `/relationships/dual-listed` | contributor | Link two share classes of a dual-listed company (`DUAL_LISTED_WITH`) |
| POST | `/locations/{entity_id}/headquartered-in/{location_id}` | contributor | Attach an HQ location |
| POST | `/locations/{entity_id}/registered-in/{location_id}` | contributor | Attach a registration location |
| POST | `/locations/{entity_id}/operates-in/{location_id}` | contributor | Attach an operating location |
