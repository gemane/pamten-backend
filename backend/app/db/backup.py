"""Taking a consistent backup of the database.

Why not just snapshot the disk: ArcadeDB writes pages and a WAL, and a
filesystem or VM snapshot taken mid-write can capture them out of step with each
other. It usually restores. The time it does not is the time you needed it.
`BACKUP DATABASE` is the server's own online backup — it produces an archive
that is consistent as of one moment, with the server still serving.

Two things about it that shape this module, both established against a real
ArcadeDB 26.7.3 rather than read from the docs:

1. **The server chooses where the file goes.** `BACKUP DATABASE <path>` is
   rejected outright ("Backup file cannot contain path change because the
   directory is specified"), so the destination is the server's
   `arcadedb.server.backupDirectory` (default `<rootPath>/backups/<database>/`)
   and the only thing the caller learns is the filename. In production, where the
   database runs in a container next to the API, that directory is the one to
   mount as a volume.

2. **Restoring is a server-side, offline operation** — `bin/restore.sh -f
   <archive> -d databases/<name>` writes a new database directory, which the
   server picks up. So a backup is not restorable by this API, and no code here
   pretends otherwise; see docs/operations.md for the procedure.
"""
import logging
from app.config import settings
from app.db.arcadedb import run_sql

log = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """The server refused or failed the backup."""


def backup_database() -> dict:
    """Take an online backup of the connected database.

    Returns ``{"database": …, "file": …}`` — `file` being a bare filename inside
    the server's backup directory, since that is all the server discloses.

    Raises BackupError rather than returning a falsy result: a backup that did
    not happen must never be mistaken for one that did, and the caller is a cron
    job whose only signal is the exit code.
    """
    db = settings.ARCADEDB_DATABASE
    rows = run_sql("BACKUP DATABASE")
    row = rows[0] if rows else {}

    # The shape is {"operation": "backup database", "result": "OK",
    # "backupFile": "<db>-backup-<timestamp>.zip"}. Anything else is a failure we
    # have not seen before, and guessing at it would be worse than stopping.
    if str(row.get("result", "")).upper() != "OK" or not row.get("backupFile"):
        raise BackupError(f"unexpected response from BACKUP DATABASE: {rows!r}")

    log.info("Backed up %s to %s", db, row["backupFile"])
    return {"database": db, "file": row["backupFile"]}
