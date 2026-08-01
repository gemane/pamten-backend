"""
Real-ArcadeDB test for the cross-process import lock (app/db/import_lock.py): only
one import at a time, atomic via the UNIQUE ImportState.key, with stale-lock steal
and an IMPORT_ORCHESTRATED no-op for the sub-steps of a chained full import.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


def test_acquire_blocks_release_and_status(it_db):
    from app.db import import_lock as L

    assert L.status() == {"held": False}

    L.acquire("full-import")
    st = L.status()
    assert st["held"] is True and st["holder"].startswith("full-import@") and st["stale"] is False

    with pytest.raises(L.ImportLocked, match="another import holds the lock"):
        L.acquire("gleif-update")           # a second, different import is refused

    L.release()
    assert L.status() == {"held": False}


def test_context_manager_and_orchestrated_noop(it_db, monkeypatch):
    from app.db import import_lock as L

    # standalone: the context manager acquires for the block and releases after
    with L.import_lock("standalone"):
        assert L.status()["held"] is True
    assert L.status()["held"] is False

    # under IMPORT_ORCHESTRATED the per-step lock is a no-op (the orchestrator holds it)
    L.acquire("full-import")
    monkeypatch.setenv("IMPORT_ORCHESTRATED", "1")
    with L.import_lock("sub-step"):
        pass
    assert L.status()["held"] is True       # still held by full-import, not touched
    monkeypatch.delenv("IMPORT_ORCHESTRATED")
    L.release()


def test_stale_lock_is_stolen(it_db):
    from app.db import import_lock as L
    from app.db.arcadedb import run_sql

    old = (datetime.now(timezone.utc) - timedelta(seconds=L.STALE_AFTER_SEC + 60)).isoformat()
    run_sql("INSERT INTO ImportState SET key='import-lock', holder='crashed', acquired_at=:t", {"t": old})
    assert L.status()["stale"] is True

    L.acquire("gleif-update")               # stale lock stolen, no exception
    assert L.status()["holder"].startswith("gleif-update@")
    L.release()
