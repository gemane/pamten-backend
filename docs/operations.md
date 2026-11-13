# Operations

Running the database day to day: backups, getting one back, and the service
account a rebuild takes with it.

## Setting up a production server

`deploy/setup-server.sh` takes a fresh Debian/Ubuntu box to serving in one run, from
one filled-in env file (`deploy/.env.example`); day two is `owlgraph <command>`
(`deploy/ops.sh`). Everything — the steps, the prerequisites, what was tested — is
in [`deploy/README.md`](../deploy/README.md).

## GLEIF history (once)

After the first full import, run `owlgraph gleif-history` (dev box: `manage.py
gleif-rr-history`) once: ended GLEIF relationships since 2018 and "at least since"
starts, from one archived relationship file per month (~1.5 GB downloaded, each
file deleted after reading). `--dry-run` reports what it would write. It keeps the
small intervals file (`/data/gleif-rr-history.jsonl.gz`), so re-running after a
restore downloads nothing; `--rebuild` downloads again. Manual, never cron: from
then on the daily `gleif-update` records endings as they happen. See
[data-model.md](data-model.md), GLEIF.

## Releases & versions

One product version for the API, the web app and the Android app, in semantic
versioning, **from the git tag only** — `app/version.py` reads `APP_VERSION`, which the
release build passes in; every other build reports `0.0.0-dev` (+ the commit on Render).

- **Release = tag `vX.Y.Z` on `main`**, with the same number in pamten-frontend
  (`~/scripts/release.sh X.Y.Z` does both). `.github/workflows/release.yml` checks the
  tag format and that the commit is on `main`, runs the unit tests, builds the image
  with the version baked in, proves the image reports it, pushes
  `ghcr.io/<owner>/pamten-backend:X.Y.Z` (never `latest`) and creates the GitHub
  release with notes from the merged PRs since the previous tag.
- **Production runs a named version**: the compose file says `pamten-backend:X.Y.Z`,
  so what is deployed is readable from it, and a rollback is the previous number.
- **Order on release day:** API first — it must keep serving the previous app — then
  the web app, then the Android rollout. Raise the minimum app version only when an
  old app would really break.
- **Schema changes go in a maintenance window, not at startup** — on a large database
  DDL takes the schema lock (see the sizing notes in the hosting plan).
- Hotfix: fix on develop → fast-forward main → tag the next patch version.

## Upgrading ArcadeDB

The database is a pinned image (`ARCADEDB_VERSION` in `deploy/.env`; the dev box's own
compose file). An upgrade within a year's releases is drop-in — same data directory, no
migration — but never blind:

1. **Read the upstream tracker first** (the rule since 2026-09-23): the issues *fixed since
   our version* and the ones *opened since the target was published*, filtered to what we
   use (LSM index range/seek, compaction, FULL_TEXT/CONTAINSTEXT, HTTP `/command`, Cypher
   MERGE/MATCH, backup/restore, sqlscript, startup). Confirm a fix is in the **tag**, not
   only on its milestone (`gh api repos/ArcadeData/arcadedb/compare/<fix sha>...<tag>` must
   say `ahead`). A new release can be a trap: 26.9.1 made `sqlscript` 1.5–3× slower.
2. **Prove it on the test container**: `ARCADEDB_IT_IMAGE=arcadedata/arcadedb:X.Y.Z`
   with the full integration suite, then the index range reproducers
   (`~/arcadedb-report/arcadedb_range_repro.py load --rows 10000000` + `walk`, and
   `_more.py`): walked must equal loaded, every bound exact.
3. **Before touching the real database**: fast-forward `main` (a stale main is not a
   fallback), take a backup (`python3 manage.py backup-database`) and keep the previous
   image tag at hand — the rollback is the old tag on the same data.
4. **Upgrade**: bump the tag, `docker compose pull arcadedb && docker compose up -d`
   (`ops.sh upgrade` covers the *application* version only). Watch the log for
   `Index 'X' should be rebuilt: its pages are physically sorted in a different order`.
5. **Rebuild what it asks for, over a direct connection** — `REBUILD INDEX *` on the full
   database runs for 10+ minutes, longer than the nginx proxy allows: open an SSH tunnel
   to the box (`ssh -N -L 2481:127.0.0.1:2480 <db host>`) and run
   `python3 manage.py reindex --db-url http://localhost:2481`.
6. **Accept**: `/health` ok; a search for a common token answers as many rows as a
   scan (`CONTAINSTEXT` vs `LIKE`); a whole-space id walk counts every Entity and Person;
   a backup taken on the new version restores into a fresh database. A `CONTAINSTEXT`
   that answers **0** where the scan finds rows means the FULL_TEXT index is empty: `/search`
   still answers companies through its name-`LIKE` fallback (no aliases) but persons not
   at all. Save the server log first (`docker compose logs arcadedb | grep -i
   "fulltext\|bm25\|corpus\|severe\|error"` — the evidence goes with the rebuild), then
   `python3 manage.py rebuild-search --hard` (drop + re-create; seconds on the dev graph,
   over a tunnel on a full import). Seen on the dev box's 26.7.3 → 26.10.1 upgrade on
   2026-10-11 and not reproduced on a copy (incremental, bulk-built + compacted, and
   updated + deleted + rebuilt indexes all survived the same reopen).

**26.10.1 (published 2026-10-05, reviewed 2026-10-10):** the first release with the
index range-read fix (PR #7616 for #7611, in the tag) and the rest of the 26.7.3 list
(#5120/#6997 compaction lookups, #5266 FULL_TEXT completeness, #7025 unclean close,
#6382 CONTAINSTEXT and ':', #7768 WAL slot, #7464/#7637 backups without schema, #8225
BM25 counters); four security advisories closed (Gremlin, Bolt, lightweight edges — none
on our surface). Opened against it and still due for 26.11.1: **#9495** (every HTTP
`UPDATE`/`DELETE` fails with 500 on the official jars under JDK 25 — the Docker image runs
Java 21 and our writes were verified on it, but do not run 26.10.1 jars on Java 25),
#9461 (a NOTUNIQUE LSM full scan turns quadratic after one hot key — our maintenance walks
the UNIQUE `id` index, which is unaffected), #9402 (an in-heap GROUP BY budget that scales
with the core count — the sharded dedup GROUP BY on a many-core prod box may need
`arcadedb.query.heapBudget` raised), #9369 (a concurrent reader can miss a key a
transaction deletes and re-creates in one commit — transient, retried by the next read).
The REPEATABLE_READ regressions (#9070/#9620) do not apply: we use the default isolation.
**It also changes a semantic we relied on:** comparisons are three-valued now — `type <> 'voting_group'`
(SQL and Cypher, `!=` and `NOT (… = …)` alike) no longer matches a row whose `type` is null,
where 26.7.3 matched it. 1,298 of the dev graph's 6,064 entities have no type, and the
quality report, the duplicate scan, the country backfill, the missing-country list and the
federation export all went silent on them. Fixed in the app first with an `IS NULL OR`
fence (`anchors.not_a_voting_group`), then by removing the need for it: since 2026-10-26 a
voting group is its own vertex type and no entity query fences anything off
(`tests/integration/test_null_type_it.py` runs on both engines); the rule for every new
query stays: a `<>` on a nullable property needs an `IS NULL OR` beside it.

## Backups

```bash
python3 manage.py backup-database        # one consistent archive, taken online
bash ~/scripts/backup-database.sh        # the same, plus verify + rotate + offsite
```

`BACKUP DATABASE` is ArcadeDB's own online backup. It produces an archive consistent as of one
moment while the server keeps serving.

**A disk or VM snapshot is not a substitute.** ArcadeDB writes data pages and a write-ahead log,
and a snapshot taken mid-write can capture them out of step. It usually restores. The time it does
not is the time you needed it. If the host does snapshots anyway, the right arrangement is to run
this backup on a schedule and let the snapshot capture the resulting *archive*.

### Where the file goes — you do not choose

The server writes it, into `arcadedb.server.backupDirectory` (default
`<rootPath>/backups/<database>/`), named `<database>-backup-<timestamp>.zip`. Passing a path is
rejected outright:

```
BACKUP DATABASE file:///somewhere/x.zip
  -> Backup file cannot contain path change because the directory is specified
```

So the caller only ever learns a filename, and anything that wants to touch the file — verifying,
rotating, copying it away — has to run on the machine the database runs on. In production that
directory is the host side of the container's volume mount. Against a remote database
`backup-database.sh` takes the backup and stops there, saying so, rather than pretending to rotate
files it cannot see.

### What the wrapper adds

| | |
|---|---|
| **Verify** | Reads the zip's central directory, which sits at the end of the file, so a truncated archive is caught rather than trusted because the server said OK |
| **Rotate** | Keeps `KEEP` archives (default 7), **after** verifying and copying offsite — deleting history before the new copy is proven is how a bad night becomes a lost year |
| **Offsite** | `rsync` to `BACKUP_REMOTE` if set. A backup that exists only on the machine it protects is not a backup |
| **Fails closed** | Any failure exits non-zero and rotates nothing |

```
KEEP=14                                                    # default 7
BACKUP_DIR=/srv/arcadedb/backups/owlgraph                  # host side of the volume
BACKUP_REMOTE=u123456@u123456.your-storagebox.de:owlgraph/ # optional
```

Cron it **before** the GLEIF delta, not after, so there is always a copy of the state the delta
started from:

```cron
0 3 * * * /bin/bash /home/administrator/scripts/backup-database.sh >> /home/administrator/data/backup.log 2>&1
```

## Restoring

Deliberately not automated: it writes a database directory, and the failure mode of getting it
wrong is losing the live one.

```bash
docker compose stop api                       # stop writers first
docker compose exec arcadedb ./bin/restore.sh \
    -f backups/owlgraph/owlgraph-backup-20260813-144217852.zip \
    -d databases/owlgraph-restored
```

Restore **beside** the live database, never over it. Then open `owlgraph-restored`, check it holds
what you expect, and only then swap the directories and restart. Verified end to end on ArcadeDB
26.7.3: backup → restore into a new database → the rows were there.

Note the script is `bin/restore.sh`, not `arcadedb-restore.sh` as some documentation says.

**Run a restore drill.** An untested backup is a belief, not a backup — restore last night's
archive into a scratch database occasionally and count the rows.

## Weekly digest

`python manage.py weekly-report [--week 2026-W38] [--email] [--json]` prints the
activity digest for the last completed calendar week (Monday–Sunday, UTC):
searches (per-week counters — how many, top queries, how many found nothing),
scrapes (runs by source, companies scraped for the **first time** vs
**refreshed** — told apart by the run's recorded `reason` — records written, SEC
enrichments, failures), imports (GLEIF/PSC runs), and graph totals with the
change since the previous stored digest, and **source calls** — how often each
outside source was asked (by answer: ok, 4xx, 429, 5xx, no response) and what the
shared bucket did per prefix (hit = a source fetch saved, miss, put), from the
`SourceCall` rows. `--email` sends it to `REPORT_EMAIL`
(falling back to `ADMIN_EMAIL`) through the configured email backend. Every run
stores the digest (`WeeklyReport`, one per week) so the next can measure growth.
Aggregates only: it names companies, never who searched.

Manual-first. To get it every Monday at 06:00 UTC:

```
0 6 * * 1 /home/administrator/scripts/cron-weekly-report.sh
```

The wrapper takes a lock and logs to `/home/administrator/data/weekly-report.log`.
The same numbers are on the Scraper tab (admins) and at `GET /analytics/weekly`.

### Source calls

`python manage.py source-calls [--days 7 | --from YYYY-MM-DD] [--to YYYY-MM-DD]`
prints the calls to each outside source and the bucket operations for those UTC
days (`SourceCall`, see `docs/data-model.md`). Counted by the API and by every
`manage.py` command, flushed each minute and at exit; a process killed hard loses
at most its last minute. Not counted: downloads made by shell scripts (`curl` in
the golden-copy refresh and the bulk-import steps) and the database itself. The
rebuild prints it in step 8 (every row of the fresh database is the rebuild's) and
keeps it in `after.json`, so `diff_snapshots.py` shows the change from the last
rebuild. Off with `SOURCE_CALLS_ENABLED=false`.

The Graph section also lists **groups with more than 500 direct subsidiaries**
(`LARGE_GROUP_THRESHOLD`), largest first — the ones the profile's section cap
(900 since 2026-09-27, temporarily) cuts or nearly cuts, and the ones to look at
one by one for a split into their real tree. Counted as the profile counts
(active edges, proven shortcuts excluded), on the same streaming owner walk as
the OWNS dedup, so it is cheap on the dev subset and one pass on a full import.

## Monitoring

Built in layers, from the box outwards (decided 2026-09-28; the outer layers are
still to set up on the production box):

1. **`GET /health` is honest.** It runs one query against ArcadeDB (bounded at
   4 s) and answers `200 {status: ok, version, database: ok}` or
   `503 {status: degraded, database: unreachable}`. A process that is up but
   cannot reach the database is not healthy. Unversioned, so the URL an uptime
   check points at never moves.
2. **Compose healthchecks** (`deploy/docker-compose.yml`): `arcadedb` on
   `/api/v1/ready`, `api` on `/health`, `caddy` on `/health` through the proxy;
   `restart: unless-stopped`; logs capped per container. `docker compose ps`
   shows `(healthy)` or not. Self-healing, but blind to the box itself being down.
3. **A dead man's switch per cron job.** Every wrapper in `~/scripts` calls
   `owlgraph_watch <job>` (`lib.sh`): a ping at start, a ping on success,
   `/fail` on any non-zero exit. The checker alerts when a run fails **or when
   the next ping does not arrive** — the case a log can never report. URLs live
   in `~/.config/owlgraph/healthchecks.env` (`HC_GLEIF_UPDATE=…`, `HC_BACKUP=…`,
   `HC_WEEKLY_REPORT=…`, `HC_PRUNE_ANALYTICS=…`); no file means no pings, and a
   ping failure never fails the job. Healthchecks.io, or its open-source
   self-hosted twin on the dev box, is the intended checker.
4. **`manage.py alerts`** — what is wrong right now, mailed only when there is
   something (`--email`, to `REPORT_EMAIL` / `ADMIN_EMAIL`). Four checks, each
   skipped when its setting is empty:
   - `imports`: an import run that failed in the last 24 h, or a run stuck
     `running` past the run log's stale threshold;
   - `backup`: the marker `backup-database.sh` writes after verify + offsite
     copy (`ALERT_BACKUP_MARKER`, e.g. `/home/administrator/data/backup.last`)
     older than `ALERT_BACKUP_MAX_AGE_HOURS` (26), missing, or not copied
     offsite (`ALERT_BACKUP_EXPECT_OFFSITE`);
   - `disk`: `ALERT_DISK_PATHS` (comma-separated mount points) above
     `ALERT_DISK_WARN_PCT` (85) / `ALERT_DISK_CRIT_PCT` (95);
   - `health`: `GET ALERT_HEALTH_URL` — the **outside** check when run on the
     dev box against production. Reported on change only (down, then
     recovered), remembered in `ALERT_STATE_FILE`, so a five-minute cron does
     not mail every five minutes.

   It exits 0 whenever the checks ran — an alert is an answer, not a failure;
   the checks crashing is what the dead man's switch on the wrapper reports.
   Manual-first; the intended cron lines (`cron-alerts.sh` in `~/scripts`
   takes the same arguments):

   ```cron
   */5 * * * * /home/administrator/scripts/cron-alerts.sh --only health --email
   0 7 * * *   /home/administrator/scripts/cron-alerts.sh --email
   ```

   The first line is the uptime check from outside the production box; the
   second the daily "anything wrong?" mail. Both need `--email`, or they only
   print.

## Retention and personal data

Backups contain the personal data in the graph (PSC people: names, birth months, addresses), so
the retention window belongs in the Art. 30 record, and an erasure request has to say what happens
to copies sitting in backups. The usual answer — copies age out on a documented cycle rather than
being surgically edited — is defensible, but only if it is written down.

## Service accounts after a rebuild

`new-database.sh` drops the database, and users live in it. Only the account named
by `ADMIN_EMAIL` comes back, because the app re-provisions that one at startup —
any *other* account is simply gone, including the contributor that
`scripts/update.sh` logs in with. The symptom is not obvious: the scrape
authenticates against nothing, every company returns 401, and on 2026-08-28 the
run exited without scraping anything.

`finish-import.sh` restores it as its last step, so a rebuild leaves a database the
scrape can actually use:

```bash
ENSURE_USER_PASSWORD=… python3 manage.py ensure-user \
    --email scraper-service@owlgraph.org \
    --role contributor \
    --confirm-database "$ARCADEDB_DATABASE"
```

- **Idempotent.** An existing account has its role corrected and is marked
  verified; its password is left alone. Safe to run on every import.
- **The password comes from the environment**, never from an argument — argv lands
  in shell history and in `ps` output for every user on the box. Without it, an
  account that does not exist is refused rather than created blank.
- **`--confirm-database` must match** the configured database. The command mints a
  privileged account from whatever the environment says, so it gets the same guard
  as `wipe-source`.
- The rebuild step reads the credentials from `~/.config/owlgraph/scrape.env`, and
  skips (loudly, non-fatally) when that file is absent.

## Scrape run log

Every scrape (`/scraper/run`, `/run-all`, `/sec-edgar/run`,
`/open-corporates/run`) records a `ScrapeRun` row: a `running` entry on start,
updated to `ok` (with node count) or `failed` (with the error) on finish. `GET
/scraper/runs` lists them newest-first, so the UI and other sessions can see
what's scraping now and which runs failed — across the panel *and* the bundled
`scrape_companies.sh` script. Every run that is about one company records the
company it resolved (`entity_id`) beside the typed target, so the weekly digest
can name it; a bulk run (a GLEIF delta, a PSC snapshot) has none. What a run
does not know is **absent**, never an empty string: no `error` on a clean run,
no `entity_id` on a run that resolved no company, `finished_at` only once it
has finished (until 2026-10-25 the row was created with `''` in all three).
A `run-all` row is `failed` when **any** source errored, with the error naming
each one (`sec_edgar: …`) and the total of the sources that did write; until
2026-10-28 it read `ok` whatever a source raised, so a Toast SEC failure
showed nowhere.

A row is **`partial`** when a scrape wrote what arrived but lost a whole
section to the network: the SEC scrape's 13D/G owners, insiders or holdings
when the one request each rests on failed for a reason other than a 404. Its
`error` names the sections (`lost to the network: 13D/G owners`), and the
company gets `scrape_incomplete_at`, so the next on-demand request scrapes it
again once the cooldown has passed instead of after the 30-day TTL. Until
2026-11-02 such a run read `ok`: the 11-01 rebuild lost 19 of Gitlab's owners to
one read timeout and nothing said so (findings item 41).
A source that raised inside a run-all or an on-demand scrape — Wikidata's label
search timed out — wrote nothing, and the company the other sources resolved gets
`scrape_incomplete_at` the same way (`graph_writer.mark_source_lost`), so every
source runs again after the cooldown; until 2026-11-05 it was fresh for 30 days
on what the others wrote, and for T. Rowe Price that was another legal entity
(findings item 54).

A lost **single** request (one Form 4, one filing index) leaves an SEC run
`ok` — the scrape goes on without that filing — but the log says
`SEC EDGAR: request lost (…) on <url>` and the row's `error` reads
`N request(s) lost to the network`, so a rebuild can tell a gap the network
made from one the code made (findings item 45).

The endpoint is **public**: what the platform ingests, against which company and
when, is the kind of thing an ownership-transparency project should be open
about, and the frontend shows the feed to every visitor. The `error` field is the
exception: it holds raw exception text, which can include internal URLs, database
errors, or a credential embedded in a failing request URL, so it is stripped for
anyone who isn't a `contributor` or `admin`. Everyone still sees that a run
failed — just not the stack's opinion about why.

The log is **bounded**: capped at 500 records, with the oldest pruned on every
write, so it can never grow the database unbounded. A `running` row older than 30
minutes is flagged `stale` (an interrupted run). Surfaced in the web app's
**Scraper tab → Recent activity** panel, which polls while a run is in progress.

## Usage measurement

Counting what the product is asked for, so the roadmap follows demand rather than
guesswork: which companies people search for, which searches find nothing (the
best possible list of registers to add next), which features get used, and how
the endpoints are behaving. Off by default — `ANALYTICS_ENABLED`.

The design constraint is that it must not become surveillance, and one detail
forced the whole shape: `SearchBar` calls `/search` every 300 ms while typing, so
counting requests would record `mi`, `mic`, `micr` — useless as demand data and
*more* intrusive than what we wanted, since partial typing exposes hesitation and
correction. So the **client reports once, when a search settles** (a result was
taken, nothing was found, or nothing was chosen), and the server keeps **totals,
never events**.

Three keyed counter types, in the database alongside `ImportState` and `GeoCache`:

| type | key | counts |
|---|---|---|
| `SearchDemand` | normalised query + ISO-2 country | searches, zero-result searches, searches where a result was taken |
| `UsageCounter` | an allow-listed event name (`export.ods`, `map.basis.hq`, `result.rank.3`) | occurrences |
| `EndpointStat` | `METHOD route_template status_class` + latency bucket | requests |

**No row can be tied to a person.** No user id, no account link, no session id, no
stored IP address (a salted hash lives in memory for rate limiting and is never
written), no device identifier, no user agent, and no per-event timestamp — only
first- and last-seen on an aggregate. There is no event log, so no individual's
activity can be reconstructed even by us. `EndpointStat` keys on the **route
template**, never the raw path, so `/entities/{id}` does not mint a row per
company.

`POST /v1/analytics/event` is public and anonymous, rate-limited like `/flags`,
and strictly allow-listed: an unknown event name is rejected, `query` is capped at
120 characters and `rank` at 0–49. An open-ended key column on a public endpoint
is unbounded row growth and a junk-injection vector. Reading it back —
`GET /v1/analytics/searches` and `/usage` — is admin-only and paged.

**The one honest caveat:** the `SearchDemand` key is free text, and somebody will
search for a person's name. That name is stored, unlinked to whoever searched for
it, readable only by an admin, and deleted once the query has been idle for twelve
months (`prune-analytics`, daily at 05:15 via `~/scripts/cron-prune-analytics.sh`).
The store is anonymous with respect to the *searcher*, not with respect to
everyone who might be named in a search box — which is exactly how the record of
processing and the published privacy notice put it. If the retention window here
changes, those change with it.

**Source calls** are counted apart from all this, and carry no user: `SourceCall`
keeps per UTC day how often the backend asked each outside source (EDGAR, GLEIF,
Wikidata, Wikimedia, Nominatim, Companies House, OpenCorporates) and how the shared
bucket answered (hit, miss, put) — `manage.py source-calls`, the weekly digest's
*Source calls* section, and the rebuild's step 8 (docs/operations.md).

## Deployment on Render

Deployed on Render as a web service. Any push to **`develop`** triggers an automatic redeploy — Render tracks the integration branch, so what runs there is the dev environment (pointed at the dev database). `main` is the verified branch and currently deploys nowhere; production is planned on a separate, non-Render host. Required environment variables must be set in the Render dashboard: `ARCADEDB_URL`, `ARCADEDB_USERNAME`, `ARCADEDB_PASSWORD`, `SECRET_KEY`, `CORS_ORIGINS`. Set `ADMIN_EMAIL` + `ADMIN_PASSWORD` too so the admin is provisioned automatically on boot (see [Authentication](authentication.md)).

**Domains.** The dev environment is reached at `dev.owlgraph.org` (frontend) and `api-dev.owlgraph.org` (API), both CNAMEd to the Render services. Keeping the two on one registrable domain is deliberate: they are **same-site**, which is what allows an `httpOnly; SameSite=Lax` auth cookie to pass between them. The `*.onrender.com` pair cannot do that — `onrender.com` is on the Public Suffix List, so each subdomain is a separate site and a cookie between them would be third-party, blocked by Safari and Chrome. Any future production pair should keep the same shape (`owlgraph.org` / `api.owlgraph.org`).

> **Env var changes apply on a *deploy*, not a restart.** Editing env vars in the dashboard triggers a deploy that applies them. But an env var set via the Render **API** does not auto-deploy, and a plain **"Restart Service"** restarts with the *already-deployed* config — so a value changed via the API only takes effect after a real deploy (**Manual Deploy → "Clear build cache & deploy"**, or a new push to `develop`).

## Schema & indexes

**Every property a writer sets is declared** (`app/db/schema.py`; since 2026-10-25 the strings too, `_STRING_PROPERTIES` — ArcadeDB accepts an undeclared property, so the catalog is a ledger that `manage.py schema-audit` checks the data against, and a typo'd property name shows up by name). Every number, flag and list has a **declared type** (`_TYPED_PROPERTIES`: `stake_percent` and `shares` DOUBLE (a Form 4 reports fractional shares), `founded` INTEGER, `is_nominee` BOOLEAN, `aliases` LIST, …), so ArcadeDB coerces every write and refuses one it cannot convert; dates stay strings because the writers keep a source's precision (`2019-00-00`). The bootstrap below declares them in scripts of 60 statements (520 on a fresh database: 0.4 s batched, 5.5 s one request each — which is what made the integration suite slow), drops and redeclares one the server holds with another type (ArcadeDB has no `ALTER PROPERTY … TYPE`), and declares the **rules** beyond type (mandatory ids, claim keys and a claim's source, the `type` / `ownership_type` / `read_from` / claim `kind` vocabularies from the code's enums, ISO-2 countries, the shape of every date and timestamp, 0–100 percentages, `stale` defaulting to false — see docs/data-model.md *Rules the database enforces*); a rule is refused while a stored row breaks it. A declaration holds for new writes only: a fresh import is typed throughout, a loaded database keeps the type its rows were stored with.

Lookup indexes (on `id`, `name`, `name_normalized`, `wikidata_id`, `sec_cik`, and a unique `User.email`) are created automatically on startup — the app runs an idempotent, best-effort bootstrap that is a no-op once they exist. To (re)create them explicitly, e.g. against a fresh database:

```bash
python3 manage.py init-schema
```

## CLI (`manage.py`)

| Command | Description |
|---|---|
| `init-schema` | Create vertex/edge types and lookup indexes (idempotent) |
| `list-mixed-generations` | Persons whose own names carry both "Sr." and "Jr." (or II and III) — a relative folded in by a merge before the duplicate scan learned to tell them apart (2026-10-25). Report only; split by hand (see docs/deduplication.md) |
| `heal-ends-at-dissolution` | Close every open holding and seat of a dissolved company on its `dissolved_on` (`until_reason = dissolution`, cited by the company's Wikidata item) — the rule the Wikidata writer applies since 2026-10-25, for a database written before it or loaded in bulk (the GLEIF and Companies House importers set the date without closing anything). Founders stay open; an edge the source dates after the dissolution is reported, not closed. Every import chain runs it with `--apply` after the dedup (`finish-import.sh`, `ops.sh import`), and so does the daily GLEIF update before it re-marks the shortcuts (`cron-gleif-update.sh`, `ops.sh gleif-update`). A scrape needs no step: its scope applies the rule to every company it touched or wrote an edge onto. Dry run unless `--apply` |
| `wikidata-suggestions` | What GLEIF and EDGAR would correct on Wikidata's LEI (P1278) and CIK (P5531) statements: a CSV of findings (`missing` / `differs` / `unknown` / `several`, with the register's name and record page; `link = refused` for an item the scraper refuses for a node, found by the node's register ids) and a QuickStatements batch of the confident additions, for a person to review and submit. Reads the graph, Wikidata and the registers; writes to none of them. `--out DIR`, `--limit N` |
| `schema-audit` | Name every property the stored rows carry that the schema does not declare — a typo'd writer, a field nobody put into the ledger, a leftover of a rename. Samples `--sample` rows per type (default 5000); exit 1 on drift. ArcadeDB accepts an undeclared property, so this is how the catalog stays the model (2026-10-25) |
| `merge-entities` | Fold one entity into another by hand: `--keep ID --dead ID`. For a pair the automatic dedup cannot see — no shared hard id — that a human has judged to be one company (the two Televisas, 2026-10-25). The survivor's values win, its gaps fill from the loser, edges and claims move, a forwarding address and a merge-log row are written; refused when the pair carries a keep-separate mark. Dry run unless `--apply` |
| `dedupe-persons` | Merge the persons two sources wrote as two nodes — same name, a shared company or birth date, high confidence only; the rest is reported for review. The net under the post-scrape dedup, which sees only what one scrape touched; the rebuild runs it after the replay. `--dry-run` reports, `--scraped-only` seeds from the persons with a SEC CIK or Wikidata item instead of scanning everyone (bounded on a graph with millions of register persons) |
| `seed` | Seed the built-in company list |
| `wipe-source` | Delete **one source's** data — its edges + the nodes only it created (nodes another source still references are kept); keeps user accounts, schema, and other sources. Finishes with `REBUILD INDEX *` to clear the stale index entries the batched deletes leave behind (else a later re-import can 500). There is **no** whole-DB wipe — drop the database for a fresh start. Requires `--source <name>` plus `ALLOW_DESTRUCTIVE_WIPE=true` **and** `--confirm-database <name>` matching the connected DB; add `--db-url http://localhost:2480` so the reindex isn't cut off by a proxy timeout, e.g. `ALLOW_DESTRUCTIVE_WIPE=true python manage.py wipe-source --source "UK PSC" --confirm-database owlgraph --db-url http://localhost:2480` |
| `geocode` | Backfill coordinates via Nominatim for **both** of a company's places: its headquarters (`hq_address` → `hq_lat/hq_lng`) and its registered office (`address` → `reg_lat/reg_lng`); the two passes are built from the address registry (`app/addresses.py`). They differ exactly where it is interesting — BARCLAYS CAPITAL (CAYMAN) is registered at its agent's door on Grand Cayman and run from London — and the map's Registered/Headquarters switch draws one or the other. `--target hq\|registered` runs a single pass; each is resumable on its own missing coordinate. |
| `backfill-industry` | Give every company a sector its sources already know: SEC filers' SIC code from EDGAR, UK companies' SIC codes from the stored register snapshot — `IndustryCode` statements plus the `nace_code` the panel's sector tree is drawn from. Fill-only; `--limit` caps the EDGAR fetches. Also `POST /scraper/backfill-industry`, which the scrape scripts run after their passes. |
| `gen-federation-key` | Generate an Ed25519 signing keypair for [federation](federation.md) |
| `gleif-lei-cdf` / `gleif-rr` / `gleif-succession` | Import GLEIF golden-copy files (entities / direct+ultimate parents / mergers — a DUPLICATE or ANNULLED registration naming its live record is not one, item 88; each takes `--only-file` for the curated subset, and the test chain runs all three) — see *GLEIF sourcing* in [`docs/data-model.md`](data-model.md) |
| `ch-psc-update` | Refresh UK PSC **incrementally**, by diffing today's Companies House snapshot against the digest of the last one applied — the source publishes no deltas. Closes ceased and withdrawn holdings, reopens corrected ones, and is a no-op when nothing moved. Guarded by a churn limit that refuses before writing; `--dry-run` reports the diff. Run by hand, not scheduled |
| `ch-officers` | Read the **serving officers** (directors, secretaries, LLP members) of every UK company in the graph from the Companies House API and write them as seats; a seat whose holder has resigned since is ended on the register's date. `--company N,N`, `--limit N`, `--dry-run` (writes nothing). Needs `COMPANIES_HOUSE_API_KEY`; 600 requests per five minutes, the reader waits for the window. **Run by hand** — no cron. See `docs/sources.md`. |
| `gleif-repex` | Import GLEIF **reporting exceptions** (`repex` = GLEIF's abbreviation). Every LEI holder must name its parent *or file a reason why not*, so "no parent recorded" and "a parent exists and was withheld" are different facts — `NATURAL_PERSONS`, `NON_PUBLIC`, `NO_LEI`, … Take the code meanings from GLEIF's XSD rather than their names: `NO_LEI` means the parent *refuses* an LEI, not that it lacks one. See *Why a company reports no parent* in [`docs/data-model.md`](data-model.md). Only ever updates entities already held; never creates one |
| `gleif-update` | Apply a GLEIF **delta** on top of the full load — the retirement-aware daily refresh (new/changed entities, merges, closed relationships). `--interval auto` (default) is gap-aware: it picks the smallest delta window covering any missed runs since the last one (fails loudly past ~30 days → full-reload). Override with `--interval LastDay\|LastWeek\|LastMonth` or pass `--lei-file`/`--rr-file`/`--repex-file`. Idempotent; runs against the live-indexed DB (no `--bulk-load`). Daily via `~/scripts/cron-gleif-update.sh` |
| `ch-psc` | Import a Companies House PSC snapshot (current UK beneficial ownership). Add `--bulk-load` on a full import to drop secondary indexes for the load and rebuild after (much faster; collapse duplicate edges afterwards with `POST /scraper/deduplicate-edges`). Company names come from a companion `ch-company-data` import |
| `ch-company-data` | Enrich UK companies with names/addresses (the registered office, as a string and in parts for the geocoder — never as a headquarters)/former-names/SIC codes/`company_status` (as the register words it, with `active`; the snapshot lists live companies only) from a Companies House BasicCompanyData snapshot (the full register). Enrichment only — updates companies already in the graph, whether keyed `gb-coh:{number}` (from `ch-psc`) or holding the number under a GLEIF id, never creates isolated nodes |
| `backfill-search` | Populate the FULL_TEXT `search_text` column powering `/search`. Run once after a bulk import (the importers set it inline, but this covers pre-existing rows). |
| `rebuild-search` | REBUILD the FULL_TEXT `search_text` indexes so `/search` (`CONTAINSTEXT`) finds every row — needed after a non-bulk / `--only` import (the FULL_TEXT index isn't maintained incrementally). `--hard` first DROPs + re-CREATEs the indexes: use it to recover a **stuck/corrupted** index that a plain REBUILD reports "ok" on but never repopulates (e.g. after a bulk-load's REBUILD was cut off mid-flight by the nginx 60s proxy timeout). Run `--hard` against `--db-url http://localhost:2480` so it isn't cut off by that same proxy again. |
| `set-password` | Set one account's password directly: `python manage.py set-password someone@example.com`. Prompts twice with hidden input (`--password` exists for scripting but puts the secret in shell history and `ps`). Applies the same policy as the API. The operator escape hatch for when neither in-app route works — the reset flow needs email, `/auth/change-password` needs the current password, and `ADMIN_PASSWORD` only ever seeds a *missing* account. |
| `sec-ex21` | One issuer's statutory subsidiary list from its latest 10-K Exhibit 21 (20-F Exhibit 8.1; a company listed within the year, with no annual filing yet, from its S-1/F-1/Form 10 Exhibit 21.1): `python manage.py sec-ex21 "Apple" [--force]`. The list is the document the filing index declares EX-21 (EX-8.1 in a 20-F), whatever its file name. Significant subsidiaries only — the rulebook's cut, not ours; `sec-ex21-history` then dates them by their oldest listing. Needs the CIK (run the SEC EDGAR scrape first). |
| `sec-acquisitions` | One filer's acquisitions from its own 10-K / 10-Q **XBRL** facts: `python manage.py sec-acquisitions "Alphabet" [--force] [--max-filings N]`. Acquirer → acquired company with the **closing date as a stated start** and the price paid, read from fields — the one SEC family that dates an ownership edge, and it names companies the Exhibit 21 omits (Alphabet lists three subsidiaries; Wiz and Intersect are here). Announced-only deals are counted, never written. A pair the Exhibit 21 already draws keeps the list's answer and gains the date and price; a pair a register draws through a chain is flagged a shortcut. Needs the CIK. See [`docs/sec_edgar_scraper.md`](sec_edgar_scraper.md). |
| `close-reported-by-parent` | End a subsidiary's Schedule 13D/G holding once its parent (an open controlling edge: Exhibit 21, GLEIF) reports the same company in a newer 13D/G (`until_reason = reported_by_parent`). Runs after every SEC scrape on its own; this is the whole-graph pass for a rebuild. See *A parent's newer statement* in [`docs/sec_edgar_scraper.md`](sec_edgar_scraper.md) |
| `close-ended-classes` | End every Schedule 13D/G holding whose shares ceased to exist — a merger's Form 25-NSE under Rule 12d2-2(a), a takeover's Form 15 with at most two holders of record — unless a successor issuer took the class over or the issuer is still a 13D/G subject (`until_reason = class_ended`; a statement of ≥ 99.5 % is the acquirer's and stays). Reads EDGAR: needs `SCRAPER_ENABLED` + `SCRAPER_SEC_EDGAR_ENABLED`. Runs in every company scrape for the company itself; this is the whole-graph pass for a rebuild. `--limit N` for a trial. See *When the shares ceased to exist* in [`docs/sec_edgar_scraper.md`](sec_edgar_scraper.md) |
| `close-absent-from-table` | End every Schedule 13D/G holding of 5 % or more whose holder the issuer's newest ownership table (DEF 14A or 10-K/A Part III, at most 18 months old) no longer names — under its own names, its controlling parents' or any reporting person its statement names — on the table's filing date (`until_reason = not_in_issuer_table`, an upper bound). Statements filed within 60 days of the table are not judged. Also ends a holding an earlier table wrote (findings item 86) that the newer one leaves out. Never writes holders — the company's own scrape does. Reads EDGAR: needs the scraper flags. Runs in every company scrape for the company itself; this is the whole-graph pass for a rebuild. See *When the issuer's own table no longer names the holder* in [`docs/sec_edgar_scraper.md`](sec_edgar_scraper.md) The same pass **writes** each table's 5 % holders that no 13D/G covers (`filing_type` DEF 14A / 10-K/A) — for every company it reads, not only the scrape targets, and independent of the order the scrapes ran in (findings item 113). |
| `close-held-by-subsidiary` | End every Schedule 13D/G holding whose holder the issuer's own subsidiary list (Exhibit 21 / 8.1) or GLEIF's record names as its subsidiary, filed before that statement — `until_reason` `holder_is_subsidiary`, the list's date, an upper bound (findings item 99 part 3). Graph only, no EDGAR read; rebuild step 7 |
| `mark-stale` | Mark community-tier OWNS edges unconfirmed for `--days` (180) as **stale** — dimmed in the UI, never deleted or closed. Register-tier edges and register-vouched pairs are exempt; the pass clears as well as sets. A second pass dims a Schedule 13D/G holding whose last statement is older than its restatement window (a 13G filed before 2024: 15 months; a 13D, and a 13G filed since 2024 — amended only on a material change under the 2024 rules: 5 years; `not_restated`) and clears it when a newer filing refreshed the edge. See *Source tiers* in [`docs/data-model.md`](data-model.md) |
| `prune-analytics` | Delete usage counters untouched inside the retention window (`--days`, default 365; `--dry-run` first). **Scheduled** — `~/scripts/cron-prune-analytics.sh` runs it daily at 05:15, because the privacy notice tells people those rows are deleted after twelve months and that is only true if something deletes them. Shortening `--days` means changing the published notice too. |
| `backup-database` | Take a consistent **online** backup — ArcadeDB's own `BACKUP DATABASE`, not a disk snapshot (pages and the WAL can be captured out of step). The **server** picks the path, so this only reports the filename; `~/scripts/backup-database.sh` wraps it with verification, rotation and an offsite copy. See [`docs/operations.md`](operations.md) for the restore procedure. |
| `sec-holdings` | Ingest the >5% stakes one SEC filer discloses in others: `python manage.py sec-holdings 0002100119 [--limit N] [--succeeds OLD_CIK]`. The mirror of a normal scrape, which reads filings *about* a company — an asset manager has none of those. Keyed on CIK because the live filer often isn't the one you'd search by name. `--succeeds` records a handover between filer entities as a `SUCCEEDED_BY` edge. |
