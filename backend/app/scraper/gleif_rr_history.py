"""
GLEIF relationship history from the golden-copy archive.

The golden copy is a picture of today. It states when each CURRENT relationship
began, but a relationship that has ended is simply gone from it: of 489,953
relationship records on 2026-10-05, 60 were marked INACTIVE. So a parent sold in
2021 never owned its subsidiary as far as an import of today's file can tell, and
time travel to 2019 showed today's tree. The deltas don't help either: GLEIF
publishes them for the last day, week and month only.

What GLEIF does keep is every golden copy it ever published, back to
**2018-02-09** (four a day; the relationship file was 5 MB in 2018, 34 MB now).
This module reads **one per month** plus the latest, oldest first, and rebuilds
each consolidation pair's history from presence alone:

* a pair present in one snapshot and gone from the next **ended between them**:
  a closed edge with ``until`` = the first snapshot without it and
  ``until_reason = gleif_dropped`` ("ended by then", the same convention as an
  INACTIVE record's ``gleif_inactive``) — so it is shown up to a month too long,
  never hidden while it was real;
* a pair with no stated start gets ``since`` = the first snapshot listing it,
  ``since_basis = gleif_first_seen``: a LOWER bound ("at least since"), which
  time travel dims before that date rather than hiding — the 2018-02-09 archive
  start included, which says only "at least since February 2018".

A stated start (the record's RELATIONSHIP_PERIOD) always wins over the snapshot
date; an existing ``since`` on a current edge is never touched.

Two phases, because the downloads are the slow, network-bound part and the
result is small:

1. :func:`build_history` downloads, reduces each snapshot to its set of pairs
   and deletes it, and writes the **intervals file** (gzip JSON lines, one
   line per period of a pair; a few MB). Kept on the box, so re-applying after
   a rebuild needs no download.
2. :func:`apply_history` writes the intervals into the graph: closed periods as
   their own closed edges beside the current one (the same shape the RR
   importer gives an INACTIVE record), and lower-bound starts on current edges
   that have none. Idempotent: a second run finds its edges and writes nothing.

It never creates a CURRENT edge or a company: today's relationships are the
importer's, and a period whose companies are not in the graph is skipped
(counted) — on the test import most historical counterparties are outside the
test families.

A run, once, after the first full import (``manage.py gleif-rr-history``).
From then on the daily ``gleif-update`` records endings as they happen.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import IO, Iterable, Iterator

import httpx
import ijson

from app.db.arcadedb import run_sql
from app.scraper.bulk_import import _BatchWriter, _flush_script, _now_iso, _tmp_dir
from app.scraper.gleif_incremental import _PUBLISHES_API
from app.scraper.gleif_rr import REGISTRATION_DAY, _rr_edge

log = logging.getLogger(__name__)

#: The first publish in GLEIF's archive.
ARCHIVE_START = date(2018, 2, 9)

#: The start label and the end reason this module writes.
SINCE_BASIS = "gleif_first_seen"
UNTIL_REASON = "gleif_dropped"

#: A broken publish is a DIP: smaller than the month before and back the month
#: after (2023-08-01: 281,309 records between 398,842 and ~400k). A real
#: clean-up stays down — the file shrank in 26 of 105 months, 2019-09 by 10.3 %,
#: and those relationships did not return. So a snapshot more than this share
#: below the last believed one is held back one month, and skipped only if the
#: next one recovers. A fixed floor alone would skip every later snapshot after
#: a real drop larger than itself, all the way to today.
_DIP = 0.05

#: Publishes are at 00:00, 08:00 and 16:00 UTC; the first that exists on or
#: after the first of the month is the month's snapshot.
_SLOTS = ("0000", "0800", "1600")
_DAYS_TO_TRY = 7

#: lei_id lookups per query, and child entities per edge query.
_CHUNK = 500


# ── snapshots ─────────────────────────────────────────────────────────────────

def month_days(start: date, end: date) -> list[date]:
    """The day each month's snapshot is looked for: ``start`` itself, then the
    first of every following month up to ``end``."""
    days, d = [start], date(start.year, start.month, 1)
    while True:
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        if d > end:
            return days
        days.append(d)


def find_publish(client: httpx.Client, day: date) -> dict | None:
    """The first publish on or after ``day`` (within a week) whose relationship
    file is offered as JSON: ``{publish_date, url, record_count}``, or None."""
    for offset in range(_DAYS_TO_TRY):
        d = day + timedelta(days=offset)
        for slot in _SLOTS:
            r = client.get(f"{_PUBLISHES_API}/rr/{d:%Y%m%d}-{slot}")
            if r.status_code != 200:
                continue
            data = r.json().get("data") or {}
            js = ((data.get("full_file") or {}).get("json")) or {}
            if js.get("url"):
                return {"publish_date": data["publish_date"][:10], "url": js["url"],
                        "record_count": js.get("record_count")}
    return None


def latest_publish(client: httpx.Client) -> dict:
    data = client.get(f"{_PUBLISHES_API}/rr/latest").json()["data"]
    js = data["full_file"]["json"]
    return {"publish_date": data["publish_date"][:10], "url": js["url"],
            "record_count": js.get("record_count")}


def snapshot_pairs(raw: IO[bytes]) -> tuple[dict[tuple[str, str], tuple[str, str | None]], int]:
    """Every ACTIVE consolidation pair in one RR-CDF file, folded like the
    importer folds them (one per pair): ``{(parent, child): (marker, since)}`` —
    ``direct`` if GLEIF stated the direct relationship, and the earlier stated
    start of the two records. Returns it with the number of records read."""
    pairs: dict[tuple[str, str], tuple[str, str | None]] = {}
    records = 0
    for rec in ijson.items(raw, "relations.item"):
        records += 1
        edge = _rr_edge(rec)
        if not edge or not edge[5]:            # not consolidation, or INACTIVE
            continue
        parent, child, marker, since = edge[0], edge[1], edge[2], edge[3]
        key = (sys.intern(parent), sys.intern(child))
        old = pairs.get(key)
        if old:
            marker = "direct" if "direct" in (marker, old[0]) else marker
            since = min((d for d in (since, old[1]) if d), default=None)
        pairs[key] = (marker, since)
    return pairs, records


class History:
    """Each pair's periods of presence, fed snapshots oldest first."""

    def __init__(self) -> None:
        # pair -> [first_seen, last_seen, marker, stated since]
        self.open: dict[tuple[str, str], list] = {}
        self.closed: list[dict] = []
        self.snapshots: list[str] = []

    def observe(self, day: str, pairs: dict[tuple[str, str], tuple[str, str | None]]) -> None:
        if self.snapshots and day <= self.snapshots[-1]:
            raise ValueError(f"snapshots must be fed oldest first: {day} after {self.snapshots[-1]}")
        self.snapshots.append(day)
        for pair, (marker, since) in pairs.items():
            period = self.open.get(pair)
            if period is None:
                self.open[pair] = [day, day, marker, since]
                continue
            period[1] = day
            if marker == "direct":
                period[2] = "direct"
            if since and (not period[3] or since < period[3]):
                period[3] = since
        for pair in [p for p in self.open if p not in pairs]:
            first, last, marker, since = self.open.pop(pair)
            self.closed.append(_period(pair, first, last, day, marker, since))

    def periods(self) -> Iterator[dict]:
        yield from self.closed
        for pair, (first, last, marker, since) in self.open.items():
            yield _period(pair, first, last, None, marker, since)


def _period(pair, first, last, until, marker, since) -> dict:
    return {"parent": pair[0], "child": pair[1], "first_seen": first, "last_seen": last,
            "until": until, "marker": marker, "since": since}


def _download(client: httpx.Client, url: str, dest: str) -> None:
    for attempt in range(3):
        try:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
            return
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise
            log.warning("download failed (%s), retrying: %s", url, exc)
            time.sleep(5 * (attempt + 1))


def _open_json(path: str) -> IO[bytes]:
    zf = zipfile.ZipFile(path)
    names = [n for n in zf.namelist() if n.lower().endswith(".json")]
    if not names:
        raise ValueError(f"no .json inside {path}")
    return zf.open(names[0])


def build_history(out_path: str, start: date = ARCHIVE_START, end: date | None = None,
                  client: httpx.Client | None = None, echo=print) -> dict:
    """Download one relationship file per month from ``start`` and the latest,
    reduce each to its pairs, and write the intervals file to ``out_path``."""
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True)
    history = History()
    skipped: list[str] = []
    prev_records = 0
    held: tuple | None = None          # (n, publish_date, pairs, records) on probation
    t0 = time.monotonic()
    try:
        latest = latest_publish(client)
        end = end or date.fromisoformat(latest["publish_date"])
        plan = []
        for day in month_days(start, end):
            pub = find_publish(client, day)
            if pub is None:
                skipped.append(f"{day:%Y-%m} (no publish)")
            elif not plan or pub["publish_date"] > plan[-1]["publish_date"]:
                plan.append(pub)
        if latest["publish_date"] > (plan[-1]["publish_date"] if plan else ""):
            plan.append(latest)
        with tempfile.TemporaryDirectory(dir=_tmp_dir(), prefix="gleif-rr-history-") as tmp:
            for n, pub in enumerate(plan, start=1):
                path = os.path.join(tmp, "rr.json.zip")
                _download(client, pub["url"], path)
                with _open_json(path) as raw:
                    pairs, records = snapshot_pairs(raw)
                os.remove(path)

                def believe(n, day, pairs, records):
                    nonlocal prev_records
                    prev_records = records
                    history.observe(day, pairs)
                    echo(f"  {n}/{len(plan)} {day}: {len(pairs):,} pairs, "
                         f"{len(history.open):,} open, {len(history.closed):,} ended so far "
                         f"({time.monotonic() - t0:.0f}s)")

                low = prev_records * (1 - _DIP)
                if held:
                    hn, hday, hpairs, hrecords = held
                    held = None
                    if records >= low:          # back up: the held one was a dip
                        skipped.append(f"{hday} ({hrecords:,} records between {prev_records:,} "
                                       f"and {records:,}: a dip, not believed)")
                        echo(f"  {hn}/{len(plan)} {hday}: SKIPPED, a dip of {hrecords:,} records")
                    else:                       # still down: a real clean-up
                        believe(hn, hday, hpairs, hrecords)
                        low = prev_records * (1 - _DIP)
                if prev_records and records < low and n < len(plan):
                    held = (n, pub["publish_date"], pairs, records)
                    continue
                # the latest is never held: it is the copy today's graph comes from
                believe(n, pub["publish_date"], pairs, records)
    finally:
        if own:
            client.close()

    written = write_intervals(out_path, history, skipped)
    return {"snapshots": len(history.snapshots), "first": history.snapshots[0] if history.snapshots else None,
            "last": history.snapshots[-1] if history.snapshots else None,
            "periods": written, "ended": len(history.closed), "open": len(history.open),
            "skipped": skipped, "seconds": round(time.monotonic() - t0)}


def write_intervals(path: str, history: History, skipped: list[str] | None = None) -> int:
    """The intervals file: a header line, then one line per period. Written
    beside and renamed into place, so an interrupted build leaves the old one."""
    tmp = f"{path}.tmp"
    n = 0
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"header": {"snapshots": history.snapshots, "skipped": skipped or [],
                                        "built_at": datetime.now(timezone.utc).isoformat()}}) + "\n")
        for period in history.periods():
            fh.write(json.dumps(period, separators=(",", ":")) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def read_intervals(path: str) -> tuple[dict, list[dict]]:
    header: dict = {}
    periods: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if "header" in row:
                header = row["header"]
            else:
                periods.append(row)
    return header, periods


# ── applying ──────────────────────────────────────────────────────────────────

def _resolve(leis: set[str]) -> tuple[dict[str, str], dict[str, tuple]]:
    """lei → entity id, by the ``lei_id`` property rather than the ``lei:`` id,
    so a company merged into a Companies House node is still found; and lei →
    (LEI registration date, founding date), for the registration-day rule."""
    out: dict[str, str] = {}
    regs: dict[str, tuple] = {}
    ordered = sorted(leis)
    for i in range(0, len(ordered), _CHUNK):
        rows = run_sql("SELECT id, lei_id, lei_registration_date, founded_date FROM Entity "
                       "WHERE lei_id IN :l", {"l": ordered[i:i + _CHUNK]})
        for r in rows or []:
            if r.get("lei_id") and r.get("id"):
                out.setdefault(r["lei_id"], r["id"])
                regs.setdefault(r["lei_id"], (r.get("lei_registration_date"), r.get("founded_date")))
    return out, regs


def _rr_edges(child_ids: set[str]) -> dict[tuple[str, str], list[dict]]:
    """The GLEIF relationship edges into these companies, current and ended."""
    out: dict[tuple[str, str], list[dict]] = {}
    ordered = sorted(child_ids)
    for i in range(0, len(ordered), _CHUNK):
        rows = run_sql(
            # `@out.id`, not `out.id`: ArcadeDB answers the latter with null
            "SELECT @rid AS rid, @out.id AS o, @in.id AS i, since, since_basis, until, until_reason "
            "FROM (SELECT expand(inE('OWNS')) FROM Entity WHERE id IN :ids) "
            "WHERE filing_type = 'RR'", {"ids": ordered[i:i + _CHUNK]})
        for r in rows or []:
            out.setdefault((r["o"], r["i"]), []).append(r)
    return out


def _overlaps(edge: dict, start: str, end: str) -> bool:
    """Whether an ended edge covers part of [start, end) — the same period
    already written (this module's own, on a re-run, or the importer's from
    an INACTIVE record)."""
    return bool(edge.get("until")) and edge["until"] >= start and \
        (not edge.get("since") or edge["since"] <= end)


def join_holes(plist: list[dict]) -> list[dict]:
    """One pair's periods, oldest first, with the holes in GLEIF's file closed:
    a pair that is gone for a while and comes back with the SAME stated start
    is one relationship the file lost track of, not an end and a new start.
    Measured on the archive (2026-10-06): 63,273 of 85,001 gaps came back with
    the same start, 18,253 with a different one (a real new period); 3,475
    had a side without a stated start and are left apart — nothing says they
    are the same relationship."""
    out: list[dict] = []
    for p in sorted(plist, key=lambda p: p["first_seen"]):
        prev = out[-1] if out else None
        if prev and prev["since"] and prev["since"] == p["since"]:
            out[-1] = {**prev, "last_seen": p["last_seen"], "until": p["until"],
                       "marker": "direct" if "direct" in (prev["marker"], p["marker"]) else p["marker"]}
        else:
            out.append(dict(p))
    return out


def tops(periods: Iterable[dict]) -> dict[str, str]:
    """company LEI → the last snapshot it was the TOP of a tree: named as
    someone's ultimate parent (a period that is ultimate-only, so marked
    ``indirect``; a pair stated both ways is folded to ``direct`` and not
    counted — which can only miss a contradiction, never invent one)."""
    out: dict[str, str] = {}
    for p in periods:
        if p["marker"] == "indirect" and p["last_seen"] > out.get(p["parent"], ""):
            out[p["parent"]] = p["last_seen"]
    return out


def refuted_by(period: dict, top_last: dict[str, str]) -> str | None:
    """The snapshot that refutes a period's stated start, or None.

    A company that is the top of a tree has no parent. So a start stated
    before the last snapshot in which the child was still someone's ultimate
    parent — and before the relationship itself appeared — is contradicted by
    GLEIF's own archive. Activision Blizzard registered Microsoft as its
    ultimate parent "since 2001-07-03" in July 2026, while King.com named
    Activision its ultimate parent until 2026-07 (the acquisition closed
    2023-10-13). 695 of 367,701 stated starts on 2026-10-06."""
    t = top_last.get(period["child"])
    s = period["since"]
    return t if s and t and s < t < period["first_seen"] else None


def plan_history(periods: Iterable[dict], ids: dict[str, str],
                 edges: dict[tuple[str, str], list[dict]],
                 regs: dict[str, tuple] | None = None) -> dict:
    """What applying would do, without touching the graph: the closed edges to
    create (``create``: (owner_id, owned_id, props, claim)), the start dates to
    fill (``fill``: (rid, since)), the refuted starts to correct (``correct``:
    (rid, stated, since, not_before)), and counts of everything skipped and why."""
    periods = list(periods)
    top_last = tops(periods)
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for p in periods:
        by_pair.setdefault((p["parent"], p["child"]), []).append(p)
    create, fill, correct = [], [], []
    skip = {"unresolved": 0, "already_written": 0, "current_in_graph": 0,
            "same_relationship": 0, "open_without_edge": 0, "stated_start": 0}
    for (parent, child), plist in by_pair.items():
        owner, owned = ids.get(parent), ids.get(child)
        if not owner or not owned:
            skip["unresolved"] += len(plist)
            continue
        existing = edges.get((owner, owned), [])
        current = [e for e in existing if not e.get("until")]
        plist = join_holes(plist)
        for k, p in enumerate(plist):
            refuted = refuted_by(p, top_last)
            if p["until"] is None:
                if not current:
                    skip["open_without_edge"] += 1
                for e in current:
                    if refuted and e.get("since") == p["since"] and \
                            e.get("since_basis") in (None, REGISTRATION_DAY):
                        # the importer wrote GLEIF's refuted date: the edge stops
                        # believing it (the claim keeps it)
                        correct.append((e["rid"], p["since"], p["first_seen"], refuted))
                    elif e.get("since"):
                        skip["stated_start"] += 1
                    else:
                        fill.append((e["rid"], p["first_seen"]))
                continue
            last = k == len(plist) - 1
            stated = p["since"] if not refuted else None
            since = stated or p["first_seen"]
            if last and current:
                # the graph is older than the newest snapshot: the daily delta
                # closes current edges, not the history
                skip["current_in_graph"] += 1
            elif any(_overlaps(e, since, p["until"]) for e in existing):
                skip["already_written"] += 1
            elif stated and any(e.get("since") == stated for e in current):
                # a gap in the file (a lapse) inside one relationship with the
                # same stated start — not a second relationship
                skip["same_relationship"] += 1
            else:
                props = {
                    "stake_percent": None, "ownership_type": "controlling",
                    "voting_power_pct": None, "interest_types": ["accountingConsolidation"],
                    "direct_or_indirect": p["marker"], "since": since,
                    "until": p["until"], "until_reason": UNTIL_REASON,
                    "source_url": f"https://search.gleif.org/#/record/{child}",
                    "source_date": p["last_seen"], "filing_type": "RR",
                }
                registered, founded = (regs or {}).get(child, (None, None))
                if not stated:
                    props["since_basis"] = SINCE_BASIS
                elif stated == registered and stated != founded:
                    props["since_basis"] = REGISTRATION_DAY
                if refuted:
                    props["since_not_before"] = refuted
                create.append((owner, owned, props, not current))
    return {"create": create, "fill": fill, "correct": correct, "skipped": skip}


def apply_history(path: str, source_id: str, credibility_score: int,
                  dry_run: bool = False) -> dict:
    """Write the intervals file into the graph (see the module docstring)."""
    header, periods = read_intervals(path)
    leis = {p["parent"] for p in periods} | {p["child"] for p in periods}
    ids, regs = _resolve(leis)
    children = {ids[p["child"]] for p in periods if p["child"] in ids}
    edges = _rr_edges(children)
    plan = plan_history(periods, ids, edges, regs)
    result = {"snapshots": len(header.get("snapshots") or []), "periods": len(periods),
              "companies_found": len(ids), "created": len(plan["create"]),
              "since_filled": len(plan["fill"]), "starts_corrected": len(plan["correct"]),
              "skipped": plan["skipped"],
              "dry_run": dry_run}
    if dry_run:
        return result
    batch = _BatchWriter()
    now = _now_iso()
    for owner, owned, props, claim in plan["create"]:
        batch.owns(owner, "Entity", owned, {**props, "source_id": source_id,
                                            "credibility_score": credibility_score,
                                            "last_scraped_at": now}, claim=claim)
    batch.flush()
    for i in range(0, len(plan["fill"]), _CHUNK):
        chunk = plan["fill"][i:i + _CHUNK]
        stmts, params = [], {}
        for k, (rid, since) in enumerate(chunk):
            params[f"s{k}"] = since
            # `since IS NULL` again here: never over a date written meanwhile
            stmts.append(f"UPDATE {rid} SET since = :s{k}, since_basis = '{SINCE_BASIS}' "
                         f"WHERE since IS NULL;")
        _flush_script("\n".join(stmts), params)
    for i in range(0, len(plan["correct"]), _CHUNK):
        chunk = plan["correct"][i:i + _CHUNK]
        stmts, params = [], {}
        for k, (rid, stated, since, floor) in enumerate(chunk):
            params.update({f"o{k}": stated, f"s{k}": since, f"f{k}": floor})
            # only while the edge still carries the refuted date
            stmts.append(f"UPDATE {rid} SET since = :s{k}, since_basis = '{SINCE_BASIS}', "
                         f"since_not_before = :f{k} WHERE since = :o{k};")
        _flush_script("\n".join(stmts), params)
    return result
