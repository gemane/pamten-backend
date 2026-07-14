"""
Integration-test support: run selected tests against a REAL ArcadeDB.

The rest of the suite fakes the DB at the db.get_session() seam, which is fast
but cannot catch two whole classes of bug that only surface against a real
ArcadeDB:

  1. Cypher dialect — ArcadeDB's Cypher engine rejects constructs Neo4j accepts
     (list literals, list indexing, UNWIND, COALESCE, ...).
  2. Result shape — the real _Record type has no keys(), so dict(rec) on a whole
     row raises; only reading columns via rec["x"] / rec.get("x") works.

Both shipped to production once (the /sources/entity provenance endpoint 500'd
on both) precisely because the mocked tests couldn't see them. These tests run
the real read/write Cypher end-to-end.

## How to run

Start a throwaway ArcadeDB (Docker), point the env at it, then run:

    docker run -d --rm --name arcadedb-it -p 2480:2480 \
        -e JAVA_OPTS="-Darcadedb.server.rootPassword=playwithdata" \
        arcadedata/arcadedb:latest

    export ARCADEDB_IT_URL=http://localhost:2480
    export ARCADEDB_IT_USERNAME=root
    export ARCADEDB_IT_PASSWORD=playwithdata

    pytest tests/integration -v

Without ARCADEDB_IT_URL set, every test here is SKIPPED, so the default
`pytest` run and CI stay green with no ArcadeDB required. A dedicated
ARCADEDB_IT_* env (separate from ARCADEDB_*) makes it impossible to point these
create/drop-database tests at production by accident.
"""
import os
import uuid

import httpx
import pytest


def _server_command(url: str, auth: tuple[str, str], command: str) -> None:
    """Run a server-level command (create/drop database) via the ArcadeDB API."""
    resp = httpx.post(
        f"{url.rstrip('/')}/api/v1/server",
        json={"command": command},
        auth=auth,
        timeout=30.0,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"server command failed [{resp.status_code}]: {resp.text[:300]}")


@pytest.fixture
def it_db(monkeypatch):
    """
    Yield with the app's DB layer pointed at a fresh, isolated ArcadeDB database
    (created for this test and dropped afterwards). Skips when ARCADEDB_IT_URL is
    not configured.
    """
    url = os.environ.get("ARCADEDB_IT_URL")
    if not url:
        pytest.skip("ARCADEDB_IT_URL not set — skipping real-ArcadeDB integration tests")
    user = os.environ.get("ARCADEDB_IT_USERNAME", "root")
    pw   = os.environ.get("ARCADEDB_IT_PASSWORD")
    if not pw:
        pytest.skip("ARCADEDB_IT_PASSWORD not set — skipping real-ArcadeDB integration tests")

    auth = (user, pw)
    dbname = f"pamten_it_{uuid.uuid4().hex[:8]}"

    from app.config import settings
    from app.db import arcadedb
    from app.db.schema import ensure_indexes

    _server_command(url, auth, f"create database {dbname}")

    # Point the app's DB layer at the fresh database. arcadedb._post reads
    # settings.ARCADEDB_URL/DATABASE per call; the pooled client captures the
    # credentials, so reset it after changing them.
    monkeypatch.setattr(settings, "ARCADEDB_URL", url)
    monkeypatch.setattr(settings, "ARCADEDB_USERNAME", user)
    monkeypatch.setattr(settings, "ARCADEDB_PASSWORD", pw)
    monkeypatch.setattr(settings, "ARCADEDB_DATABASE", dbname)
    arcadedb.close_client()

    try:
        ensure_indexes()  # vertex types + id indexes (Entity, Source, Person, ...)
        # Edge types aren't part of the app's index bootstrap — create them here.
        for etype in ("OWNS", "HAS_ROLE", "DUAL_LISTED_WITH"):
            arcadedb.run_sql(f"CREATE EDGE TYPE {etype} IF NOT EXISTS")
        yield arcadedb
    finally:
        arcadedb.close_client()
        try:
            _server_command(url, auth, f"drop database {dbname}")
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
