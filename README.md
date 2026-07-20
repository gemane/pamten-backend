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

Nodes (`Entity`, `Person`, `Location`, `Source`, `MergeLog`, `Peer`, `ScrapeRun`,
`ScraperSource`, `User`) and their edges (`OWNS`, `HAS_ROLE`, `RELATED_TO`,
`NOT_DUPLICATE`, `DUAL_LISTED_WITH`, location edges) with all properties:
**[`docs/data-model.md`](docs/data-model.md)**.

---

## API

The full REST reference — Auth, Entities, Persons (incl. deduplication), Search,
Sources, Relationships, Scraper, Federation, and maintenance/advanced endpoints —
lives in **[`docs/api-reference.md`](docs/api-reference.md)**. An interactive
version is served at `/docs` (Swagger) and `/redoc` on a running instance.

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
| `require_moderator` | Role must be `admin` or `moderator` (data-moderation / verification queue) |
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

Different sources spell the same person differently (SEC's "Page Lawrence" vs
Wikidata's "Larry Page", nicknames, aliases), so scraping creates duplicate
`Person` nodes. `GET /persons/duplicates` scans for them (name/alias tokens,
birth date+place, surname+company); high-confidence groups are auto-merged after
each `run-all` scrape (`SCRAPER_AUTODEDUP_ENABLED`), the rest are resolved from
the web app's **Scraper tab → Review duplicate persons** panel (merge, keep
separate, or view the merge log).

📄 **Deep dive:** [`docs/deduplication.md`](docs/deduplication.md) — the scan signals, confidence model, the ArcadeDB param-mediated merge, keep-separate, and the merge log.

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

Independent instances, run by different people, share ownership data as
**trusted peers** — each *publishes* its graph and *pulls* from peers it trusts.
A pull is **one-way and opt-in**: pulled nodes are reconciled on external ids and
run through the [duplicate scan](#duplicate-persons), and every imported fact is
attributed to a `Peer: <name>` Source, so you can trust or drop a peer without
touching your own data. Exports are **Ed25519-signed**, and a pull verifies the
peer's signature (a mismatch is refused).

Disabled by default. Enable with `FEDERATION_ENABLED=true`, generate a signing
key via `python manage.py gen-federation-key` (set it as `FEDERATION_SIGNING_KEY`),
then register peers and pull from the web app's **Scraper tab → Federation**
panel or the `/federation/*` API.

📄 **Deep dive:** [`docs/federation.md`](docs/federation.md) — the snapshot format, Ed25519 signing/verification, external-id reconciliation, the trust/threat model, why it's a native format rather than BODS, and setup commands.

---

## Import verification (planned)

Almost every node and edge comes from a scraper, and scrapers are sometimes
wrong. Phase A lets anyone (logged in or not) **⚑ report** a node or edge that
looks wrong and gives **moderators** a **queue of what's disputed** — without a
manual-entry UI.
Corrections live as a re-scrape-surviving overlay (like the dedup keep-separate
log), not as in-place edits that the next scrape would clobber.

📄 **Design (not yet implemented):** [`docs/verification.md`](docs/verification.md) — the `Flag` node and stable edge addressing, anonymous rate-limited reporting, the endpoints, the read-time "disputed" badge, GDPR intake, and the Phase-B non-goals (suppress/pin).

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
| `wipe-data` | Delete all imported data (keeps user accounts + schema); rebuilds indexes. Requires `ALLOW_DESTRUCTIVE_WIPE=true` **and** `--confirm-database <name>` matching the connected DB, e.g. `ALLOW_DESTRUCTIVE_WIPE=true python manage.py wipe-data --confirm-database pamten` |
| `geocode` | Backfill HQ/location coordinates via Nominatim |
| `normalize-countries` | Convert country values to canonical ISO-2 codes |
| `gen-federation-key` | Generate an Ed25519 signing keypair for [federation](#federation) |
| `bods-gleif` / `bods-uk-psc` | Import a local BODS file. Add `--bulk-load` on a full import to drop secondary indexes for the load and rebuild them after (much faster; collapse any duplicate edges afterwards with `POST /scraper/deduplicate-edges`) |
| `backfill-search` | Populate the FULL_TEXT `search_text` column powering `/search`. Run once after a bulk import (the BODS importer sets it inline, but this covers pre-existing rows). |

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
