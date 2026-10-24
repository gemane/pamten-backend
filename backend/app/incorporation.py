"""`incorporated_on` — the legal entity's registration date — beside `founded`,
the year the business began. Two facts, two fields (since 2026-10-24).

Until then the registers wrote both into one pair: `founded_date` held the
incorporation date and `founded` its year, while Wikidata wrote the business's
inception year into `founded` too — and only ever moved it earlier. Barclays
PLC read `founded 1690` beside `founded_date 1896-07-20`; 22 dev entities
contradicted themselves under one name. Now GLEIF, Companies House and
OpenCorporates write `incorporated_on` only, and `founded` is Wikidata's.

`heal` is the one-off for a database written before: it renames the field on
every node that carries it and clears a `founded` that was nothing but the
incorporation year — recognisable because the node has no Wikidata id, so no
other source could have stated it. A node with a Wikidata id keeps its
`founded` even when it equals the incorporation year: that may be a real
statement, and clearing it would lose a fact to save a guess.
"""
from __future__ import annotations

import logging

from app.db.arcadedb import run_sqlscript

log = logging.getLogger(__name__)

_WRITE_CHUNK = 500


def _decide(row: dict) -> tuple[str | None, bool]:
    """(the incorporation date to carry over or None, whether `founded` goes)."""
    old = row.get("founded_date")
    carry = str(old) if old and not row.get("incorporated_on") else None
    inc = row.get("incorporated_on") or old
    year = str(inc)[:4] if inc else None
    founded = row.get("founded")
    clear = bool(founded is not None and year and str(founded) == year and not row.get("wikidata_id"))
    return carry, clear


def heal(apply: bool = True) -> dict:
    """Walk every Entity by its id index (app.db.paging) and, by @rid in
    chunks: move `founded_date` to `incorporated_on` (dropping the old field),
    and remove a `founded` that only repeated the incorporation year on a node
    no other source dated. Dry run counts."""
    from app.db import paging
    c = {"scanned": 0, "renamed": 0, "dropped_old_field": 0, "cleared_founded": 0, "written": 0,
         "applied": bool(apply)}
    for page in paging.iter_id_pages("Entity", columns="id, @rid AS rid, founded, founded_date, incorporated_on, wikidata_id"):
        c["scanned"] += len(page)
        stmts: list[str] = []
        params: dict = {}
        for r in page:
            if not r.get("rid"):
                continue
            carry, clear = _decide(r)
            sets, removes = [], []
            if carry:
                k = f"v{len(params)}"
                params[k] = carry
                sets.append(f"incorporated_on = :{k}")
                c["renamed"] += 1
            if r.get("founded_date") is not None:
                removes.append("founded_date")
                c["dropped_old_field"] += 1
            if clear:
                removes.append("founded")
                c["cleared_founded"] += 1
            if not sets and not removes:
                continue
            stmt = f"UPDATE {r['rid']}"
            if sets:
                stmt += " SET " + ", ".join(sets)
            if removes:
                stmt += " REMOVE " + ", ".join(removes)
            stmts.append(stmt + ";")
        if not apply or not stmts:
            continue
        for i in range(0, len(stmts), _WRITE_CHUNK):
            chunk = stmts[i:i + _WRITE_CHUNK]
            run_sqlscript("\n".join(chunk), params)
            c["written"] += len(chunk)
    log.info("incorporated_on heal: %s", c)
    return c
