# Pamten Backend

FastAPI backend for the Pamten ownership mapping platform. Stores corporate ownership hierarchies in a Neo4j graph database and exposes a REST API consumed by the frontend.

**Live API:** https://pamten-backend-yrbh.onrender.com  
**Docs (Swagger):** https://pamten-backend-yrbh.onrender.com/docs  
**Frontend:** https://pamten-frontend.onrender.com

---

## Tech stack

| Layer | Library |
|---|---|
| Framework | FastAPI 0.111 |
| Database | Neo4j AuraDB (graph) |
| Auth | PyJWT + passlib/bcrypt |
| HTTP client | httpx (Wikidata SPARQL) |
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
NEO4J_URI=neo4j+s://<your-instance>.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<password>
NEO4J_DATABASE=neo4j
SECRET_KEY=<long-random-string>
SCRAPER_ENABLED=false
```

Run this once in the Neo4j console to enable full-text search:

```cypher
CREATE FULLTEXT INDEX namesIndex
FOR (n:Entity|Person)
ON EACH [n.name, n.full_name, n.description]
```

---

## Project structure

```
backend/
└── app/
    ├── main.py              # FastAPI app, CORS, router registration
    ├── config.py            # Settings loaded from environment variables
    ├── database.py          # Neo4j driver + session helper
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
    └── scraper/             # Wikidata scraper
        ├── router.py        # /scraper/status, /scraper/run
        ├── sources.py       # /scraper/sources — per-source toggle switches
        ├── runner.py        # Orchestration: search → fetch → write to Neo4j
        ├── wikidata.py      # Wikidata SPARQL client
        └── mapper.py        # Maps Wikidata instance types → Pamten entity types
```

---

## Data model

### Nodes

| Label | Key properties |
|---|---|
| `Entity` | `id`, `name`, `type` (company/brand/holding), `country`, `founded`, `revenue`, `wikidata_id` |
| `Person` | `id`, `full_name`, `first_name`, `last_name`, `nationality`, `wikidata_id` |
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
`ownership_type`: `full`, `majority`, `minority`, `controlling`, `partnership`

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

### Relationships
| Method | Path | Description |
|---|---|---|
| POST | `/relationships/owns` | Create OWNS edge |
| POST | `/relationships/owns/close` | Set `until` date (end ownership) |
| POST | `/relationships/roles` | Create HAS_ROLE edge |
| POST | `/relationships/roles/close` | End a role |
| GET | `/relationships/ownership-tree/{id}` | Recursive ownership tree (depth param, max 10) |
| GET | `/relationships/owners/{id}` | Current active owners of an entity |
| GET | `/relationships/history/{id}` | Full history: ownership in/out + executive roles |

### Scraper
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/scraper/status` | — | Whether master `SCRAPER_ENABLED` flag is on |
| POST | `/scraper/run` | admin | Run a Wikidata scrape by company name |
| GET | `/scraper/sources` | — | Per-source toggle states |
| PATCH | `/scraper/sources/{name}/toggle` | admin | Flip a source on/off |

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

## Scraper

The Wikidata scraper imports corporate ownership data via SPARQL. It is controlled by two independent switches:

1. **`SCRAPER_ENABLED`** env var — master on/off, set in Render
2. **Per-source toggles** — stored as `ScraperSource` nodes in Neo4j, toggled via the API by admins

Both must be on for a scrape to run. Behaviour:
- Searches Wikidata for the company name and picks the top result
- Fetches subsidiaries, parent organisations, and CEOs recursively up to `depth` levels (max 3)
- Writes to Neo4j using `MERGE` — safe to re-run, no duplicates
- Caps at 15 subsidiaries and 3 CEOs per entity
- Adds a 400 ms delay between Wikidata requests

To add a new scraper source, add an entry to `KNOWN_SOURCES` in `scraper/sources.py`. It will automatically appear as a toggle in the frontend.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NEO4J_URI` | required | Neo4j AuraDB connection URI |
| `NEO4J_USERNAME` | required | Database username |
| `NEO4J_PASSWORD` | required | Database password |
| `NEO4J_DATABASE` | `neo4j` | Database name |
| `SECRET_KEY` | insecure default | JWT signing key — **must be overridden in production** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` (7 days) | Token lifetime |
| `SCRAPER_ENABLED` | `false` | Master scraper switch |
| `DEBUG` | `false` | FastAPI debug mode |

---

## Deployment

Deployed on Render as a web service. Render detects `render.yaml` automatically. Required environment variables must be set in the Render dashboard: `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `SECRET_KEY`. Any push to `main` triggers a redeploy.

---

## Built With

This project was designed and built with the assistance of
[Claude](https://claude.ai) by Anthropic, using
[Claude Code](https://claude.ai/code) CLI for development.
