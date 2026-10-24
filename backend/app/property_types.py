"""Bring the rows stored before a property had a declared type into that type.

`app.db.schema._TYPED_PROPERTIES` declares every number, flag and list a
writer sets (2026-10-24). A declaration coerces every write from then on,
but ArcadeDB never touches what is already stored: `stake_percent` stayed
DOUBLE on 974 edges and INTEGER on 20, `shares` DOUBLE, INTEGER and LONG
side by side. Nor does rewriting a value to itself with a conversion
method re-type it (probed on 26.10.1: `SET p = p.asDouble()` left the
INTEGER in place), and neither does writing the value back as a parameter
of the right shape: the server compares the new value to the stored one,
20.0 equals 20, and skips the write. What re-types a row is a write that
CHANGES it — so `heal` reads the mismatched rows by @rid and, in one
script per chunk, sets each value to null and then to itself as a
parameter of the declared shape (a float for DOUBLE, an int for LONG).
Whole-type scans on unindexed columns: run straight against ArcadeDB
(`--db-url`), not through the proxy's 60 s.
"""
from __future__ import annotations

import logging

from app.db.arcadedb import run_sql, run_sqlscript

log = logging.getLogger(__name__)

_SCAN_TIMEOUT = 6 * 3600
_WRITE_CHUNK = 500
#: ArcadeDB type → how the stored value is re-shaped before it is written back
_CONVERT = {"DOUBLE": float, "FLOAT": float, "LONG": int, "INTEGER": int, "SHORT": int}


def convertible() -> list[tuple[str, str, str]]:
    """The declared properties whose stored type can differ from the declared
    one and be converted: the numeric ones. A BOOLEAN or LIST stored wrong
    is a bug in a writer, not a shape to coerce."""
    from app.db.schema import typed_properties
    return [(t, p, k) for t, p, k in typed_properties() if k in _CONVERT]


def heal(apply: bool = True) -> dict:
    """Rewrite every stored value whose type is not its declared one. Returns
    {"Type.property": rows} for what was (or would be) rewritten."""
    out: dict = {"converted": {}, "applied": bool(apply)}
    for vtype, prop, ptype in convertible():
        rows = run_sql(f"SELECT @rid AS rid, {prop} AS v FROM {vtype} "
                       f"WHERE {prop} IS NOT NULL AND {prop}.type() <> '{ptype}'",
                       timeout=_SCAN_TIMEOUT)
        rows = [r for r in rows or [] if r.get("rid") and r.get("v") is not None]
        if not rows:
            continue
        out["converted"][f"{vtype}.{prop}"] = len(rows)
        if not apply:
            continue
        cast = _CONVERT[ptype]
        for i in range(0, len(rows), _WRITE_CHUNK):
            chunk = rows[i:i + _WRITE_CHUNK]
            # null first: a write of an equal value is skipped and keeps the old type
            script = "\n".join(f"UPDATE {r['rid']} SET {prop} = null;\nUPDATE {r['rid']} SET {prop} = :v{k};"
                                for k, r in enumerate(chunk))
            params = {}
            for k, r in enumerate(chunk):
                try:
                    params[f"v{k}"] = cast(r["v"])
                except (TypeError, ValueError):
                    params[f"v{k}"] = r["v"]        # not ours to guess; the write will refuse it
            run_sqlscript(script, params)
    log.info("property types heal: %s", out)
    return out
