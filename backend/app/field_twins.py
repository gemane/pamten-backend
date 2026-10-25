"""One fact, one field — the helpers and heals for the twins the model carried.

A scalar beside its list (`nationality` / `nationalities`, `country` /
`countries`) is one fact stored twice, and two writers each fill the half
they know: on 2026-10-25 all 371 dev persons carried both fields, 204 of them
an EMPTY list beside the scalar — 7 (the PSC writer's) with the nationality
only in the scalar, 197 (the SEC writer's) with an empty string there, so no
nationality in either field — while the Wikidata path filled the list and
copied its first entry. The panel fell back to the scalar, so nothing was
lost on screen; what was wrong is two fields for one fact, each writer
choosing one. 17 entities had a filled `country` beside `countries: []`.
The same disease as `founded` / `founded_date` (see `app.incorporation`).

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


#: The lists that are absent when the source stated nothing — never `[]`,
#: as a scalar is never `''`. `alias`/`aliases` stay lists: search and the
#: person merge read them unconditionally.
EMPTY_LISTS: tuple[tuple[str, str], ...] = (("Person", "nationalities"), ("Entity", "countries"))


def heal_empty_lists(apply: bool = True) -> dict:
    """Remove every `nationalities` / `countries` that is an empty list (196
    persons and 4 entities on dev, 2026-10-25): the writers now leave the
    property absent when no source named a country. `.size()` reads 0 on an
    absent property too, so the filter says IS NOT NULL first. A whole-type
    scan per list — run with --db-url."""
    out: dict = {"applied": bool(apply)}
    for label, col in EMPTY_LISTS:
        where = f"{col} IS NOT NULL AND {col}.size() = 0"
        rows = run_sql(f"SELECT count(*) AS n FROM {label} WHERE {where}", timeout=3600)
        n = int((rows or [{}])[0].get("n") or 0)
        out[f"{label}.{col}"] = n
        if apply and n:
            run_sql(f"UPDATE {label} REMOVE {col} WHERE {where}", timeout=3600)
    log.info("empty lists heal: %s", out)
    return out


#: Properties the ledger dropped on 2026-10-25 because no writer ever set
#: them — a merge recreates an edge with every schema key, null included, so
#: the dead key sits on the rows it touched: `OWNS.file_date` (the runner
#: passed it, the writer stored it as `source_date`), `Person.updated_at`
#: (nothing wrote it), `RELATED_TO.credibility_score` (typed, never set).
DEAD_KEYS: tuple[tuple[str, str], ...] = (
    ("OWNS", "file_date"), ("Person", "updated_at"), ("RELATED_TO", "credibility_score"))


def heal_dead_keys(apply: bool = True) -> dict:
    """Remove every key of `DEAD_KEYS` from the rows that carry it — null or
    not, a dropped declaration has nothing to say. A whole-type scan per
    key — run with --db-url."""
    out: dict = {"applied": bool(apply)}
    for label, col in DEAD_KEYS:
        where = f"{col} IS NOT NULL"
        rows = run_sql(f"SELECT count(*) AS n FROM {label} WHERE {where}", timeout=3600)
        out[f"{label}.{col}"] = int((rows or [{}])[0].get("n") or 0)
        if apply:
            run_sql(f"UPDATE {label} REMOVE {col}", timeout=3600)
    log.info("dead keys heal: %s", out)
    return out


#: The claim rows a knowledge-base role write left undated (`_upsert_role`
#: passed no `source_date` until 2026-10-25; 290 on dev): dated by the day
#: they were first seen, the closest thing to the day the source said it.
UNDATED_CLAIMS_WHERE = "kind = 'role' AND source_date IS NULL AND first_seen_at IS NOT NULL"


def heal_claim_dates(apply: bool = True) -> dict:
    """Give every undated role claim a `source_date`: the day of its
    `first_seen_at`. The owns claims were always dated this way."""
    rows = run_sql(f"SELECT count(*) AS n FROM Claim WHERE {UNDATED_CLAIMS_WHERE}", timeout=3600)
    out = {"applied": bool(apply), "claims": int((rows or [{}])[0].get("n") or 0)}
    if apply and out["claims"]:
        run_sql(f"UPDATE Claim SET source_date = first_seen_at.left(10) WHERE {UNDATED_CLAIMS_WHERE}",
                timeout=3600)
    log.info("claim dates heal: %s", out)
    return out


def heal_address_keys(apply: bool = True) -> dict:
    """`registered_address` → `address_normalized` for a database written
    before 2026-10-25. GLEIF filled the old field with a lowercased matching
    key, the Companies House importers with the readable address (and left
    `address` empty on the company-data rows): every entity with an address
    gets `address_normalized` = the key of its readable `address`; a readable
    value found only in the old field moves to `address` first; the old key
    is removed. Pages the id index; batched scripts; dry run unless `apply`."""
    from app.db.paging import iter_id_pages
    from app.scraper.mapper import normalize_address
    out = {"applied": bool(apply), "scanned": 0, "keyed": 0, "addresses_moved": 0, "removed": 0}
    for page in iter_id_pages("Entity", columns="id, @rid AS rid, address, registered_address, address_normalized"):
        out["scanned"] += len(page)
        stmts: list[str] = []
        params: dict = {}
        for k, r in enumerate(page):
            old = r.get("registered_address")
            readable = r.get("address") or (old if old and normalize_address(old) != old else None)
            sets: dict = {}
            if readable and not r.get("address"):
                sets["address"] = readable
                out["addresses_moved"] += 1
            key = normalize_address(readable or old)
            if key and key != r.get("address_normalized"):
                sets["address_normalized"] = key
                out["keyed"] += 1
            if old is not None:
                out["removed"] += 1
            if not sets and old is None:
                continue
            assigns = ", ".join(f"{f} = :{f}_{k}" for f in sets)
            params.update({f"{f}_{k}": v for f, v in sets.items()})
            stmts.append(f"UPDATE {r['rid']}" + (f" SET {assigns}" if assigns else "")
                         + (" REMOVE registered_address" if old is not None else ""))
        if apply and stmts:
            run_sqlscript(";".join(stmts), params)
    log.info("address keys heal: %s", out)
    return out


def heal_all(apply: bool = True) -> dict:
    """Every model-gap heal in one call (`manage.py heal-model-gaps`): the
    2026-10-25 survey's four, then the round-four three — empty lists, the run
    log's `''` placeholders, the SEC seat claims' filing type — then the
    round-five two: dead keys, undated role claims."""
    from app.roles import heal_seat_claim_filings
    from app.scraper.run_log import heal_placeholders
    return {"nationalities": heal_nationalities(apply), "countries": heal_countries(apply),
            "person_sources": heal_person_sources(apply), "stale_flags": heal_stale_flags(apply),
            "empty_lists": heal_empty_lists(apply), "run_log": heal_placeholders(apply),
            "seat_filings": heal_seat_claim_filings(apply),
            "dead_keys": heal_dead_keys(apply), "claim_dates": heal_claim_dates(apply),
            "address_keys": heal_address_keys(apply)}
