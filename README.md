# Pamten Backend

![CI](https://github.com/gemane/pamten-backend/actions/workflows/ci.yml/badge.svg)
[![Licence: MIT](https://img.shields.io/badge/Code-MIT-yellow.svg)](LICENSE)
[![Data Licence: ODbL](https://img.shields.io/badge/Data-ODbL-brightgreen.svg)](DATA_LICENSE.md)

FastAPI backend for the Pamten ownership mapping platform. Stores corporate ownership hierarchies in an ArcadeDB graph database and exposes a REST API consumed by the frontend.

**Live API:** https://pamten-backend-yrbh.onrender.com  
**Docs (Swagger):** https://pamten-backend-yrbh.onrender.com/docs  
**Frontend:** https://pamten-frontend.onrender.com

---

## Branch protection

To require CI to pass before any merge into `main`:

1. Go to **Settings → Branches → Add rule**
2. Branch name pattern: `main`
3. Enable: **Require status checks to pass before merging**
4. Select the **test** job as required
5. Enable: **Require branches to be up to date before merging**

---

## Tech stack

| Layer | Library |
|---|---|
| Framework | FastAPI 0.111 |
| Database | ArcadeDB (graph, Cypher-compatible) |
| Auth | PyJWT + passlib/bcrypt |
| HTTP client | httpx |
| Config | pydantic-settings |
| Server | Uvicorn |
| Hosting | Render (web service) |

---

## Getting started

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload   # http://localhost:8000
```

Create a `.env` file with your credentials:

```env
ARCADEDB_URL=http://<your-instance>:2480
ARCADEDB_USERNAME=root
ARCADEDB_PASSWORD=<password>
ARCADEDB_DATABASE=pamten
SECRET_KEY=<long-random-string>
SCRAPER_ENABLED=false
SCRAPER_SEC_EDGAR_ENABLED=false
```

---

## Project structure

```
backend/
└── app/
    ├── main.py              # FastAPI app, CORS, router registration
    ├── config.py            # Settings loaded from environment variables
    ├── database.py          # ArcadeDB HTTP client + Neo4j-compatible shim
    ├── models/              # Pydantic request/response models
    │   ├── entity.py
    │   ├── person.py
    │   ├── location.py
    │   ├── relationship.py
    │   └── source.py
    ├── routers/             # REST endpoints
    │   ├── entities.py
    │   ├── persons.py
    │   ├── locations.py
    │   ├── relationships.py
    │   ├── search.py
    │   └── sources.py
    ├── auth/                # JWT authentication
    │   ├── router.py        # /auth/register, /auth/login, /auth/me
    │   ├── security.py      # Password hashing, token create/decode
    │   └── dependencies.py  # FastAPI Depends: get_current_user, require_admin, etc.
    └── scraper/
        ├── router.py        # All /scraper/* endpoints
        ├── sources.py       # Per-source toggle switches
        ├── runner.py        # Orchestration: search → fetch → write to DB
        ├── wikidata.py      # Wikidata SPARQL client
        ├── sec_edgar.py     # SEC EDGAR scraper (ownership filings + executives)
        ├── open_corporates.py  # OpenCorporates client (requires API key)
        └── mapper.py        # Entity type inference, name normalisation
```

---

## Data model

### Nodes

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

### Relationships

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

---

## API reference

### Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create account (first → admin, rest → viewer) |
| POST | `/auth/login` | — | Returns JWT access token |
| GET | `/auth/me` | bearer | Current user info |

### Entities
| Method | Path | Description |
|---|---|---|
| GET | `/entities/` | List entities |
| GET | `/entities/by-country` | Entities grouped by ISO country code |
| GET | `/entities/{id}` | Single entity |
| POST | `/entities/` | Create entity |
| PUT | `/entities/{id}` | Update entity |
| DELETE | `/entities/{id}` | Delete entity |

### Persons
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/persons/{id}` | — | Single person |
| POST | `/persons/` | contributor | Create person |
| GET | `/persons/duplicates` | contributor | Suggest likely-duplicate people (see [Duplicate persons](#duplicate-persons)) |
| POST | `/persons/deduplicate` | contributor | Auto-merge high-confidence duplicates (`apply=false` = dry run) |
| POST | `/persons/merge` | contributor | Fold a duplicate person into the one to keep |
| POST | `/persons/keep-separate` | contributor | Mark a group as confirmed-different (stops being suggested) |
| DELETE | `/persons/keep-separate` | contributor | Undo a keep-separate |
| GET | `/persons/kept-separate` | contributor | List confirmed-distinct pairs |
| GET | `/persons/merge-log` | contributor | History of merges (the "already merged" list) |

### Search
| Method | Path | Description |
|---|---|---|
| GET | `/search/?q=` | Full-text search across entities and persons |
| GET | `/search/entity/{id}/full-profile` | Entity with owners, subsidiaries, executives, HQ |
| GET | `/search/person/{id}/full-profile` | Person with positions, holdings, place of birth |
| GET | `/search/geographic` | Entities grouped by country for map view |

### Sources (provenance)
| Method | Path | Description |
|---|---|---|
| GET | `/sources/entity/{id}` | Sources behind an entity's facts (from its edges + node) |
| GET | `/sources/person/{id}` | Sources behind a person's roles/ownership |

### Relationships
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

### Scraper
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/scraper/status` | — | Master + per-source flag states (incl. `autodedup_enabled`) |
| GET | `/scraper/runs` | contributor | Recent scrape run log — status, counts, failures (see [Scrape run log](#scrape-run-log)) |
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

### Federation
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/federation/status` | contributor | Whether federation is on, plus this instance's publish counts |
| GET | `/federation/export` | contributor | This instance's ownership snapshot (signed if a key is set) |
| GET | `/federation/public-key` | contributor | This instance's signing public key + `key_id` |
| GET | `/federation/peers` | contributor | List trusted peers (tokens/keys never returned) |
| POST | `/federation/peers` | admin | Register a trusted peer |
| DELETE | `/federation/peers/{id}` | admin | Remove a trusted peer |
| POST | `/federation/peers/{id}/pull` | admin | Pull a peer's snapshot, verify, import, reconcile |

### Maintenance / advanced
One-off migrations and lower-level tools, mostly for operators. The person-merge
endpoints under [Persons](#persons) supersede the legacy scraper ones below.

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/scraper/proxy-statement/run` | contributor | Parse a company's latest DEF 14A proxy and return per-person voting power (read-only) |
| POST | `/scraper/proxy-statement/write` | contributor | Fetch the latest DEF 14A and write `voting_power_pct` onto OWNS edges (`entity_id` overrides name lookup) |
| POST | `/scraper/deduplicate-edges` | admin | Collapse duplicate active OWNS edges, keeping the most informative |
| POST | `/scraper/deduplicate-persons` | admin | Legacy: merge reversed-name Person duplicates (use `/persons/deduplicate`) |
| POST | `/scraper/migrate-ownership-types` | admin | One-time migration deriving canonical `ownership_type` values |
| POST | `/relationships/dual-listed` | contributor | Link two share classes of a dual-listed company (`DUAL_LISTED_WITH`) |
| POST | `/locations/{entity_id}/headquartered-in/{location_id}` | contributor | Attach an HQ location |
| POST | `/locations/{entity_id}/registered-in/{location_id}` | contributor | Attach a registration location |
| POST | `/locations/{entity_id}/operates-in/{location_id}` | contributor | Attach an operating location |

---

## Authentication

JWTs are signed with `SECRET_KEY` (HS256, 7-day expiry). Set a strong random key in production:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

The first account registered automatically receives the `admin` role. Protected routes use FastAPI `Depends`:

| Dependency | Requirement |
|---|---|
| `get_current_user` | Any valid JWT |
| `require_admin` | Role must be `admin` |
| `require_contributor` | Role must be `admin` or `contributor` |

---

## Scrapers

Wikidata, SEC EDGAR, and OpenCorporates run per-company via `/scraper/run-all`;
BODS (GLEIF / UK PSC) is a separate bulk dataset import. Each source has an
independent on/off toggle (`/scraper/sources`).

🧩 **Adding a source:** [`docs/scraper-plugin-guide.md`](docs/scraper-plugin-guide.md) is a step-by-step guide (API module → source toggle → config flags → runner → endpoints → dedup) with a pre-deploy checklist.

### Wikidata
Imports corporate ownership data via SPARQL. Fetches subsidiaries, parent organisations, and CEOs recursively up to `depth` levels (max 3). Controlled by `SCRAPER_ENABLED`.

- Searches Wikidata by company name, picks the best-matching entity
- Writes to the DB using upsert — safe to re-run, no duplicates
- Caps at 15 subsidiaries and 3 CEOs per entity
- 400 ms delay between requests (Wikidata rate limit)

### SEC EDGAR
Imports investor data from SC 13D/13G ownership filings and executive data from Form 3/4 XML. Controlled by `SCRAPER_SEC_EDGAR_ENABLED`.

Company lookup uses a three-vector strategy to avoid false matches:

1. **`company_tickers.json`** — instant lookup for all US-listed companies
2. **`browse-edgar` name index** — EDGAR's registered company-name search; returns 0 results for companies not on EDGAR (Nestlé, Samsung, Volkswagen, etc.), preventing false positives from full-text matches
3. **EFTS full-text search** — last resort, guarded by a name-similarity check (SequenceMatcher ratio ≥ 0.55 after stripping legal suffixes)

Investor names are classified as Person or Entity using heuristics that recognise common legal suffixes including European forms (S.A.R.L., GmbH, S.A., N.V., AG, etc.).

📄 **Deep dive:** [`docs/sec_edgar_scraper.md`](docs/sec_edgar_scraper.md) — research and implementation notes (which EDGAR APIs, CIK resolution, 13D/13G & Form 3/4 parsing, per-company request budgets).

### OpenCorporates
Requires a paid API key (`OPENCORPORATES_API_KEY`). Disabled by default.

### BODS (GLEIF & UK PSC)
Beneficial-ownership data imported via the **Beneficial Ownership Data Standard**:
**GLEIF** (Global LEI, corporate ownership worldwide, CC0) and the **UK PSC**
register (people with significant control, CC0). Controlled by
`SCRAPER_BODS_GLEIF_ENABLED` / `SCRAPER_BODS_UK_PSC_ENABLED`.

Unlike the per-company scrapers above, BODS is a **bulk dataset import**, not a
name lookup — so it is *not* part of `run-all` and has its own endpoints
(`/scraper/bods/*`) and, in the web app, its own **Bulk import** card. It streams
a BODS file (URL or a local file inside `BODS_DATA_DIR`), reconciles endpoints by
LEI / Companies House id, and can be filtered by `jurisdiction` and `limit`. Both
sources still appear in `/scraper/sources` with independent on/off toggles.

---

## Duplicate persons

Different sources spell the same person differently — SEC's last-first "Page
Lawrence" vs Wikidata's "Larry Page", nicknames (Rob/Robert), and legal-name
aliases — so every scrape can create duplicate `Person` nodes. `GET
/persons/duplicates` scans for them using three signals:

- **name/alias token set** — order/case/honorific-insensitive, matched across a
  person's full name *and* every Wikidata alias (so SEC's "Gates William H Iii"
  links to "Bill Gates" via its "William H. Gates III" alias);
- **same birth date + place**;
- **same surname + a shared company + a compatible given name** — catches
  nickname/legal-name variants, gated by the shared company so relatives (e.g.
  Elon/Kimbal Musk) aren't flagged.

Groups are ranked `high` / `medium` / `low`; conflicting birth dates flag a group
as `likely_distinct`.

- **Auto-merge** — `POST /persons/deduplicate` (and, after every `run-all`
  scrape, gated by `SCRAPER_AUTODEDUP_ENABLED`) merges only high-confidence,
  non-distinct groups; the rest are left for review.
- **Merge** — `POST /persons/merge` re-homes a duplicate's edges (with their
  provenance) onto the kept person, folds its name in as an alias, and records a
  `MergeLog` entry (`GET /persons/merge-log`).
- **Keep separate** — `POST /persons/keep-separate` records a `NOT_DUPLICATE`
  edge so a confirmed-different pair (e.g. Keith vs Rupert Murdoch) stops being
  suggested; reversible via `DELETE`, listed via `GET /persons/kept-separate`.

Admins can drive all of this from the web app's **Scraper tab → Review duplicate
persons** panel.

---

## Scrape run log

Every scrape (`/scraper/run`, `/run-all`, `/sec-edgar/run`,
`/open-corporates/run`) records a `ScrapeRun` row: a `running` entry on start,
updated to `ok` (with node count) or `failed` (with the error) on finish. `GET
/scraper/runs` lists them newest-first, so the UI and other sessions can see
what's scraping now and which runs failed — across the panel *and* the bundled
`scrape_companies.sh` script.

The log is **bounded**: capped at 500 records, with the oldest pruned on every
write, so it can never grow the database unbounded. A `running` row older than 30
minutes is flagged `stale` (an interrupted run). Surfaced in the web app's
**Scraper tab → Recent activity** panel, which polls while a run is in progress.

---

## Federation

Federation lets independent instances — run by different people, on different
servers — share ownership data as **trusted peers**. Each instance can *publish*
its graph and *pull* from peers it trusts. A pull is **one-way and opt-in**:
nothing is pushed to you and nothing syncs automatically.

Pulled data is reconciled, not blindly copied. Nodes are matched on their
external ids (Wikidata QID, SEC CIK, LEI, Companies House) and then run through
the [duplicate scan](#duplicate-persons), so a peer's "Larry Fink" folds into yours instead
of duplicating it. Every imported fact is attributed to a `Peer: <name>` Source
carrying that peer's credibility, so you can always tell what came from where —
and downgrade or drop a peer without touching your own data.

Disabled by default. Turn it on with `FEDERATION_ENABLED=true`.

### Signing (verifiable provenance)

So a pulled contribution is provably the peer's — not fabricated by whoever sent
the bytes — exports are signed with **Ed25519**. Generate a keypair:

```bash
python3 manage.py gen-federation-key
```

Set the printed private seed as the `FEDERATION_SIGNING_KEY` env var (keep it
secret — env only, never commit it) and redeploy. Your instance now signs every
`/federation/export`, and `/federation/public-key` publishes the matching public
key and its `key_id` fingerprint for peers to register.

### Adding a trusted peer

Register a peer with its base URL, an optional bearer token for its export
endpoint, and its public key:

```bash
curl -X POST "$API/federation/peers" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Partner Org","base_url":"https://partner.example.com",
       "auth_token":"<their export token>","public_key":"<their base64 key>",
       "credibility_score":70}'
```

Then pull:

```bash
curl -X POST "$API/federation/peers/{peer_id}/pull" -H "Authorization: Bearer $TOKEN"
```

The pull fetches the peer's signed snapshot and, if you registered their public
key, **verifies the signature — a mismatch is refused (422)** and the import and
its Source are stamped `verified: true`. A peer with no key on file still
imports, but is marked unverified. Admins can also drive all of this from the
web app's **Scraper tab → Federation** panel.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ARCADEDB_URL` | required | ArcadeDB HTTP endpoint |
| `ARCADEDB_USERNAME` | required | Database username |
| `ARCADEDB_PASSWORD` | required | Database password |
| `ARCADEDB_DATABASE` | `pamten` | Database name |
| `SECRET_KEY` | insecure default | JWT signing key — **must be overridden when `DEBUG=false`, or the app refuses to start** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` (7 days) | Token lifetime |
| `CORS_ORIGINS` | `` (none) | Comma-separated list of allowed frontend origins |
| `SCRAPER_ENABLED` | `false` | Master scraper switch (required for any scrape) |
| `SCRAPER_WIKIDATA_ENABLED` | `true` | Wikidata source switch |
| `SCRAPER_SEC_EDGAR_ENABLED` | `false` | SEC EDGAR source switch |
| `SCRAPER_OPENCORPORATES_ENABLED` | `false` | OpenCorporates source switch |
| `SCRAPER_BODS_GLEIF_ENABLED` | `false` | GLEIF BODS import switch |
| `SCRAPER_BODS_UK_PSC_ENABLED` | `false` | UK PSC BODS import switch |
| `SCRAPER_AUTODEDUP_ENABLED` | `true` | Auto-merge high-confidence duplicate persons after each `run-all` scrape |
| `FEDERATION_ENABLED` | `false` | Enable trusted-peer federation (publish/pull) |
| `FEDERATION_SIGNING_KEY` | — | Ed25519 private seed (base64) for signing exports; generate with `manage.py gen-federation-key`. Secret — env only |
| `OPENCORPORATES_API_KEY` | — | OpenCorporates API token (optional) |
| `BODS_DATA_DIR` | `/data` | Only .zip/.json files inside this directory may be passed as `local_file` to BODS imports |
| `GEOCODING_ENABLED` | `false` | Geocode addresses to coordinates via Nominatim |
| `GEOCODING_CONTACT` | — | Contact email added to the Nominatim User-Agent (required by their usage policy) |
| `GEOCODING_USER_AGENT` | `pamten-ownership-platform` | Base User-Agent for Nominatim requests |
| `NOMINATIM_URL` | public endpoint | Nominatim search URL (override to self-host) |
| `GEOCODING_MIN_INTERVAL` | `1.0` | Minimum seconds between geocoding requests |
| `DEBUG` | `false` | FastAPI debug mode |

---

## Deployment

Deployed on Render as a web service. Any push to `main` triggers an automatic redeploy. Required environment variables must be set in the Render dashboard: `ARCADEDB_URL`, `ARCADEDB_USERNAME`, `ARCADEDB_PASSWORD`, `SECRET_KEY`, `CORS_ORIGINS`.

### Schema & indexes

Lookup indexes (on `id`, `name`, `name_normalized`, `wikidata_id`, `sec_cik`, and a unique `User.email`) are created automatically on startup — the app runs an idempotent, best-effort bootstrap that is a no-op once they exist. To (re)create them explicitly, e.g. against a fresh database:

```bash
python3 manage.py init-schema
```

### CLI (`manage.py`)

| Command | Description |
|---|---|
| `init-schema` | Create vertex/edge types and lookup indexes (idempotent) |
| `seed` | Seed the built-in company list |
| `wipe-data` | Delete all imported data (keeps user accounts + schema); rebuilds indexes. Guarded behind `DEBUG=true` |
| `geocode` | Backfill HQ/location coordinates via Nominatim |
| `normalize-countries` | Convert country values to canonical ISO-2 codes |
| `gen-federation-key` | Generate an Ed25519 signing keypair for [federation](#federation) |
| `bods-gleif` / `bods-uk-psc` | Import a local BODS file |

---

## Licence

### Source Code
The Pamten source code is licensed under the
[MIT Licence](LICENSE).

### Database
The Pamten ownership database is licensed under the
[Open Database Licence (ODbL) v1.0](DATA_LICENSE.md).

You are free to copy, distribute and use the data,
as long as you attribute Pamten and share any adapted
databases under ODbL. See [DATA_LICENSE.md](DATA_LICENSE.md)
for full details.

This dual licence model follows the same approach as
[OpenStreetMap](https://www.openstreetmap.org/copyright).

---

## Built With

This project was designed and built with the assistance of
[Claude](https://claude.ai) by Anthropic, using
[Claude Code](https://claude.ai/code) CLI for development.
