# Deploying Owlgraph (production)

One box, Docker Compose, three containers: `caddy` (TLS, the web app) →
`api` (uvicorn) → `arcadedb`. Decided 2026-08-13; not Kubernetes, and not until
there is a second app node, an HA database or a team.

```bash
cp deploy/.env.example deploy/.env      # fill it in — see the comments
mkdir -p /srv/owlgraph/arcadedb /srv/owlgraph/www
tar -xzf owlgraph-web-1.0.0.tar.gz -C /srv/owlgraph/www   # the release's web tarball
cd deploy && docker compose up -d
docker compose ps                        # every service must say (healthy)
docker compose run --rm api python manage.py init-schema
```

## Health, as the box sees it

| Check | What it proves |
|---|---|
| `arcadedb` healthcheck: `GET /api/v1/ready` | the server accepts connections |
| `api` healthcheck: `GET /health` | the API can **serve**: it runs one query against the database and answers 503 when it cannot, with `database: unreachable` in the body. A process that is up but cannot reach the database is not healthy |
| `caddy` healthcheck: `GET /health` through the proxy | TLS termination and routing work |

`restart: unless-stopped` plus the healthchecks restart a service that stops
answering; `docker compose ps` shows the state. This is the self-healing layer.
It does **not** tell anyone when the whole box is down — that needs a check from
outside (see `docs/operations.md`, Monitoring).

Logs are capped (20 MB × 5 per container). ArcadeDB is not published to the
host; only the API reaches it.

## Versions

Nothing says `latest`. `APP_VERSION` names the release (the git tag on main),
the same version's web tarball goes into `WEB_DIR`, and `ARCADEDB_VERSION` is
checked against the upstream bug tracker before it changes. To release: change
`APP_VERSION`, unpack the matching tarball, `docker compose up -d`.
