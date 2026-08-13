"""Taking a backup, and refusing to claim one when it did not happen.

The failure this guards against is the quiet one: a cron job that exits 0 every
night for a year, and a backup directory nobody looks in. So the only thing worth
asserting hard is that anything other than an explicit OK from the server raises.
"""
import pytest

from app.db.backup import backup_database, BackupError

OK = [{"operation": "backup database", "result": "OK",
       "backupFile": "owlgraph-backup-20260813-143715208.zip"}]


@pytest.fixture
def server(monkeypatch):
    """Stand in for the ArcadeDB server, recording what it was asked."""
    calls: list[str] = []
    reply: list = [OK]

    def fake_run_sql(sql, params=None):
        calls.append(sql)
        result = reply[0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("app.db.backup.run_sql", fake_run_sql)
    return {"calls": calls, "reply": reply}


class TestTakingOne:
    def test_asks_the_server_for_an_online_backup(self, server):
        backup_database()
        assert server["calls"] == ["BACKUP DATABASE"]

    def test_reports_the_filename_the_server_chose(self, server):
        # The caller cannot pick a path — ArcadeDB rejects `BACKUP DATABASE <path>`
        # outright — so the filename coming back is the only handle on the archive.
        assert backup_database()["file"] == "owlgraph-backup-20260813-143715208.zip"

    def test_reports_which_database(self, server, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ARCADEDB_DATABASE", "owlgraph")
        assert backup_database()["database"] == "owlgraph"


class TestRefusingToClaimOne:
    """Every one of these would otherwise be a green cron job and no backup."""

    def test_a_non_OK_result_raises(self, server):
        server["reply"][0] = [{"operation": "backup database", "result": "FAILED"}]
        with pytest.raises(BackupError):
            backup_database()

    def test_a_missing_filename_raises(self, server):
        # OK with nothing to point at is not a backup anyone can restore.
        server["reply"][0] = [{"operation": "backup database", "result": "OK"}]
        with pytest.raises(BackupError):
            backup_database()

    def test_an_empty_response_raises(self, server):
        server["reply"][0] = []
        with pytest.raises(BackupError):
            backup_database()

    def test_an_unrecognised_shape_raises(self, server):
        server["reply"][0] = [{"something": "else"}]
        with pytest.raises(BackupError):
            backup_database()

    def test_a_transport_failure_propagates(self, server):
        # Not swallowed into a falsy return: the exit code is the cron job's
        # entire feedback channel.
        server["reply"][0] = RuntimeError("connection refused")
        with pytest.raises(RuntimeError):
            backup_database()
