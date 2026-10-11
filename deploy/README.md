# Deploying Owlgraph (production)

One box, Docker Compose, three containers: `caddy` (TLS, the web app) →
`api` (uvicorn) → `arcadedb`. Decided 2026-08-13; not Kubernetes, and not until
there is a second app node, an HA database or a team.

**One script, one env file.** On a fresh Debian/Ubuntu server (16 GB, the
plan's Hetzner CPX42), as root:

```bash
cp deploy/.env.example owlgraph.env      # on your machine: fill it in (comments explain each key)
scp deploy/setup-server.sh owlgraph.env root@SERVER:
ssh root@SERVER bash setup-server.sh owlgraph.env
```

`setup-server.sh` does, in order and safe to re-run: preflight (root, OS, the env
file complete and not the example, RAM for the heap, disk for the import, DNS of
`DOMAIN` pointing at this box) → Docker from its own repo + unattended security
upgrades → a 4 GB swapfile and ufw (22/80/443) → the release's deploy files from
the tag `vAPP_VERSION` into `/opt/owlgraph` → `deploy/.env` and `host.env` (0600;
host-only keys — backups, the first import, GHCR token, check URLs — never reach
a container) → the release's web tarball (checksum verified, and checked to be
built for `DOMAIN`) → `docker compose pull/up` and waiting until every service is
**healthy** → `init-schema` → cron (backup 02:00 before the GLEIF delta 03:00,
analytics 04:30, weekly report Mon 06:00, UTC) → the first import (`IMPORT=full|
test|none`) as the systemd unit `owlgraph-import`, so it survives the SSH session
(`tail -f /var/log/owlgraph/import.log`).

**Prerequisites it cannot do:** a DNS record for `DOMAIN`; a published release of
**both** repos (tag `vX.Y.Z` on main → the release workflow builds the API image
and the web tarball), the frontend built with `PROD_API_URL=https://DOMAIN` and
the `LEGAL_*` variables; and an ArcadeDB release of `ARCADEDB_VERSION` (26.10.1 or
later — 26.7.3 returns incomplete index range reads on big indexes; 26.10.1 was
published on 2026-10-05 and verified on 2026-10-10, see `docs/operations.md`,
*Upgrading ArcadeDB*).

Day two is `owlgraph <command>` (`/opt/owlgraph/deploy/ops.sh`): `status`,
`backup`, `import full|test`, `refresh-golden-copies`, `refresh-psc`,
`refresh-company-data`, `gleif-update`, `gleif-history` (once, after the first full
import), `upgrade X.Y.Z`, `manage <manage.py args>`
— the production twin of the dev box's `~/scripts`, with every `manage.py` call
inside the `api` container and the importers' files in `DATA_DIR` (mounted at
`/data`). `owlgraph help` lists them.

Tested end to end on 2026-10-05 against a local stack built from this
directory (the API image from the repo, ArcadeDB 26.7.3, `ops.sh import test`):
it found the two bugs fixed then — ArcadeDB's home was mounted over (the server
never started) and Caddy's healthcheck could never pass. The provisioning steps
(apt, ufw, systemd) need root and were not run there.

## Health, as the box sees it

| Check | What it proves |
|---|---|
| `arcadedb` healthcheck: `GET /api/v1/ready` | the server accepts connections |
| `api` healthcheck: `GET /health` | the API can **serve**: it runs one query against the database and answers 503 when it cannot, with `database: unreachable` in the body. A process that is up but cannot reach the database is not healthy |
| `caddy` healthcheck: `GET /health` on Caddy's internal, unpublished `:8080` | Caddy runs and routes to the API. TLS is checked from **outside** — `setup-server.sh` fetches `https://DOMAIN/health`, and so does the uptime check |

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
