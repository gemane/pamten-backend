# Ownership Platform

A platform for mapping and visualizing corporate ownership hierarchies worldwide.

## Tech Stack

- **Backend**: Python + FastAPI
- **Database**: Neo4j (AuraDB)
- **Deployment**: Render (backend) + Vercel (frontend, coming soon)

## Getting Started

### 1. Set up Neo4j AuraDB

1. Go to https://neo4j.io/cloud/aura-free
2. Create a free instance
3. Save your credentials (password is only shown once!)
4. Run this index in the AuraDB console:

```cypher
CREATE FULLTEXT INDEX namesIndex
FOR (n:Entity|Person)
ON EACH [n.name, n.full_name, n.description]
```

### 2. Configure environment

```bash
cd backend
cp .env.example .env
# Edit .env with your Neo4j credentials
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the API

```bash
uvicorn app.main:app --reload
```

API docs available at: http://localhost:8000/docs

### 5. Seed with example data

```bash
python seed.py
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /entities/ | Create entity |
| GET | /entities/ | List entities |
| GET | /entities/{id} | Get entity |
| PUT | /entities/{id} | Update entity |
| DELETE | /entities/{id} | Delete entity |
| POST | /persons/ | Create person |
| GET | /persons/{id} | Get person |
| POST | /locations/ | Create location |
| POST | /locations/{entity_id}/headquartered-in/{location_id} | Set HQ |
| POST | /locations/{entity_id}/operates-in/{location_id} | Set operations |
| POST | /relationships/owns | Create ownership |
| POST | /relationships/owns/close | Close ownership |
| POST | /relationships/roles | Add role |
| POST | /relationships/roles/close | Close role |
| GET | /relationships/ownership-tree/{id} | Get ownership tree |
| GET | /relationships/owners/{id} | Get owners |
| GET | /relationships/history/{id} | Get ownership history |
| GET | /search/ | Full text search |
| GET | /search/entity/{id}/full-profile | Full entity profile |
| GET | /search/geographic | Search by country |
| POST | /sources/ | Create source |
| GET | /sources/ | List sources |

## Deployment

### Render (Backend)

1. Push to GitHub
2. Connect repo on https://render.com
3. Render detects `render.yaml` automatically
4. Add environment variables in Render dashboard:
   - `NEO4J_URI`
   - `NEO4J_USERNAME`
   - `NEO4J_PASSWORD`

## Data Model

```
(Person) --[OWNS]-----------> (Entity)
(Person) --[HAS_ROLE]-------> (Entity)
(Person) --[RELATED_TO]-----> (Person)
(Person) --[RESIDES_IN]-----> (Location)

(Entity) --[OWNS]-----------> (Entity)
(Entity) --[HEADQUARTERED_IN]->(Location)
(Entity) --[REGISTERED_IN]--> (Location)
(Entity) --[OPERATES_IN]----> (Location)

(Source) <-- referenced by all relationships
```

## Roadmap

- [ ] Frontend (React + Cytoscape.js visualization)
- [ ] Web scraping pipeline
- [ ] Source credibility scoring
- [ ] Community verification
- [ ] Historical timeline view
- [ ] Geographic map view
- [ ] Industry/sector tags
- [ ] Financial snapshots
