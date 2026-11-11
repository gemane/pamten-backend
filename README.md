# Owlgraph Backend

![CI](https://github.com/gemane/pamten-backend/actions/workflows/ci.yml/badge.svg)
[![Licence: MIT](https://img.shields.io/badge/Code-MIT-yellow.svg)](LICENSE)
[![Data Licence: ODbL](https://img.shields.io/badge/Data-ODbL-brightgreen.svg)](DATA_LICENSE.md)

FastAPI backend for the Owlgraph ownership mapping platform. Stores corporate ownership hierarchies in an ArcadeDB graph database and exposes a REST API consumed by the frontend.

**Live API (dev):** https://api-dev.owlgraph.org  
**Docs (Swagger):** https://api-dev.owlgraph.org/docs  
**Frontend (dev):** https://dev.owlgraph.org

The `*.onrender.com` URLs still serve the same deployments, but the owlgraph.org
domains are canonical and are the only origins in `CORS_ORIGINS` — reaching the
frontend on its onrender URL loads the page but every API call fails CORS.

---

## Branch model

Two long-lived branches:

| Branch | Deploys to | Purpose |
|---|---|---|
| `develop` | Render (dev) — auto-deploy on push | Integration branch. Everything lands here first and runs against the dev database. |
| `main` | nothing yet (production, once it exists) | Only ever contains code that has been verified running on the dev deploy. |

The flow is: **feature branch → PR into `develop` → verify on the dev deploy → fast-forward `main` to `develop`**. `develop` is the repository's **default branch**, so a new PR targets it automatically.

Promotion is a fast-forward, never a merge or squash, so `main` is always literally a commit that already exists on `develop` — the two histories can't drift:

```bash
git checkout main && git pull
git merge --ff-only origin/develop
git push origin main
```

If `--ff-only` refuses, `main` has picked up a commit `develop` doesn't have (a merge commit from a promotion done the old way, say). Fix the ancestry once by merging `main` into `develop` — don't force-push `main`.

Branch protection, CI checks and how security updates arrive: [`docs/development.md`](docs/development.md).

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
python3.13 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt     # the app + test/lint tools, exact versions
uvicorn app.main:app --reload   # http://localhost:8000
```
Python 3.13 everywhere; dependencies are locked with pip-tools and every locked package's
licence is checked on each test run — how to upgrade: [`docs/development.md`](docs/development.md).

Create a `.env` file with your credentials:

```env
ARCADEDB_URL=http://<your-instance>:2480
ARCADEDB_USERNAME=root
ARCADEDB_PASSWORD=<password>
ARCADEDB_DATABASE=owlgraph
SECRET_KEY=<long-random-string>
SCRAPER_ENABLED=false
SCRAPER_SEC_EDGAR_ENABLED=false
```

Every other setting: [`docs/configuration.md`](docs/configuration.md).

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

## Documentation

| Topic | Where |
|---|---|
| Nodes, edges and their properties; the rules the database enforces | [`docs/data-model.md`](docs/data-model.md) |
| REST API (also live at `/docs` and `/redoc`; everything under `/v1`, `/` and `/health` unversioned) | [`docs/api-reference.md`](docs/api-reference.md) |
| Accounts, tokens, roles | [`docs/authentication.md`](docs/authentication.md) |
| Data sources — Wikidata, SEC EDGAR, OpenCorporates, GLEIF, UK Companies House | [`docs/sources.md`](docs/sources.md), deep dive [`docs/sec_edgar_scraper.md`](docs/sec_edgar_scraper.md) |
| Adding a source | [`docs/scraper-plugin-guide.md`](docs/scraper-plugin-guide.md), [`docs/new-source-checklist.md`](docs/new-source-checklist.md) |
| Duplicates: merging entities, merged ids, duplicate persons | [`docs/deduplication.md`](docs/deduplication.md) |
| Running it: backups, monitoring, scrape run log, usage measurement, deployment, schema, the `manage.py` CLI | [`docs/operations.md`](docs/operations.md) |
| Environment variables | [`docs/configuration.md`](docs/configuration.md) |
| Federation between instances | [`docs/federation.md`](docs/federation.md) |
| Reporting wrong data (flags, moderators) | [`docs/verification.md`](docs/verification.md) |

---

## Deployment

The dev environment runs on Render and redeploys on every push to `develop`
(`api-dev.owlgraph.org`, `dev.owlgraph.org`); `main` deploys nowhere yet — production is
planned on its own host. Domains, environment changes and the schema bootstrap:
[`docs/operations.md`](docs/operations.md).

---

## Licence

### Source Code
The Owlgraph source code is licensed under the
[MIT Licence](LICENSE).

### Database
The Owlgraph ownership database is licensed under the
[Open Database Licence (ODbL) v1.0](DATA_LICENSE.md).

You are free to copy, distribute and use the data,
as long as you attribute Owlgraph and share any adapted
databases under ODbL. See [DATA_LICENSE.md](DATA_LICENSE.md)
for full details.

This dual licence model follows the same approach as
[OpenStreetMap](https://www.openstreetmap.org/copyright).

### Bundled reference data
Code lists shipped under `backend/app/scraper/data/`, each under its publisher's
open terms: GLEIF's ELF and Registration Authority lists (CC0); Eurostat's NACE
Rev. 2 codelist with its EN/DE labels (free re-use with attribution, CC BY 4.0);
the SEC's SIC code list and the US Census SIC/NAICS/ISIC concordances (US
government works, public domain); Companies House's SIC 2007 list (Open
Government Licence v3); the UN Statistics Division's ISIC Rev. 4 – NACE Rev. 2
correspondence. `scripts/build_industry_bundle.py` records where each came from.

---

## Built With

This project was designed and built with the assistance of
[Claude](https://claude.ai) by Anthropic, using
[Claude Code](https://claude.ai/code) CLI for development.
