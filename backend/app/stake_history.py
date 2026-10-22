"""
Stake history — the dated statements behind a holding's stake, kept for
holdings at or above 5 %.

An `OWNS` edge and the `Claim` beneath it hold ONE stake: the latest a source
stated. Each newer statement overwrote the older — a 13F every quarter, a 13D/G
amendment, a register re-read — so a past year showed today's number and the
timeline could not say when a holder became material. A `StakeHistory` row
keeps what was overwritten.

Three rules, each the point rather than an implementation detail:

**Only changes are written, and only material ones.** A row is written when a
source's newly stated stake differs from what it stated before, and one side of
the change is at or above `MATERIAL_PCT` — the SEC's own 13D/G line, the
threshold above which a holder must disclose. The long tail of sub-5 % 13F
positions never reaches the table; a holding crossing 5 % in either direction
always does, because one side of the change is above it.

**The first change also records the statement it replaces.** A claim's first
statement writes nothing — the claim holds it, and most claims never change.
When one does, the OLD statement (its stake and its own date) goes in beside
the new one, so the series starts at the value the claim had before, not at the
change. From then on each change writes one row.

**The row says when the source stated the number** (`as_of`): the filing's date
of event when it gives one, else its filing date; a 13D/G stake re-divided by a
newer total of the class carries the date of that total. Never the day the
scrape ran — that is `recorded_at`.

The edge and the claim are unchanged. Readers:

- time travel (`/entities/.../full-profile?as_of=`) replaces a holding's stake
  with the latest row on or before the date (the earliest row when none
  precedes it, which is what the edge showed before this table existed);
- the timeline shows the two crossings of 5 % — reached, dropped below — and
  nothing else, so quarterly drift cannot flood it;
- the holding's own row offers the full series (`/relationships/stake-history`);
- the spreadsheet export carries every row on its own sheet.

Rows carry `from_id` / `to_id` / `source_id`, not the claim key: a merge
re-points them with the claims (`migrate`), and the key would go stale.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: A holding at or above this is material: the SEC 13D/G disclosure line.
MATERIAL_PCT = 5.0

TYPE = "StakeHistory"

#: What the pre-read needs of a stored claim to decide whether a write changes it.
_CLAIM_COLUMNS = ("claim_key, from_id, to_id, source_id, stake_percent, event_date, "
                  "source_date, denominator_date, filing_type, source_url, first_seen_at")

_ROW_FIELDS = ("from_id", "to_id", "source_id", "stake_percent", "previous_percent",
               "as_of", "filing_type", "source_url", "recorded_at")

_PRE_READ_CHUNK = 500


# ── Pure logic ───────────────────────────────────────────────────────────────

def _pct(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def changed(old, new) -> bool:
    """A different number, or a number where there was none (and the reverse)."""
    o, n = _pct(old), _pct(new)
    if o is None or n is None:
        return (o is None) != (n is None)
    return abs(o - n) > 1e-6


def material(old, new) -> bool:
    """One side of the change is at or above the 5 % line."""
    return max(_pct(old) or 0.0, _pct(new) or 0.0) >= MATERIAL_PCT


def _day(v) -> str | None:
    s = str(v or "").strip()
    return s[:10] if len(s) >= 10 else (s or None)


def statement_date(claim: dict) -> str | None:
    """When the source stated this stake: the date of event, else the filing
    date; a stake re-divided by a newer total of the class (`denominator_date`
    after the statement) is as of that total."""
    base = _day(claim.get("event_date")) or _day(claim.get("source_date"))
    restated = _day(claim.get("denominator_date"))
    if base and restated and restated > base:
        return restated
    return base


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _row(claim: dict, stake, previous, as_of: str, recorded_at: str) -> dict:
    return {
        "from_id": claim.get("from_id"),
        "to_id": claim.get("to_id"),
        "source_id": claim.get("source_id"),
        "stake_percent": _pct(stake),
        "previous_percent": _pct(previous),
        "as_of": as_of,
        "filing_type": claim.get("filing_type"),
        "source_url": claim.get("source_url"),
        "recorded_at": recorded_at,
    }


def rows_for(old: dict | None, new: dict, had_history: bool,
             now: str | None = None) -> list[dict]:
    """The rows one claim write produces.

    `old` is the stored claim (None when this is its first statement: nothing
    is written — the claim holds it). `had_history` says whether the pair +
    source already has rows; when it has none, the old statement is written
    first so the series starts where the claim stood.
    """
    if new.get("kind", "owns") != "owns" or old is None:
        return []
    o, n = old.get("stake_percent"), new.get("stake_percent")
    if not changed(o, n) or not material(o, n):
        return []
    recorded_at = now or datetime.now(timezone.utc).isoformat()
    rows = []
    if _pct(o) is not None and not had_history:
        rows.append(_row(old, o, None,
                         statement_date(old) or _day(old.get("first_seen_at")) or recorded_at[:10],
                         recorded_at))
    rows.append(_row(new, n, o, statement_date(new) or recorded_at[:10], recorded_at))
    return rows


def crossings(rows: list[dict]) -> list[dict]:
    """The rows where a holding crossed the 5 % line — reached it, or dropped
    below it. A statement with no previous number is not a crossing: before it
    the stake was unknown, not below."""
    out = []
    for r in rows:
        p, n = _pct(r.get("previous_percent")), _pct(r.get("stake_percent"))
        if p is None or n is None:
            continue
        if p < MATERIAL_PCT <= n:
            out.append({**r, "crossing": "reached"})
        elif n < MATERIAL_PCT <= p:
            out.append({**r, "crossing": "dropped"})
    return out


def _series_key(r: dict) -> tuple:
    return (r.get("from_id"), r.get("to_id"), r.get("source_id"))


def _ordered(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r.get("as_of") or "", r.get("recorded_at") or ""))


def stakes_as_of(rows: list[dict], as_of: str) -> dict[tuple, dict]:
    """Per (from, to, source): the stake in force on `as_of` — the latest row on
    or before it, else the earliest row (what the edge showed before the table
    existed). ISO strings compare as dates."""
    series: dict[tuple, list[dict]] = {}
    for r in _ordered(rows):
        series.setdefault(_series_key(r), []).append(r)
    out = {}
    for key, seq in series.items():
        pick = seq[0]
        for r in seq:
            if (r.get("as_of") or "") <= as_of:
                pick = r
            else:
                break
        out[key] = {"stake_percent": pick.get("stake_percent"), "as_of": pick.get("as_of")}
    return out


def create_statements(rows: list[dict], params: dict, prefix: str = "sh") -> list[str]:
    """SQL-script statements creating the rows, parameters added to `params`."""
    stmts = []
    for k, row in enumerate(rows):
        sets = []
        for name in _ROW_FIELDS:
            pk = f"{prefix}_{name}__{k}"
            params[pk] = row.get(name)
            sets.append(f"{name} = :{pk}")
        stmts.append(f"CREATE VERTEX {TYPE} SET {', '.join(sets)};")
    return stmts


# ── Database ─────────────────────────────────────────────────────────────────
# `run_sql` is imported inside each function so the pure logic above stays
# importable without a database layer (as app.claims does).

def previous_claims(keys: list[str]) -> dict[str, dict]:
    """The stored claims for these keys, by key — one indexed query per chunk."""
    from app.db.arcadedb import run_sql
    out: dict[str, dict] = {}
    keys = list(dict.fromkeys(k for k in keys if k))
    for i in range(0, len(keys), _PRE_READ_CHUNK):
        for r in run_sql(f"SELECT {_CLAIM_COLUMNS} FROM Claim WHERE claim_key IN :keys",
                         {"keys": keys[i:i + _PRE_READ_CHUNK]}):
            out[r["claim_key"]] = r
    return out


def has_history(from_id: str, to_id: str, source_id: str) -> bool:
    from app.db.arcadedb import run_sql
    rows = run_sql(f"SELECT count(*) AS n FROM {TYPE} "
                   "WHERE from_id = :f AND to_id = :t AND source_id = :s",
                   {"f": from_id, "t": to_id, "s": source_id})
    return bool(rows and rows[0].get("n"))


def rows_for_batch(claims: list[dict]) -> list[dict]:
    """The history rows a batch of claim writes produces, read against the
    stored claims BEFORE the writes land. Best-effort: a failed pre-read logs
    and writes no history — the claim and the edge still go through."""
    owns = [c for c in claims if c.get("kind", "owns") == "owns" and c.get("claim_key")]
    if not owns:
        return []
    try:
        stored = previous_claims([c["claim_key"] for c in owns])
        rows: list[dict] = []
        seen_history: set[tuple] = set()
        for c in owns:
            old = stored.get(c["claim_key"])
            if old is None:
                stored[c["claim_key"]] = c        # a twin later in the batch compares to this
                continue
            if not changed(old.get("stake_percent"), c.get("stake_percent")) \
                    or not material(old.get("stake_percent"), c.get("stake_percent")):
                stored[c["claim_key"]] = {**old, **c}
                continue
            key = _series_key(c)
            had = key in seen_history or has_history(*key)
            rows.extend(rows_for(old, c, had))
            seen_history.add(key)
            stored[c["claim_key"]] = {**old, **c}
        return rows
    except Exception as exc:  # noqa: BLE001 - history must never break a write
        log.warning("stake history: pre-read failed, rows skipped for this batch: %s", exc)
        return []


def record_rows(rows: list[dict]) -> None:
    """Write the rows one by one (the incremental scrapers' path)."""
    from app.db.arcadedb import run_sql
    for row in rows:
        params: dict = {}
        try:
            run_sql(create_statements([row], params)[0].rstrip(";"), params)
        except Exception as exc:  # noqa: BLE001
            log.warning("stake history: could not record %s->%s from %s: %s",
                        row.get("from_id"), row.get("to_id"), row.get("source_id"), exc)


def _clean(r: dict) -> dict:
    return {f: r.get(f) for f in _ROW_FIELDS}


def history_for_pair(from_id: str, to_id: str) -> list[dict]:
    """Every source's rows for one holding, newest first."""
    from app.db.arcadedb import run_sql
    rows = run_sql(f"SELECT FROM {TYPE} WHERE from_id = :f AND to_id = :t",
                   {"f": from_id, "t": to_id})
    return list(reversed(_ordered([_clean(r) for r in rows])))


def history_for_entity(entity_id: str) -> list[dict]:
    """Every row about an entity's holders (to_id) and holdings (from_id),
    oldest first. Two indexed queries rather than one OR."""
    from app.db.arcadedb import run_sql
    rows = [_clean(r) for r in run_sql(f"SELECT FROM {TYPE} WHERE to_id = :id", {"id": entity_id})]
    rows += [_clean(r) for r in run_sql(f"SELECT FROM {TYPE} WHERE from_id = :id", {"id": entity_id})
             if r.get("to_id") != entity_id]
    return _ordered(rows)


def migrate(dead_id: str, keep_id: str) -> int:
    """Re-point the rows of a merged-away (or renamed) node at its survivor."""
    from app.db.arcadedb import run_sql
    moved = 0
    for col in ("from_id", "to_id"):
        res = run_sql(f"UPDATE {TYPE} SET {col} = :new WHERE {col} = :old",
                      {"new": keep_id, "old": dead_id})
        moved += int((res[0].get("count") or 0) if res and isinstance(res[0], dict) else 0)
    return moved
