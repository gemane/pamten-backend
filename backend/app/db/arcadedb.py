"""
ArcadeDB HTTP client.

A single httpx.Client is shared across all queries so TCP (and TLS)
connections are pooled and kept alive. Previously every query opened and
closed its own connection, which was very costly inside the bulk-import
loops that issue thousands of queries. httpx.Client is safe for concurrent
use across threads, which is how FastAPI runs sync endpoints.

Environment variables
---------------------
ARCADEDB_URL       – base URL of the ArcadeDB server  (e.g. http://localhost:2480)
ARCADEDB_USERNAME  – database user
ARCADEDB_PASSWORD  – database password
ARCADEDB_DATABASE  – database name
"""
import threading
import httpx
from app.config import settings

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    """Lazily build the shared, pooled client (double-checked locking)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    auth=(settings.ARCADEDB_USERNAME, settings.ARCADEDB_PASSWORD),
                    timeout=_TIMEOUT,
                    limits=httpx.Limits(
                        max_keepalive_connections=5,
                        max_connections=10,
                        keepalive_expiry=30.0,
                    ),
                )
    return _client


def close_client() -> None:
    """Close the pooled client (call on application shutdown)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _post(endpoint: str, statement: str, params: dict, language: str = "cypher") -> list[dict]:
    url  = f"{settings.ARCADEDB_URL}/api/v1/{endpoint}/{settings.ARCADEDB_DATABASE}"
    body = {"language": language, "command": statement, "params": params}
    try:
        resp = _get_client().post(url, json=body)
    except httpx.RequestError as exc:
        raise ConnectionError(f"ArcadeDB unreachable: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"ArcadeDB {endpoint} failed [{resp.status_code}]: {resp.text[:400]}"
        )
    return resp.json().get("result", [])


def run_query(cypher: str, params: dict | None = None) -> list[dict]:
    """Execute a read-only Cypher query against /api/v1/query/{db}."""
    return _post("query", cypher, params or {})


def run_command(cypher: str, params: dict | None = None) -> list[dict]:
    """Execute a write Cypher command against /api/v1/command/{db}."""
    return _post("command", cypher, params or {})


def run_sql(command: str, params: dict | None = None) -> list[dict]:
    """Execute an ArcadeDB SQL command — used for schema DDL (Cypher can't)."""
    return _post("command", command, params or {}, language="sql")
