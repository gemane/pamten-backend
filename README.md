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
| `Entity` | `id`, `name`, `type` (company/brand/holding), `country`, `founded`, `revenue`, `wikidata_id`, `sec_cik` |
| `Person` | `id`, `full_name`, `first_name`, `last_name`, `nationality`, `wikidata_id`, `wikipedia_url` |
| `Location` | `id`, `city`, `country`, `coordinates` |
| `Source` | `id`, `name`, `url`, `credibility_score` |
| `User` | `id`, `email`, `password_hash`, `role` (admin/contributor/viewer) |
| `ScraperSource` | `name`, `enabled`, `description` |

### Relationships

| Pattern | Properties |
|---|---|
| `(Entity\|Person)-[:OWNS]->(Entity)` | `stake_percent`, `ownership_type`, `since`, `until`, `source_id` |
| `(Person)-[:HAS_ROLE]->(Entity)` | `role`, `since`, `until`, `source_id` |
| `(Person)-[:RELATED_TO]->(Person)` | `relation` |
| `(Entity)-[:HEADQUARTERED_IN]->(Location)` | — |
| `(Entity)-[:REGISTERED_IN]->(Location)` | — |
| `(Entity)-[:OPERATES_IN]->(Location)` | — |

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
| Method | Path | Description |
|---|---|---|
| GET | `/persons/{id}` | Single person |
| POST | `/persons/` | Create person |

### Search
| Method | Path | Description |
|---|---|---|
| GET | `/search/?q=` | Full-text search across entities and persons |
| GET | `/search/entity/{id}/full-profile` | Entity with owners, subsidiaries, executives, HQ |
| GET | `/search/geographic` | Entities grouped by country for map view |

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
| GET | `/scraper/status` | — | Whether master `SCRAPER_ENABLED` flag is on |
| POST | `/scraper/run` | admin | Run a Wikidata scrape by company name |
| GET | `/scraper/sec-edgar/status` | — | Whether `SCRAPER_SEC_EDGAR_ENABLED` flag is on |
| POST | `/scraper/sec-edgar/run` | admin | Run an SEC EDGAR scrape by company name |
| POST | `/scraper/run-all` | admin | Run all enabled scrapers for a company name |
| GET | `/scraper/open-corporates/status` | — | Whether OpenCorporates is configured |
| POST | `/scraper/open-corporates/run` | admin | Run an OpenCorporates scrape by company name |
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

Three data sources are supported, all triggered via `/scraper/run-all`:

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

### OpenCorporates
Requires a paid API key (`OPENCORPORATES_API_KEY`). Disabled by default.

---

## Federation

Federation lets independent instances — run by different people, on different
servers — share ownership data as **trusted peers**. Each instance can *publish*
its graph and *pull* from peers it trusts. A pull is **one-way and opt-in**:
nothing is pushed to you and nothing syncs automatically.

Pulled data is reconciled, not blindly copied. Nodes are matched on their
external ids (Wikidata QID, SEC CIK, LEI, Companies House) and then run through
the [duplicate scan](#persons), so a peer's "Larry Fink" folds into yours instead
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
| `SCRAPER_ENABLED` | `false` | Master Wikidata scraper switch |
| `SCRAPER_SEC_EDGAR_ENABLED` | `false` | SEC EDGAR scraper switch |
| `SCRAPER_AUTODEDUP_ENABLED` | `true` | Auto-merge high-confidence duplicate persons after each `run-all` scrape |
| `FEDERATION_ENABLED` | `false` | Enable trusted-peer federation (publish/pull) |
| `FEDERATION_SIGNING_KEY` | — | Ed25519 private seed (base64) for signing exports; generate with `manage.py gen-federation-key`. Secret — env only |
| `OPENCORPORATES_API_KEY` | — | OpenCorporates API token (optional) |
| `BODS_DATA_DIR` | `/data` | Only .zip/.json files inside this directory may be passed as `local_file` to BODS imports |
| `GEOCODING_ENABLED` | `false` | Geocode addresses to coordinates via Nominatim |
| `GEOCODING_CONTACT` | — | Contact email added to the Nominatim User-Agent (required by their usage policy) |
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
