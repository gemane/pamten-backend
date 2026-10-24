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

The heal converts to the type the CODE declares, so it first checks that
the server declares the same: a property declared with another type
(`shares_outstanding` was LONG on dev for a day) coerces every rewrite
straight back, and the heal would report the same rows rewritten forever.
Such a group is skipped and reported; the schema bootstrap (`init-schema`,
or an API start) drops and redeclares the property, after which the heal
converts the rows.
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


def declared_types() -> dict[tuple[str, str], str | None]:
    """{(type, property): type} as the server's catalog declares them."""
    out: dict = {}
    for t in run_sql("SELECT name, properties FROM schema:types") or []:
        for p in t.get("properties") or []:
            if isinstance(p, dict) and p.get("name"):
                out[(t.get("name"), p["name"])] = p.get("type")
    return out


def heal(apply: bool = True) -> dict:
    """Rewrite every stored value whose type is not its declared one. Returns
    {"converted": {"Type.property": rows}} for what was (or would be)
    rewritten and {"misdeclared": {"Type.property": {"server", "code"}}} for
    the groups left alone because the server declares another type (or
    none) — run the schema bootstrap first for those."""
    out: dict = {"converted": {}, "misdeclared": {}, "applied": bool(apply)}
    live = declared_types()
    for vtype, prop, ptype in convertible():
        if live.get((vtype, prop)) != ptype:
            out["misdeclared"][f"{vtype}.{prop}"] = {"server": live.get((vtype, prop)), "code": ptype}
            log.warning("%s.%s is declared %s on the server, %s in the code — not converted",
                        vtype, prop, live.get((vtype, prop)), ptype)
            continue
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
