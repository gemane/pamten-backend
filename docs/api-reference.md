# API reference

Full REST surface. Auth is JWT bearer (see the README's *Authentication*);
`contributor` = admin or contributor role. An interactive version is served at
`/docs` (Swagger) and `/redoc` on a running instance.

> **Base path: `/v1`.** Paths below are written without the prefix for brevity — the real URL for `/auth/login` is `/v1/auth/login`. The unversioned paths still work but are deprecated and hidden from the schema; use `/v1` in anything new. `/` and `/health` are unversioned by design.
>
> **Merged ids keep working.** When a duplicate is merged away its id is not dropped: a by-id read that misses falls back to a `MergedId` forwarding row and returns the surviving node, whose own `id` is the canonical one. Applies to `/entities/{id}`, `/persons/{id}` and both `full-profile` endpoints. A live id is never redirected, and an id that was never merged still 404s.

## Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create account (first → admin, rest → viewer). Non-admins are created unverified and emailed a link; response has `verification_required:true` and **no token** |
| POST | `/auth/login` | — | Returns a short-lived JWT access token (`{access_token, expires_in, …}`) and sets the httpOnly refresh cookie. `403 {code:"email_not_verified"}` when the email isn't verified yet |
| POST | `/auth/refresh` | cookie | Trade the refresh cookie for a new access token, rotating the cookie. `401` (and the cookie is cleared) if it is missing, expired, revoked, or replayed |
| POST | `/auth/logout` | cookie | Revoke this session and clear the cookie. Idempotent — `200` even with no session |
| POST | `/auth/verify-email` | — | Confirm an email from the emailed token `{token}` |
| POST | `/auth/resend-verification` | — | Re-send the verification link `{email}` (rate-limited; always `200`) |
| POST | `/auth/forgot-password` | — | Email a password-reset link `{email}` (always `200` — no user enumeration) |
| POST | `/auth/reset-password` | — | Set a new password from the emailed token `{token, new_password}` (link is single-use) |
| POST | `/auth/change-password` | bearer | Change your own password `{current_password, new_password}` — the self-service route that needs no email. Revokes other sessions and re-issues the caller's. `400` on a wrong current password, a policy violation, or reusing the current password |
| DELETE | `/auth/me` | bearer | **Permanently delete your own account** `{password}`. Re-authenticates with the password. Removes the User node (password hash, TOTP secret, recovery codes) and the account's rate-limit counters; flags the user filed are anonymised, not deleted. `400` for a wrong password, for the `ADMIN_EMAIL` bootstrap account, or when you are the last admin |
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
| POST | `/entities/keep-separate` | **contributor** — mark entities as confirmed DIFFERENT companies `{ids}`; excluded from auto-merge, checked per pair |
| DELETE | `/entities/keep-separate` | **contributor** — undo a keep-separate `{ids}` |
| GET | `/entities/kept-separate` | **contributor** — pairs confirmed to be different companies |
| GET | `/entities/merge-log` | **contributor** — recent entity merges, most recent first |

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
| GET | `/search/?q=` (`limit` 1-50, default 20) | Full-text search across entities and persons (FULL_TEXT `search_text` index, whole-word `CONTAINSTEXT`). If the index returns nothing, falls back to a bounded substring name scan so a degraded/incomplete FULL_TEXT index can't hide companies that are in the DB (`SEARCH_SUBSTRING_FALLBACK`, default on) |
| GET | `/search/entity/{id}/full-profile` | Entity with owners (self-loops excluded), subsidiaries, executives, HQ, dual-listed pairs, succession (`succeeded_by` / `replaces`), `cross_holdings` (reciprocal/circular owners), and an `ownership` summary — `free_float_pct` (computed residual = 100 − disclosed, when every owner's stake is known) + `exceeds_100` flag (`limit` per section, default 200, max 1000) |
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

## Usage measurement

Aggregate counters — what people search for, what they fail to find, which features get used.
No user id, session, IP or per-event timestamp is stored, so no row can be attributed to a
person; see `app/analytics.py` for the design and `pamten-legal` Activity 3 for the record.
Off unless `ANALYTICS_ENABLED` is set.

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/analytics/event` | public (rate-limited) | Record one **settled** search (`{kind:"search", query, country?, outcome:"selected"\|"zero"\|"abandoned", rank?}`) or one allow-listed interaction (`{kind:"usage", event}`). Never a keystroke: the search box queries every 300 ms while typing, and counting requests would record prefixes. Always `204` — measurement may not break, slow or reveal anything about what it measures. Unknown event names are dropped silently; 240 events/hour per IP fingerprint (hashed in memory, never stored) |
| GET | `/analytics/status` | — | Whether measurement is on. Exists because `/analytics/event` answers `204` whether it recorded anything or not, which makes "switched off" and "broken" indistinguishable from outside |
| GET | `/analytics/searches` | admin | What was searched for, most-searched first, with `zero_results` — a ranked list of demand the graph cannot answer. Paged (`skip`, `X-Total-Count`) |
| GET | `/analytics/usage` | admin | Feature counters and clicked result positions |
| GET | `/analytics/endpoints` | admin | Request counts per route template, status class and latency band |

## Verification flags
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/flags` | public (rate-limited) | Report a node/edge as wrong. Anonymous **or** logged-in; anon capped at 2/hour per IP fingerprint, users higher. Repeat of the same target+category is collapsed |
| GET | `/flags` | moderator | Moderation queue, newest first; filter `?status=`, `?target_kind=`, `?category=`, `?related_to=` (one node **and** every relationship at either end of it). `?group=true` collapses to one row per target+category (`count` + `flag_ids`); grouping is not paged. Ungrouped: `?skip=`/`?limit=` page it and the total for the same filters comes back in the `X-Total-Count` header |
| GET | `/flags/summary` | public | Open-flag count for one target (`?node_id=` or `?from_id=&to_id=[&role=]`), or for a node and everything reported about it (`?related_to=`) — powers the "disputed" badge |
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
| POST | `/relationships/owns` | Create OWNS edge; when `source_id` is set, also records the matching `Claim` |
| POST | `/relationships/owns/close` | Set `until` date (end ownership) |
| POST | `/relationships/roles` | Create HAS_ROLE edge |
| POST | `/relationships/roles/close` | End a role |
| POST | `/relationships/related-to` | Create RELATED_TO edge between persons |
| GET | `/relationships/ownership-tree/{id}` | Recursive ownership tree (`depth` max 10; `limit` paths, default 500, max 5000). `include_indirect=true` by default — most ultimate-parent edges duplicate a path the tree already contains, but some are the only link to a company, so excluding them by kind loses entities. Redundancy is decided by `POST /scraper/mark-shortcuts`, which stamps `shortcut` on the edge; stamped edges are excluded from the tree and from `/relationships/owners`, same rule as the graph views |
| GET | `/relationships/owners/{id}` | Current active owners of an entity (`limit`, default 200, max 1000) |
| GET | `/relationships/history/{id}` | Full history: ownership in/out + executive roles (`limit` **per category**, default 500, max 2000) |

> These three walk the graph and are bounded so a hub node can't return tens of thousands of rows. When a cap is hit the response carries **`X-Result-Truncated: true`** — the array length alone can't tell you, since suppressed rows are filtered out after the limit is applied. The header is in the CORS `expose_headers` list, so browser clients can read it.

## Scraper
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/scraper/status` | — | Master + per-source flag states (incl. `autodedup_enabled`) |
| GET | `/scraper/runs` | — | Recent scrape run log — source, target, timings, counts, status (see [Scrape run log](../README.md#scrape-run-log)). Public, but the `error` field is **omitted** unless the caller is a contributor or admin, since exception text can carry internal URLs or credentials |
| GET | `/scraper/sources` | — | The public source catalogue: every source the platform draws on, with `label`, `url`, `description`, `kind` (instant/bulk), `credibility` (0-100) and `quality` band. `enabled` is the run toggle — it does **not** mean the source's data is absent |
| POST | `/scraper/ensure` | **verified** | On-demand enrichment for any authenticated + email-verified user. Body `{query, depth=1, force=false, country=null}`. Ensures a company is present + fresh, scraping the enabled **instant** sources (Wikidata, SEC EDGAR, OpenCorporates — never bulk/GLEIF) only when it's absent, never on-demand-scraped, stale (> `SCRAPER_ONDEMAND_TTL_DAYS`, default 30), forced, or a deeper pass is asked for. A **cooldown** (`SCRAPER_ONDEMAND_COOLDOWN_HOURS`, default 24) caps even `force`: a company scraped within the last N hours can't be re-scraped again (`reason: "cooldown"`, served from the DB), so the sources aren't hammered — a deeper pass is still allowed. A search that finds **nothing** is remembered for the same window, keyed by normalised name + country, and repeating it returns `reason: "recently_missed"` with `missed_at` and no source calls: "Alphabet" in France has no answer, and asking twice does not produce one. A later successful scrape of the same name+country clears it. `country` (ISO-2, from the search box) narrows the whole operation: the DB lookup that decides freshness resolves within that country, and the sources search **inside** it where their APIs allow (Wikidata `haswbstatement:P17`, OpenCorporates `jurisdiction_code`) or have their single match checked where they don't (SEC EDGAR, whose search-side filter matches the filing address rather than the incorporation). So "Alphabet" with Germany selected finds Alphabet Fuhrparkmanagement, not Alphabet Inc. A match that states no country is rejected — asked for Germany, "unknown" is not an answer. Returns `{scraped, reason, kind, entity_id, person_id, depth_reached, sources_run, profile}`. **`kind`** is `entity` or `person`: a name can be either, and searching a person's name used to write them into the graph as a company. When the graph has no company by that name, the person path runs — Wikidata's reverse links (P169 CEO, P112 founder, P488 chairperson, P3320 board member, P127 owner) become `HAS_ROLE` and `OWNS` edges, and `profile` is the person profile. Link targets are filtered: "founded by" also covers buildings, software, schools and, for Elon Musk, a car and an aeroplane. Degrades to a DB-only response when `SCRAPER_ENABLED` is off |
| POST | `/scraper/run` | admin | Run a Wikidata scrape by company name. `?depth=` (0–3, default 2) = how many levels **down the subsidiary tree** to recursively expand — each level fetches up to 15 subsidiaries per node, so it grows ~exponentially (depth 3 ≈ up to 15³ nodes and thousands of Wikidata calls). Owners/executives are recorded but not recursed |
| POST | `/scraper/sec-edgar/run` | admin | Run an SEC EDGAR scrape by company name |
| POST | `/scraper/sec-13f/run` | contributor | Institutional holders of one issuer from Form 13F (~100 EDGAR fetches — filed by the holders, read one filing per manager). Quarterly by deadline: a re-run before the next 13F due date (quarter end + 45 days) returns `status: fresh` and fetches nothing; `?force=true` overrides. 409 until the SEC EDGAR scrape has stamped the entity's CIK |
| POST | `/scraper/open-corporates/run` | admin | Run an OpenCorporates scrape by company name |
| POST | `/scraper/run-all` | admin | Run all enabled scrapers for a company (then auto-dedup). `?depth=` passes through to the Wikidata scrape (see `/scraper/run`) |
| POST | `/scraper/geocode` | contributor | Backfill HQ coordinates via Nominatim (needs `GEOCODING_ENABLED`) |
| GET | `/scraper/bods/status` | — | Enabled state of the bulk GLEIF / UK datasets (imported from the CLI, not HTTP) |
| GET | `/scraper/sources` | — | Per-source toggle states + `kind` (`instant` = query-driven, on-demand; `bulk` = scheduled dataset import). On-demand `/scraper/ensure` runs only enabled `instant` sources |
| PATCH | `/scraper/sources/{name}/toggle` | admin | Flip a source on/off |
| DELETE | `/scraper/company` | admin | Delete a company and all its related nodes |

## App

### `GET /app-version`

Whether a released client may keep running. **Unauthenticated** — a client too old
to authenticate still has to learn it must upgrade — and safe to call on every app
start.

```
GET /app-version?platform=ios&version=1.2.3
```

```json
{
  "platform": "ios",
  "min_supported": "1.2.0",
  "latest": "1.4.0",
  "update_required": false,
  "update_available": true,
  "store_url": "https://apps.apple.com/...",
  "message": null
}
```

The **server** compares the versions, not the client: the clients that most need
correcting are the ones running whatever comparison bug shipped with them, and
`"1.10.0"` sorts before `"1.9.0"` as a string.

It **fails open**. No policy, an unknown platform, an unparseable version or a
database error all answer `update_required: false`. A bug that locked every user
out would arrive on devices that cannot be reached.

Clients should call the **unversioned** `/app-version`. It is served under `/v1`
too, but a version check that a version bump can move is not a check.

### `PUT /app-version` — admin

Replaces the policy for all platforms at once. A full replace rather than a merge,
so a partial update cannot raise the iOS minimum while leaving Android pointing at
last year's store URL. Stored in the database, so locking out a broken client does
not need a deploy.

```json
{ "ios": { "min_supported": "1.2.0", "latest": "1.4.0", "store_url": "https://..." } }
```

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
| POST | `/scraper/mark-shortcuts` | admin | Flag GLEIF ultimate-parent OWNS edges that duplicate a path already in the graph, so the renderer can omit them without losing companies whose only link is a shortcut. `limit` bounds parents processed. **Re-run after every import** |
| GET | `/scraper/duplicate-entities/name-count` | admin | Count same-name entity duplicate groups — the same company under different identifiers (e.g. two GLEIF LEIs) the id-based dedup can't see. Also reported in the BODS import result as `duplicate_names` |
| GET | `/scraper/duplicate-entities/name-candidates` | admin | The biggest same-name duplicate groups, each tagged with a `confidence` they're the same company — **definitive** (shared wikidata_id/sec_cik/companies_house_id), **high** (same registered address), **medium** (same country+founded), **low** (name only) — with members (id/country/lei/address) for review. `?limit=`, `?min_confidence=` |
| POST | `/scraper/deduplicate-entities` | admin | Collapse Entity duplicates sharing an LEI / Companies House number (heals the recordId-keyed BODS doubling). Background by default (returns `started`; poll `GET /scraper/runs`). `strategy=bulk` (default) keeps one node per id and deletes the rest (fast; drops losers' edges); `strategy=merge` migrates edges first (only finishes on small data). `background=false` runs the sync bounded-batch merge (`?limit=`, returns `remaining`) |
| POST | `/scraper/deduplicate-persons` | admin | Legacy: merge reversed-name Person duplicates (use `/persons/deduplicate`) |
| POST | `/scraper/migrate-ownership-types` | admin | One-time migration deriving canonical `ownership_type` values |
| POST | `/relationships/dual-listed` | contributor | Link two share classes of a dual-listed company (`DUAL_LISTED_WITH`) |

### Counting companies by country

`GET /entities/by-country?basis=jurisdiction|hq|subdivision` returns `[{country, count}]`.

**`basis`** chooses what "country" means, and it changes the answer where it matters —
BARCLAYS CAPITAL (CAYMAN) LIMITED is `KY` by jurisdiction and `GB` by headquarters:

| basis | property | means |
|---|---|---|
| `jurisdiction` (default) | `country` | where the company is registered |
| `hq` | `hq_country` | where it is actually run |
| `subdivision` | `jurisdiction_code` | registered, one level finer: `US-DE` |

There is **no fallback between them.** A company with no recorded headquarters is not shown
under its registration country in `hq` mode — that would present a guess as a fact and erase the
distinction the parameter exists to draw.

`subdivision` keys the groups by ISO 3166-2 (`US-DE`, `CA-ON`), never by country, so a caller
narrows to one country by prefix rather than mapping the world by it: only ~1% of records state a
subdivision and only six countries use them at all, which makes the null group most of the graph.
"United States, no subdivision stated" is the country total minus its `US-` rows — absent means
*not stated*, never *none*.

Instead, companies with no country for the chosen basis come back as a single group with
**`country: null`**, so the counts still add up to the whole graph. A tenth of it has no country
at all. `GET /entities/without-country?basis=…` lists them.

`GET /entities/by-country/{country}?basis=…` takes the same parameter. An unrecognised `basis`
is a **422**, not a silent fallback: a typo should not render the wrong map with nothing to say
so.

