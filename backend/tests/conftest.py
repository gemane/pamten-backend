"""
Shared pytest fixtures.

Env vars are set at module level (before any app import) so that
Settings() can initialise without a real .env file.
The autouse fixture re-applies them per-test via monkeypatch so that
individual tests can safely override them.
"""

import os

# Set at module level — these run before any test-file import
_TEST_ENV = {
    "ARCADEDB_URL":                  "http://localhost:2480",
    "ARCADEDB_USERNAME":             "test",
    "ARCADEDB_PASSWORD":             "test",
    "ARCADEDB_DATABASE":             "test",
    "SCRAPER_ENABLED":               "true",
    "SCRAPER_SEC_EDGAR_ENABLED":     "true",
    "SCRAPER_OPENCORPORATES_ENABLED":"true",
    "SCRAPER_BODS_GLEIF_ENABLED":    "true",
    "SCRAPER_BODS_UK_PSC_ENABLED":   "true",
    "OPENCORPORATES_API_KEY":        "",
    "SECRET_KEY":                    "test-secret",
}
for k, v in _TEST_ENV.items():
    os.environ.setdefault(k, v)

import pytest  # noqa: E402  (env vars above must be set before app imports)
from contextlib import contextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402


@pytest.fixture(autouse=True)
def scraper_env(monkeypatch):
    """Re-apply test env vars per-test so individual tests can override them."""
    for k, v in _TEST_ENV.items():
        monkeypatch.setenv(k, v)


# ── Router / auth test support ─────────────────────────────────────────────────
#
# These fixtures let the API be tested end-to-end (real auth, real security,
# real request validation) while the ArcadeDB layer is faked at the
# db.get_session() seam.
#
# Queued rows are wrapped in the SAME _Record type the real ArcadeDB layer
# returns, so mocked tests exercise the production record interface — e.g.
# dict(rec) on a whole row raises here just like it does against ArcadeDB
# (_Record has no keys()). Routers must use rec["x"] / rec.get("x").

class _FakeResult:
    def __init__(self, rows):
        # Import lazily: env vars are set at the top of this module before any
        # app import, so importing app.database here is safe.
        from app.database import _Record
        self._rows = [_Record(r) if isinstance(r, dict) else r for r in rows]

    def single(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Returns queued results from successive .run() calls, in order."""

    def __init__(self):
        self._queue = []
        self.calls = []  # list of (cypher, params) for assertions

    def queue(self, *results):
        for r in results:
            self._queue.append(r if isinstance(r, _FakeResult) else _FakeResult(r))
        return self

    def run(self, cypher, **params):
        self.calls.append((cypher, params))
        return self._queue.pop(0) if self._queue else _FakeResult([])


@pytest.fixture
def fake_db():
    """Patch db.get_session to yield a controllable fake session."""
    from app.database import db

    session = _FakeSession()

    @contextmanager
    def _get_session():
        yield session

    with patch.object(db, "get_session", _get_session):
        yield session


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture
def make_token():
    """Factory: make_token(role='admin', sub='u1', email='a@x.com') -> JWT string."""
    from app.auth.security import create_access_token

    def _make(role="viewer", sub="user-1", email="user@example.com"):
        return create_access_token({"sub": sub, "email": email, "role": role})

    return _make


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    """Clear the in-memory login rate-limit state between tests."""
    from app.auth import router as auth_router
    with auth_router._login_attempts_lock:
        auth_router._login_attempts.clear()
    yield
    with auth_router._login_attempts_lock:
        auth_router._login_attempts.clear()
