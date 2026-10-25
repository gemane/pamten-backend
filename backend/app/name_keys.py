"""The stored name key (`Entity.name_normalized`) kept in step with the
normaliser.

`mapper.normalize_entity_name` is what every by-name lookup and the same-name
dedup scan key on, and the key is STORED on the node at write time. When the
normaliser learns something — 2026-10-25: a run of spaced single letters is an
abbreviation, so "GRUPO TELEVISA S A B" keys like "Grupo Televisa, S.A.B." —
every node written before that carries the old key, and the two Televisas
stay two companies until their keys are recomputed. This heal recomputes them.
"""
from __future__ import annotations

import logging

from app.db.arcadedb import run_sqlscript
from app.scraper.mapper import normalize_entity_name

log = logging.getLogger(__name__)

_CHUNK = 500


def heal_name_keys(apply: bool = False) -> dict:
    """Recompute `name_normalized` from `name` on every Entity whose stored
    key differs from what the normaliser says today. Dry run unless `apply`.
    Returns the scan and change counts, the changed (name, old, new) rows
    (first 50), and the keys that two or more entities now share for the
    first time — the duplicates the old key hid, to merge by hand."""
    from app.db import paging
    out = {"scanned": 0, "changed": 0, "sample": [], "newly_shared": {}, "applied": bool(apply)}
    keys_before: dict[tuple, int] = {}
    keys_after: dict[tuple, list] = {}
    for page in paging.iter_id_pages("Entity", columns="id, @rid AS rid, name, name_normalized, country"):
        out["scanned"] += len(page)
        rows = []
        for k, r in enumerate(page):
            name = r.get("name")
            if not name:
                continue
            old, new = r.get("name_normalized"), normalize_entity_name(name)
            keys_before[(old, r.get("country"))] = keys_before.get((old, r.get("country")), 0) + 1
            keys_after.setdefault((new, r.get("country")), []).append(name)
            if new == old:
                continue
            out["changed"] += 1
            if len(out["sample"]) < 50:
                out["sample"].append((name, old, new))
            rows.append((str(r["rid"]), f"SET name_normalized = :n{k}", {f"n{k}": new}))
        if apply and rows:
            _apply_rows(rows)
    for key, names in keys_after.items():
        if len(names) > 1 and keys_before.get(key, 0) < len(names):
            out["newly_shared"][f"{key[0]} | {key[1]}"] = sorted(names)
    log.info("name keys heal: scanned %d, changed %d, newly shared %d",
             out["scanned"], out["changed"], len(out["newly_shared"]))
    return out


def _apply_rows(rows: list[tuple[str, str, dict]]) -> None:
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i:i + _CHUNK]
        script = "\n".join(f"UPDATE {rid} {clause};" for rid, clause, _ in chunk)
        params: dict = {}
        for _, _, p in chunk:
            params.update(p)
        run_sqlscript(script, params)
