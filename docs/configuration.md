# Configuration

Every environment variable the backend reads (`app/config.py`). A local `.env` needs only the database, `SECRET_KEY` and the scraper switches — see the [README](../README.md).

| Variable | Default | Description |
|---|---|---|
| `ARCADEDB_URL` | required | ArcadeDB HTTP endpoint |
| `ARCADEDB_USERNAME` | required | Database username |
| `ARCADEDB_PASSWORD` | required | Database password |
| `ARCADEDB_DATABASE` | `owlgraph` | Database name |
| `SECRET_KEY` | insecure default | JWT signing key — **min 32 chars, always enforced; must also be overridden when `DEBUG=false`, or the app refuses to start** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access-token lifetime. Not revocable, so this is the exposure window for a stolen one |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | Absolute session lifetime. Rotation does not extend it |
| `REFRESH_COOKIE_NAME` | `owlgraph_refresh` | Name of the httpOnly session cookie |
| `REFRESH_COOKIE_DOMAIN` | `` (host-only) | Leave empty unless a host other than the API must receive the cookie |
| `REFRESH_COOKIE_SECURE` | `true` | HTTPS-only cookie. Set `false` only for a plain-http local backend |
| `CORS_ORIGINS` | `` (none) | Comma-separated list of allowed frontend origins |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | none | Provision this account as admin on startup (created if missing). When set, self-registration never grants admin — avoids the "first person to `/register` becomes admin" race on a fresh DB |
| `REQUIRE_EMAIL_VERIFICATION` | `true` | Block login until the account's email is verified |
| `EMAIL_BACKEND` | `` (auto) | `scaleway` \| `resend` \| `smtp` \| `console`; empty = auto (console unless `SMTP_HOST` set) |
| `RESEND_API_KEY` | — | Resend API key (for `EMAIL_BACKEND=resend`). Secret — env only |
| `SCALEWAY_SECRET_KEY` | — | Scaleway API key **Secret Key** (for `EMAIL_BACKEND=scaleway`). Secret — env only |
| `SCALEWAY_PROJECT_ID` | — | Scaleway project holding the verified TEM domain (for `EMAIL_BACKEND=scaleway`) |
| `SCALEWAY_TEM_REGION` | `fr-par` | Scaleway TEM region: `fr-par` \| `nl-ams` \| `pl-waw` |
| `SMTP_HOST` / `SMTP_PORT` | `` / `587` | SMTP server (e.g. `smtp.gmail.com`) — note Render blocks outbound SMTP |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | none | SMTP credentials — for Gmail, the account + an **App Password**. Secret — env only |
| `EMAIL_FROM` | = `SMTP_USERNAME` | `From` header on outgoing mail |
| `APP_BASE_URL` | `http://localhost:5173` | Frontend origin used to build verification / reset links in emails |
| `ANALYTICS_ENABLED` | `false` | Record aggregate usage counters — what is searched for, which features are used, endpoint health. No user, session, IP or per-event timestamp is stored; see *Usage measurement* below. Live on dev since 2026-08-04 |
| `SCRAPER_ENABLED` | `false` | Master scraper switch (required for any scrape) |
| `SCRAPER_WIKIDATA_ENABLED` | `true` | Wikidata source switch |
| `SCRAPER_WIKIDATA_DATA_MODE` | `claims_only` | The data mode a fresh database seeds the Wikidata source with, and the mode it runs in until its catalogue row says otherwise: `claims_only` records claims and enriches entities but never draws ownership structure, `full` draws edges like every other source. Survives a drop-and-rebuild, which the Scraper-tab toggle does not (see docs/deduplication.md, *Claims-only sources*) |
| `SCRAPER_SEC_EDGAR_ENABLED` | `false` | SEC EDGAR source switch |
| `REPORT_EMAIL` | *(empty)* | Recipient of the weekly activity digest (`manage.py weekly-report --email`); falls back to `ADMIN_EMAIL` |
| `SOURCE_CALLS_ENABLED` | `true` | Count the calls to each outside source (EDGAR, GLEIF, Wikidata, …) and the shared bucket's hits/misses per UTC day in `SourceCall` — `manage.py source-calls`, the weekly digest, the rebuild's step 8. No user in it (docs/operations.md, *Source calls*) |
| `ALERT_HEALTH_URL` | `manage.py alerts`: the `/health` URL to probe from outside (empty = skip) |
| `ALERT_DISK_PATHS` / `ALERT_DISK_WARN_PCT` / `ALERT_DISK_CRIT_PCT` | mount points to watch (default `/`), warn at 85 %, critical at 95 % |
| `ALERT_BACKUP_MARKER` / `ALERT_BACKUP_MAX_AGE_HOURS` / `ALERT_BACKUP_EXPECT_OFFSITE` | the marker `backup-database.sh` writes; alert when older than 26 h, missing, or not offsite |
| `ALERT_STATE_FILE` | where the health check remembers its last state (`~/data/alerts-state.json`) |
| `SEC_CACHE_DIR` | *(empty)* | Local layer of the EDGAR filing cache (immutable Archives files only; never searches). Needs a persistent disk — leave unset on Render |
| `OBJECT_STORE_BUCKET` / `_ENDPOINT` / `_ACCESS_KEY` / `_SECRET_KEY` | *(empty)* | The shared S3-compatible object store (`app/objectstore.py`) — one bucket for every environment, a prefix per purpose; `edgar/` is the filing cache's shared layer, `geocode/` the address cache's (so a rebuild does not re-ask Nominatim for every address). Any S3 API (Hetzner, Scaleway, AWS); keys are secrets, env only |
| `SCRAPER_OPENCORPORATES_ENABLED` | `false` | OpenCorporates source switch |
| `SCRAPER_AUTODEDUP_ENABLED` | `true` | Auto-merge high-confidence duplicate persons after each `run-all` scrape |
| `FEDERATION_ENABLED` | `false` | Enable trusted-peer federation (publish/pull) |
| `FEDERATION_SIGNING_KEY` | — | Ed25519 private seed (base64) for signing exports; generate with `manage.py gen-federation-key`. Secret — env only |
| `OPENCORPORATES_API_KEY` | — | OpenCorporates API token (optional) |
| `GEOCODING_ENABLED` | `false` | Geocode addresses to coordinates via Nominatim |
| `GEOCODING_CONTACT` | — | Contact email added to the Nominatim User-Agent (required by their usage policy) |
| `GEOCODING_USER_AGENT` | `owlgraph-ownership-platform` | Base User-Agent for Nominatim requests |
| `NOMINATIM_URL` | public endpoint | Nominatim search URL (override to self-host) |
| `GEOCODING_MIN_INTERVAL` | `1.0` | Minimum seconds between geocoding requests |
| `DEBUG` | `false` | FastAPI debug mode |
