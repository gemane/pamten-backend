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

**One statement per run.** A scrape that writes the same claim twice in one
run states the LAST value, not a flap through the first. The SEC company
scrape used to put a holder's 13D/G figure and then their Form 4 figure into
the one claim (Musk → Tesla read 28.4 → 19.9 → 28.4 on every run); the Form 4
now yields to a 13D/G statement (`sec_writer.stated_by_13dg`), and this rule
stays as the safety net for any runner. A runner wraps its writes in
`one_statement_per_run()`; inside it `record_claim` defers, the stored claim
is read once at the first write of a key, and the rows are computed at the end
against that pre-run claim and the key's last write.

**A statement dated before the one it replaces** writes no replaced-statement
row: the series must not carry a later-dated number the claim no longer states.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
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
    old_day = statement_date(old) or _day(old.get("first_seen_at")) or recorded_at[:10]
    new_day = statement_date(new) or recorded_at[:10]
    rows = []
    # The replaced statement is kept only when it precedes the new one: a
    # statement dated BEFORE the one it replaces (a holder's 13G/A beneficial
    # ownership taking over from their later-dated Form 4 count of record) must
    # not leave a later-dated number in the series that the claim no longer
    # states — time travel would show it after the new statement's day.
    if _pct(o) is not None and not had_history and old_day <= new_day:
        rows.append(_row(old, o, None, old_day, recorded_at))
    rows.append(_row(new, n, o, new_day, recorded_at))
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


def _owns_with_keys(claims: list[dict]) -> list[dict]:
    return [c for c in claims if c.get("kind", "owns") == "owns" and c.get("claim_key")]


def _rows_against(stored: dict[str, dict], owns: list[dict]) -> list[dict]:
    """The rows these writes produce against the claims in `stored` (by key)."""
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


def rows_for_batch(claims: list[dict]) -> list[dict]:
    """The history rows a batch of claim writes produces, read against the
    stored claims BEFORE the writes land. Best-effort: a failed pre-read logs
    and writes no history — the claim and the edge still go through."""
    owns = _owns_with_keys(claims)
    if not owns:
        return []
    try:
        return _rows_against(previous_claims([c["claim_key"] for c in owns]), owns)
    except Exception as exc:  # noqa: BLE001 - history must never break a write
        log.warning("stake history: pre-read failed, rows skipped for this batch: %s", exc)
        return []


# ── One statement per run ────────────────────────────────────────────────────

_UNREAD = object()      # the pre-read of this key failed: no history for it this run
_run_state = threading.local()


class _Run:
    def __init__(self) -> None:
        self.before: dict[str, object] = {}     # claim_key → stored claim | None | _UNREAD
        self.last: dict[str, dict] = {}         # claim_key → the key's last write
        self.order: list[str] = []


def deferred(props: dict) -> bool:
    """`record_claim` asks before it computes history: inside
    `one_statement_per_run()` the write is noted and its rows wait for the end
    of the run (True); outside, history is immediate (False)."""
    run = getattr(_run_state, "run", None)
    if run is None:
        return False
    if props.get("kind", "owns") != "owns" or not props.get("claim_key"):
        return True
    key = props["claim_key"]
    if key not in run.before:
        try:
            run.before[key] = previous_claims([key]).get(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("stake history: pre-read of %s failed, no history this run: %s", key, exc)
            run.before[key] = _UNREAD
        run.order.append(key)
    run.last[key] = props
    return True


def _flush(run: _Run) -> list[dict]:
    stored = {k: v for k, v in run.before.items() if isinstance(v, dict)}
    writes = [run.last[k] for k in run.order if run.before[k] is not _UNREAD]
    try:
        return _rows_against(stored, writes)
    except Exception as exc:  # noqa: BLE001
        log.warning("stake history: rows skipped for this run: %s", exc)
        return []


@contextmanager
def one_statement_per_run():
    """Inside: every `record_claim` of one key counts once, as its LAST write
    against the claim stored BEFORE the run. Nested blocks join the outer one.
    Usable as a decorator. The rows are written when the block ends, also when
    it ends by an exception — the claims it wrote have landed."""
    outer = getattr(_run_state, "run", None)
    if outer is not None:
        yield outer
        return
    run = _Run()
    _run_state.run = run
    try:
        yield run
    finally:
        _run_state.run = None
        rows = _flush(run)
        if rows:
            record_rows(rows)


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
