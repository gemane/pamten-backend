# Integration tests (real ArcadeDB)

The main test suite fakes the database at the `db.get_session()` seam — fast, but
blind to two classes of bug that only appear against a real ArcadeDB:

1. **Cypher dialect.** ArcadeDB's Cypher engine rejects constructs Neo4j accepts
   (list literals, list indexing, `UNWIND`, `COALESCE`, …).
2. **Result shape.** The real `_Record` has no `keys()`, so `dict(rec)` on a whole
   row raises — you must read columns with `rec["x"]` / `rec.get("x")`.

Both of these shipped to production once (the `/sources/entity` provenance
endpoint 500'd on each) because the mocked tests couldn't see them. These tests
run the real read/write Cypher end-to-end, so they catch that class of bug.

> The faithful mocked fixture (`tests/conftest.py` wraps rows in the real
> `_Record`) now catches bug #2 in the fast suite too. These integration tests
> are what catch **#1**.

## Run

Start a throwaway ArcadeDB with Docker:

```bash
docker run -d --rm --name arcadedb-it -p 2480:2480 \
    -e JAVA_OPTS="-Darcadedb.server.rootPassword=playwithdata" \
    arcadedata/arcadedb:latest
```

Point a **dedicated** env at it (separate from `ARCADEDB_*` so these
create/drop-database tests can never hit production), then run:

```bash
export ARCADEDB_IT_URL=http://localhost:2480
export ARCADEDB_IT_USERNAME=root
export ARCADEDB_IT_PASSWORD=playwithdata

pytest tests/integration -v
```

Each test gets a fresh, isolated database (`pamten_it_<random>`) that is dropped
on teardown.

## Default behaviour

Without `ARCADEDB_IT_URL`, every test here is **skipped**, so `pytest` and CI stay
green with no ArcadeDB required. Wire the Docker step above into CI to run them
on every PR.
