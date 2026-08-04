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
    "SECRET_KEY":                    "test-secret-key-not-for-production-use-only-tests",
}
for k, v in _TEST_ENV.items():
    os.environ.setdefault(k, v)

# Never send real email from the test suite. Some tests exercise the register /
# reset paths without stubbing the sender, so with real SMTP creds in a local
# .env the suite would actually send verification mail to the fake test addresses
# (new@example.com, first@example.com, …) and get bounced. Force the console backend here —
# an env var beats the .env file, so this overrides any real SMTP config.
os.environ["EMAIL_BACKEND"] = "console"
os.environ["SMTP_HOST"] = ""

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
    """Factory: make_token(role='admin', sub='u1', email='a@example.com') -> JWT string."""
    from app.auth.security import create_access_token

    def _make(role="viewer", sub="user-1", email="user@example.com"):
        return create_access_token({"sub": sub, "email": email, "role": role})

    return _make


@pytest.fixture(autouse=True)
def _mock_rate_limit(monkeypatch):
    """Replace the DB-backed rate-limit functions with a pure in-memory implementation.

    Patches ``check_rate_limit``, ``record_attempt``, and ``clear_attempts`` at
    the call site (``app.auth.router``) so unit tests never touch ArcadeDB.
    Each test gets a fresh store that resets automatically between tests.

    The ``test_rate_limit.py`` tests bypass this fixture — they import and test
    ``app.auth.rate_limit`` functions directly, using their own ``run_sql`` mock.
    """
    import time as _time
    from fastapi import HTTPException
    from app.auth import router as _auth_router
    from app.auth.rate_limit import _LOCKOUT_DURATIONS

    # timestamps: key → list[float]
    # lockouts:   key → (lockout_until: float, lockout_count: int)
    _timestamps: dict[str, list] = {}
    _lockouts:   dict[str, tuple] = {}

    def _check(key: str, limit: int, window: int) -> None:
        now = _time.time()
        lu, lc = _lockouts.get(key, (0.0, 0))
        if lu > now:
            raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
        ts = [t for t in _timestamps.get(key, []) if t > now - window]
        if len(ts) >= limit:
            dur = _LOCKOUT_DURATIONS[min(lc, len(_LOCKOUT_DURATIONS) - 1)]
            _lockouts[key] = (now + dur, lc + 1)
            _timestamps[key] = ts
            raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    def _record(key: str, window: int) -> None:
        now = _time.time()
        ts = [t for t in _timestamps.get(key, []) if t > now - window]
        ts.append(now)
        _timestamps[key] = ts

    def _clear(key: str) -> None:
        _timestamps.pop(key, None)
        _lockouts.pop(key, None)

    monkeypatch.setattr(_auth_router, "check_rate_limit", _check)
    monkeypatch.setattr(_auth_router, "record_attempt",   _record)
    monkeypatch.setattr(_auth_router, "clear_attempts",   _clear)
