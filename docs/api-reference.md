# API reference

Full REST surface. Auth is JWT bearer (see the README's *Authentication*);
`contributor` = admin or contributor role. An interactive version is served at
`/docs` (Swagger) and `/redoc` on a running instance.

## Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create account (first → admin, rest → viewer). Non-admins are created unverified and emailed a link; response has `verification_required:true` and **no token** |
| POST | `/auth/login` | — | Returns a JWT access token. `403 {code:"email_not_verified"}` when the email isn't verified yet |
| POST | `/auth/verify-email` | — | Confirm an email from the emailed token `{token}` |
| POST | `/auth/resend-verification` | — | Re-send the verification link `{email}` (rate-limited; always `200`) |
| POST | `/auth/forgot-password` | — | Email a password-reset link `{email}` (always `200` — no user enumeration) |
| POST | `/auth/reset-password` | — | Set a new password from the emailed token `{token, new_password}` (link is single-use) |
| GET | `/auth/me` | bearer | Current user info |
| GET | `/auth/mfa/status` | bearer | Whether TOTP two-factor is enabled |
| POST | `/auth/mfa/setup` | bearer | Begin TOTP enrolment → `{secret, otpauth_uri}` for a QR |
| POST | `/auth/mfa/enable` | bearer | Confirm enrolment `{code}` → `{enabled, recovery_codes[]}` (shown once) |
| POST | `/auth/mfa/disable` | bearer | Turn MFA off `{code}` (TOTP or recovery code required) |
| POST | `/auth/mfa/verify` | — | Exchange the login `mfa_token` + `{code}` (TOTP or recovery) for an access token |

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
| GET | `/search/entity/{id}/full-profile` | Entity with owners (self-loops excluded), subsidiaries, executives, HQ, dual-listed pairs, succession (`succeeded_by` / `replaces`), `cross_holdings` (reciprocal/circular owners), and an `ownership` summary — `free_float_pct` (computed residual = 100 − disclosed, when every owner's stake is known) + `exceeds_100` flag |
| GET | `/scraper/ownership-quality` | admin | Data-quality report: `self_loops` count (A owns A) + `cross_holdings` pairs (A↔B) |
| GET | `/search/person/{id}/full-profile` | Person with positions, holdings, place of birth |
| GET | `/search/geographic` | Entities grouped by country for map view |

## Stats
| Method | Path | Description |
|---|---|---|
| GET | `/stats` | **Public.** Data-scale counts for the landing page: `{companies, people, relationships, sources}`. Read from ArcadeDB `schema:types` metadata (O(1), no scan), cached ~60s, best-effort (returns zeros rather than erroring) |

## Sources (provenance)
| Method | Path | Description |
|---|---|---|
| GET | `/sources/entity/{id}` | Sources behind an entity's facts (from its edges + node) |
| GET | `/sources/person/{id}` | Sources behind a person's roles/ownership |

## Verification flags
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/flags` | public (rate-limited) | Report a node/edge as wrong. Anonymous **or** logged-in; anon capped at 2/hour per IP fingerprint, users higher. Repeat of the same target+category is collapsed |
| GET | `/flags` | moderator | Moderation queue, newest first; filter `?status=`, `?target_kind=`, `?category=`. `?group=true` collapses to one row per target+category (`count` + `flag_ids`) |
| GET | `/flags/summary` | public | Open-flag count for one target (`?node_id=` or `?from_id=&to_id=[&role=]`) — powers the "disputed" badge |
| PATCH | `/flags/{id}` | moderator | Triage status: `open` ⇄ `reviewing`, `→ rejected` |
| DELETE | `/flags/{id}` | moderator | Remove a flag entirely (spam/test/duplicate); any Suppression/Pin it made is left untouched |
| POST | `/flags/{id}/suppress` | moderator | Resolve a flag by **suppressing** its target — an *edge* flag deletes the edge + records a `Suppression`; a *node* flag (entity/person) is a pure read-time hide (search, own profile, related-node lists). Survives re-scrapes; flag → `resolved` |
| GET | `/flags/suppressions` | moderator | Active suppression overrides |
| DELETE | `/flags/suppressions/{id}` | moderator | Un-suppress (edge reappears if a re-scrape recreates it) |
| POST | `/flags/{id}/pin` | moderator | Resolve an OWNS flag by **pinning** a corrected `stake_percent`/`ownership_type` — a read-time override that survives re-scrapes (edge not mutated); flag → `resolved` |
| GET | `/flags/pins` | moderator | Active pin overrides |
| DELETE | `/flags/pins/{id}` | moderator | Un-pin (reads fall back to the scraped value) |

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
| POST | `/scraper/ensure` | **verified** | On-demand enrichment for any authenticated + email-verified user. Body `{query, depth=1, force=false}`. Ensures a company is present + fresh, scraping the enabled **instant** sources (Wikidata, SEC EDGAR, OpenCorporates — never bulk/GLEIF) only when it's absent, never on-demand-scraped, stale (> `SCRAPER_ONDEMAND_TTL_DAYS`, default 30), forced, or a deeper pass is asked for. Returns `{scraped, reason, entity_id, depth_reached, sources_run, profile}`. Degrades to a DB-only response when `SCRAPER_ENABLED` is off |
| POST | `/scraper/run` | admin | Run a Wikidata scrape by company name. `?depth=` (0–3, default 2) = how many levels **down the subsidiary tree** to recursively expand — each level fetches up to 15 subsidiaries per node, so it grows ~exponentially (depth 3 ≈ up to 15³ nodes and thousands of Wikidata calls). Owners/executives are recorded but not recursed |
| POST | `/scraper/sec-edgar/run` | admin | Run an SEC EDGAR scrape by company name |
| POST | `/scraper/open-corporates/run` | admin | Run an OpenCorporates scrape by company name |
| POST | `/scraper/run-all` | admin | Run all enabled scrapers for a company (then auto-dedup). `?depth=` passes through to the Wikidata scrape (see `/scraper/run`) |
| POST | `/scraper/geocode` | contributor | Backfill HQ coordinates via Nominatim (needs `GEOCODING_ENABLED`) |
| GET | `/scraper/bods/status` | — | Enabled state of the bulk GLEIF / UK datasets (imported from the CLI, not HTTP) |
| GET | `/scraper/sources` | — | Per-source toggle states + `kind` (`instant` = query-driven, on-demand; `bulk` = scheduled dataset import). On-demand `/scraper/ensure` runs only enabled `instant` sources |
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
| GET | `/scraper/duplicate-edges/count` | admin | Count duplicate active OWNS edges (read-only): `{active_edges, distinct_pairs, duplicate_pairs, redundant_edges}` |
| POST | `/scraper/deduplicate-edges` | admin | Collapse duplicate active OWNS edges, keeping the largest stake (by @rid, provenance-preserving) |
| GET | `/scraper/duplicate-entities/name-count` | admin | Count same-name entity duplicate groups — the same company under different identifiers (e.g. two GLEIF LEIs) the id-based dedup can't see. Also reported in the BODS import result as `duplicate_names` |
| GET | `/scraper/duplicate-entities/name-candidates` | admin | The biggest same-name duplicate groups, each tagged with a `confidence` they're the same company — **definitive** (shared wikidata_id/sec_cik/companies_house_id), **high** (same registered address), **medium** (same country+founded), **low** (name only) — with members (id/country/lei/address) for review. `?limit=`, `?min_confidence=` |
| POST | `/scraper/deduplicate-entities` | admin | Collapse Entity duplicates sharing an LEI / Companies House number (heals the recordId-keyed BODS doubling). Background by default (returns `started`; poll `GET /scraper/runs`). `strategy=bulk` (default) keeps one node per id and deletes the rest (fast; drops losers' edges); `strategy=merge` migrates edges first (only finishes on small data). `background=false` runs the sync bounded-batch merge (`?limit=`, returns `remaining`) |
| POST | `/scraper/deduplicate-persons` | admin | Legacy: merge reversed-name Person duplicates (use `/persons/deduplicate`) |
| POST | `/scraper/migrate-ownership-types` | admin | One-time migration deriving canonical `ownership_type` values |
| POST | `/relationships/dual-listed` | contributor | Link two share classes of a dual-listed company (`DUAL_LISTED_WITH`) |
| POST | `/locations/{entity_id}/headquartered-in/{location_id}` | contributor | Attach an HQ location |
| POST | `/locations/{entity_id}/registered-in/{location_id}` | contributor | Attach a registration location |
| POST | `/locations/{entity_id}/operates-in/{location_id}` | contributor | Attach an operating location |
