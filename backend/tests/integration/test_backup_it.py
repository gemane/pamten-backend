"""Backing up a real ArcadeDB.

The mocked tests pin our handling of the response. They cannot tell us that
`BACKUP DATABASE` exists, is spelled that way, or returns what we expect — and
that is exactly the kind of thing this codebase has been wrong about before. So
this runs the statement against a real server and reads the archive back.

What it deliberately does NOT test is restoring, because restoring is not an API
operation: it is `bin/restore.sh -f <archive> -d databases/<name>` on the server
host, writing a new database directory. Verified by hand on 26.7.3 (backup →
restore → the row was there); the procedure is in docs/operations.md and belongs
in a restore drill, not a test suite that has no shell on the server.
"""
import pytest

from app.db.backup import backup_database

pytestmark = pytest.mark.integration


def test_the_server_takes_a_backup(it_db):
    it_db.run_command("CREATE (:Entity {id:'e1', name:'Acme Holdings', type:'company'})")

    res = backup_database()

    # A .zip named after the database, which is how the wrapper script finds it
    # on disk afterwards.
    assert res["file"].endswith(".zip")
    assert res["database"] in res["file"]


def test_two_backups_do_not_collide(it_db):
    """The filename carries a timestamp, so a second run in the same session must
    not overwrite the first — an hourly cron would otherwise keep one copy."""
    it_db.run_command("CREATE (:Entity {id:'e1', name:'Acme Holdings', type:'company'})")

    first = backup_database()["file"]
    it_db.run_command("CREATE (:Entity {id:'e2', name:'Beta Corp', type:'company'})")
    second = backup_database()["file"]

    assert first != second


def test_it_works_on_a_database_with_data_in_it(it_db):
    # Cheap, but it is the case that matters: an empty database backs up whatever
    # the server does with it, and would hide a failure that only appears once
    # there are buckets and indexes to walk.
    for i in range(50):
        it_db.run_command(f"CREATE (:Entity {{id:'e{i}', name:'Company {i}', type:'company'}})")
    assert backup_database()["file"].endswith(".zip")
