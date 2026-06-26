"""
ArcadeDB HTTP client.

Environment variables
---------------------
ARCADEDB_URL       – base URL of the ArcadeDB server  (e.g. http://localhost:2480)
ARCADEDB_USERNAME  – database user
ARCADEDB_PASSWORD  – database password
ARCADEDB_DATABASE  – database name
"""
import os
import httpx

_TIMEOUT = 30.0


def _post(endpoint: str, cypher: str, params: dict) -> list[dict]:
    url  = os.getenv("ARCADEDB_URL", "http://localhost:2480")
    user = os.getenv("ARCADEDB_USERNAME", "root")
    pw   = os.getenv("ARCADEDB_PASSWORD", "")
    db   = os.getenv("ARCADEDB_DATABASE", "pamten")
    url  = f"{url}/api/v1/{endpoint}/{db}"
    body = {"language": "cypher", "command": cypher, "params": params}
    try:
        with httpx.Client(auth=(user, pw), timeout=_TIMEOUT) as client:
            resp = client.post(url, json=body)
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
