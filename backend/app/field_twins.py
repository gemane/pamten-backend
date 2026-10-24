"""One fact, one field — the helpers and heals for the twins the model carried.

A scalar beside its list (`nationality` / `nationalities`, `country` /
`countries`) is one fact stored twice, and two writers each fill the half
they know: on 2026-10-25 all 371 dev persons carried both fields, 204 of them
a filled scalar beside an EMPTY list (the PSC and SEC writers set the
scalar, the Wikidata path the list), and 17 entities a filled `country`
beside `countries: []`. The panel reads the list first, so those persons
showed no nationality at all. The same disease as `founded` /
`founded_date` (see `app.incorporation`).

The rule now: `nationalities` is the field, the scalar is gone; `countries`,
when written, always contains `country`. `merge_*` enforce it at every
writer, `heal_*` bring an existing database to it. Two more gaps found by the
same survey live here too: `OWNS.stale` was unset on 1,274 active edges (a
flag is true or false — since ArcadeDB 26.10.1 a `<>` on null is FALSE, so an
unset flag silently drops out of every filter), and the PSC person writer
built its props without `source_id` (7 persons).

Whole-type scans for the edge flag: run with `--db-url` straight at ArcadeDB.
"""
from __future__ import annotations

import logging

from app.db.arcadedb import run_sql, run_sqlscript

log = logging.getLogger(__name__)

_CHUNK = 500
_EDGE_PAGE = 2000


def merge_nationalities(nationalities: list[str] | None, nationality: str | None) -> list[str]:
    """The list with the single value folded in (first when the list was empty,
    appended when it is new), blanks dropped, order kept."""
    out: list[str] = []
    for v in [*(nationalities or []), nationality]:
        v = (v or "").strip() if isinstance(v, str) else v
        if v and v not in out:
            out.append(v)
    return out


def merge_countries(countries: list[str] | None, country: str | None) -> list[str]:
    """`countries` with `country` guaranteed in it — the same fold."""
    return merge_nationalities(countries, country)


def _walk(label: str, columns: str):
    from app.db import paging
    yield from paging.iter_id_pages(label, columns=f"id, @rid AS rid, {columns}")


def _apply_rows(rows: list[tuple[str, str, dict]]) -> None:
    """[(rid, set_clause_with_named_params, params)] in one script per chunk;
    the parameter names are suffixed per row by the caller."""
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i:i + _CHUNK]
        script = "\n".join(f"UPDATE {rid} {clause};" for rid, clause, _ in chunk)
        params = {}
        for _, _, p in chunk:
            params.update(p)
        run_sqlscript(script, params)


def heal_nationalities(apply: bool = True) -> dict:
    """Fold every Person's scalar `nationality` into `nationalities` and drop
    the scalar. A list that already has values keeps them (the scalar was
    derived from its first entry on the Wikidata path)."""
    out = {"scanned": 0, "folded": 0, "dropped": 0, "applied": bool(apply)}
    for page in _walk("Person", "nationality, nationalities"):
        out["scanned"] += len(page)
        rows = []
        for k, r in enumerate(page):
            if r.get("nationality") is None:  # absent (SQL projects it as null): already one field
                continue
            merged = merge_nationalities(r.get("nationalities"), r.get("nationality"))
            out["dropped"] += 1
            if merged != list(r.get("nationalities") or []):
                out["folded"] += 1
            rows.append((str(r["rid"]), f"SET nationalities = :n{k} REMOVE nationality", {f"n{k}": merged}))
        if apply and rows:
            _apply_rows(rows)
    log.info("nationalities heal: %s", out)
    return out


def heal_countries(apply: bool = True) -> dict:
    """Put `country` into every Entity's `countries` list that exists without it."""
    out = {"scanned": 0, "fixed": 0, "applied": bool(apply)}
    for page in _walk("Entity", "country, countries"):
        out["scanned"] += len(page)
        rows = []
        for k, r in enumerate(page):
            lst, c = r.get("countries"), r.get("country")
            if lst is None or not c or c in lst:
                continue
            out["fixed"] += 1
            rows.append((str(r["rid"]), f"SET countries = :c{k}", {f"c{k}": merge_countries(lst, c)}))
        if apply and rows:
            _apply_rows(rows)
    log.info("countries heal: %s", out)
    return out


def heal_person_sources(apply: bool = True) -> dict:
    """Stamp `source_id` on the PSC persons written without one: their ids are
    `chpsc:` and the register is the only writer of that prefix."""
    from app.scraper.sources import SOURCE_ID
    sid = SOURCE_ID["bods_uk_psc"]
    out = {"scanned": 0, "stamped": 0, "source_id": sid, "applied": bool(apply)}
    for page in _walk("Person", "source_id"):
        out["scanned"] += len(page)
        rids = [str(r["rid"]) for r in page
                if not r.get("source_id") and str(r.get("id") or "").startswith("chpsc:")]
        out["stamped"] += len(rids)
        if apply and rids:
            for i in range(0, len(rids), _CHUNK):
                run_sql(f"UPDATE [{', '.join(rids[i:i + _CHUNK])}] SET source_id = :sid", {"sid": sid})
    log.info("person sources heal: %s", out)
    return out


def heal_stale_flags(apply: bool = True) -> dict:
    """`stale = false` on every OWNS edge where the flag is unset. A whole-type
    scan paged by LIMIT; the WHERE excludes what a pass has done, so it ends."""
    out = {"fixed": 0, "applied": bool(apply)}
    if not apply:
        rows = run_sql("SELECT count(*) AS n FROM OWNS WHERE stale IS NULL", timeout=3600)
        out["fixed"] = int((rows or [{}])[0].get("n") or 0)
        return out
    while True:
        rows = run_sql(f"SELECT @rid AS rid FROM OWNS WHERE stale IS NULL LIMIT {_EDGE_PAGE}", timeout=3600)
        rids = [str(r["rid"]) for r in rows or [] if r.get("rid")]
        if not rids:
            break
        for i in range(0, len(rids), _CHUNK):
            run_sql(f"UPDATE [{', '.join(rids[i:i + _CHUNK])}] SET stale = false")
        out["fixed"] += len(rids)
    log.info("stale flags heal: %s", out)
    return out


def heal_all(apply: bool = True) -> dict:
    return {"nationalities": heal_nationalities(apply), "countries": heal_countries(apply),
            "person_sources": heal_person_sources(apply), "stale_flags": heal_stale_flags(apply)}
