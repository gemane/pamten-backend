# Operations

Running the database day to day: backups, getting one back, and the service
account a rebuild takes with it.

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
change since the previous stored digest. `--email` sends it to `REPORT_EMAIL`
(falling back to `ADMIN_EMAIL`) through the configured email backend. Every run
stores the digest (`WeeklyReport`, one per week) so the next can measure growth.
Aggregates only: it names companies, never who searched.

Manual-first. To get it every Monday at 06:00 UTC:

```
0 6 * * 1 /home/administrator/scripts/cron-weekly-report.sh
```

The wrapper takes a lock and logs to `/home/administrator/data/weekly-report.log`.
The same numbers are on the Scraper tab (admins) and at `GET /analytics/weekly`.

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
