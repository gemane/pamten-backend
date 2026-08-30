"""
Companies House PSC incremental refresh — a delta from a source that has none.

GLEIF publishes delta files; Companies House does not. It publishes one ~2.2 GB
zipped snapshot of the whole PSC register, overwritten every morning before 10:00.
So the delta is computed here: **digest today's snapshot, compare it against
yesterday's digest, apply only what moved.**

Three properties make that exact rather than a guess:

* **Ceased PSCs stay in the snapshot**, carrying `ceased_on` — 17.9% of records do.
  A PSC ending its control is an in-record change, not a record disappearing, so
  the common case needs no inference at all.
* **Every record carries `data.links.self`**, unique across the file. It identifies
  an *appointment*, which is exactly one OWNS edge — so each change maps to one
  edge operation, which is what makes the whole thing testable.
* **A snapshot is a complete state.** Diffing against a five-day-old digest yields
  the five-day delta; nothing degrades but the size of the change set. There is no
  catch-up window to fall out of, so GLEIF's `choose_catchup_interval` has no
  equivalent here and deliberately none was written.

## What gets hashed, and why not the obvious things

Not the raw line, and not Companies House's own `etag`. Both are cheaper and both
are wrong for the same reason: `identity_verification_details` is mid-rollout
across the register, present on 44% of individual records and climbing. Either
would report millions of records as changed for a field the graph does not read.
A false positive costs one idempotent rewrite; a *mass* false positive costs a
multi-hour run that changes nothing.

So the digest covers a **projection**: exactly the fields `psc_record()` consumes.
Its failure mode is drift between the two — which is a test, not a mystery, and
`test_ch_psc_incremental.py` asserts every mapped field moves the digest and that
the ignored ones do not.

## Memory

The register is ~15.6M records. Everything here streams: a `dict` of that many
keys would want ~4.4 GB and this box has ~1.5 GB free. The digest sidecar is a
sorted TSV (gzipped, ~583 MB), and the diff is a two-cursor merge over two of
them — about 50 MB of resident memory regardless of register size.

`LC_ALL=C` on the sort is load-bearing, not a micro-optimisation: it is ~6× faster,
and it is the only collation whose ordering a Python string comparison reproduces.
A locale-collated sidecar walked by a byte-ordered merge silently mis-classifies
every record whose link straddles a collation difference.
"""
import gzip
import json
import logging
import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import blake2b
from typing import IO, Iterator

from app.db.arcadedb import run_command, run_sql
from app.claims import KIND_OWNS
from app.scraper.bulk_import import (
    _BatchWriter, _flush_script, _loads, _now_iso, _ProgressBar,
)
from app.scraper.companies_house_psc import psc_record

log = logging.getLogger(__name__)

#: Checkpoint for the last snapshot applied.
_STATE_KEY = "ch-psc-refresh"
#: Written by a full `ch-psc` import — the refresh's precondition.
_FULL_LOAD_KEY = "ch-psc-full-load"

#: Bump when `_projection` changes. Every stored digest is invalidated by such a
#: change, and a refresh that did not notice would report the whole register as
#: changed and rewrite 15.6M edges.
PROJECTION_VERSION = 1

#: `persons-with-significant-control-snapshot-2026-07-27.txt`
_ENTRY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
#: Pulls the self link out of a raw line without parsing the JSON — pass B tests
#: ~15.6M lines for membership and full-parses only the handful that changed.
#:
#: Anchored on the `links` object rather than on `"self"` alone. Position is not a
#: safe tiebreak: Companies House emits keys alphabetically, so `links` sits in the
#: middle of the line, and neither the first nor the last `"self"` is reliably the
#: right one if any other object ever carries that key.
_SELF_RE = re.compile(rb'"links"\s*:\s*\{[^{}]*?"self"\s*:\s*"([^"]+)"')

_PROBE_CHUNK = 1000


# ── the digest ────────────────────────────────────────────────────────────────

def _projection(rec: dict) -> list:
    """The parts of a record the graph actually reads, in a fixed order.

    Deliberately a hand-written list rather than `psc_record()`'s output: the
    mapping returns derived values (band floors, parsed names), and a change in
    *derivation* should not look like a change in the *record*. This is the source
    data those derivations read, and nothing else.
    """
    data = rec.get("data") or {}
    return [
        rec.get("company_number"),
        data.get("kind"),
        data.get("name"),
        data.get("name_elements"),
        data.get("nationality"),
        data.get("country_of_residence"),
        data.get("date_of_birth"),
        data.get("natures_of_control"),
        data.get("notified_on"),
        data.get("ceased_on"),
        data.get("identification"),
        data.get("address"),
        (data.get("links") or {}).get("self"),
    ]


def record_digest(rec: dict) -> str:
    """A short, stable digest of everything the graph would store for this record."""
    blob = json.dumps(_projection(rec), sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    return blake2b(blob, digest_size=9).hexdigest()


def self_link_of(line: bytes) -> str | None:
    """The self link in a raw snapshot line, found without parsing it.

    Anchored on `links.self`, not on any `"self"` in the line. Being wrong here is
    silent — the record simply never matches the change set and is skipped — so the
    pattern names the structure it wants rather than relying on where it falls.
    """
    m = _SELF_RE.search(line)
    return m.group(1).decode() if m else None


def snapshot_entry(zf: zipfile.ZipFile) -> str:
    """The single NDJSON member of a PSC snapshot zip."""
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if len(names) != 1:
        raise ValueError(f"expected one entry in the snapshot, found {len(names)}: {names}")
    return names[0]


def snapshot_date(entry_name: str) -> str:
    """The date Companies House stamped into the entry name.

    The register's own truth-time, and better than the file's mtime: it survives a
    copy, and it is what a closed edge is dated with, so the same input produces
    the same graph whenever it is applied.
    """
    m = _ENTRY_DATE.search(entry_name)
    if not m:
        raise ValueError(f"no snapshot date in entry name {entry_name!r}")
    return m.group(1)


def _open_snapshot(filepath: str) -> tuple[IO[bytes], str, int]:
    """(stream, entry name, uncompressed size) for a snapshot zip or bare NDJSON."""
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        entry = snapshot_entry(zf)
        return zf.open(entry), entry, zf.getinfo(entry).file_size
    return open(filepath, "rb"), os.path.basename(filepath), os.path.getsize(filepath)  # noqa: WPS515


class DigestSink:
    """Collects `link\tdigest` rows on disk, then sorts and gzips them.

    On disk, not in a list: the register is ~15.6M rows and the unsorted text is
    1.6 GB. Buffering that in memory is precisely the mistake this whole design
    exists to avoid, and it is an easy one to make when the code reads as "collect
    then write".

    `sort` does the external merge — it spills to `-T` and is bounded by `-S`, so
    peak memory is the sort buffer rather than the file.
    """

    def __init__(self, out_path: str, tmp_dir: str | None = None):
        self.out_path = out_path
        self.tmp_dir = (tmp_dir or os.environ.get("SCRAPER_TMP_DIR")
                        or os.path.dirname(out_path) or ".")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._unsorted = os.path.join(self.tmp_dir, f"{os.path.basename(out_path)}.unsorted")
        self._fh = open(self._unsorted, "w", encoding="utf-8")  # noqa: WPS515
        self.rows = 0

    def add(self, link: str, digest: str) -> None:
        self._fh.write(f"{link}\t{digest}\n")
        self.rows += 1

    def add_record(self, rec: dict) -> bool:
        """Digest one snapshot record. False when it carries no link to key on."""
        link = ((rec.get("data") or {}).get("links") or {}).get("self")
        if not link:
            return False
        self.add(link, record_digest(rec))
        return True

    def finish(self) -> str:
        self._fh.close()
        env = {**os.environ, "LC_ALL": "C"}       # see the module docstring
        sorted_path = f"{self._unsorted}.sorted"
        subprocess.run(["sort", "-S", "256M", "-T", self.tmp_dir, "-o", sorted_path,
                        self._unsorted], check=True, env=env)
        with open(sorted_path, "rb") as src, gzip.open(self.out_path, "wb", compresslevel=1) as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
        os.unlink(self._unsorted)
        os.unlink(sorted_path)
        return self.out_path

    def abandon(self) -> None:
        try:
            self._fh.close()
            os.unlink(self._unsorted)
        except OSError:
            pass


def write_digest(filepath: str, out_path: str, limit: int | None = None,
                 tmp_dir: str | None = None) -> dict:
    """Digest every record in a snapshot into a sorted, gzipped `link\\tdigest` TSV.

    Sorted with `LC_ALL=C sort` so the merge walk can compare bytes — see the module
    docstring on why that is correctness rather than speed.
    """
    raw, entry, _ = _open_snapshot(filepath)
    sink = DigestSink(out_path, tmp_dir)
    counts = {"records": 0, "skipped": 0, "errors": 0}
    bar = _ProgressBar("PSC digest")
    try:
        for line in raw:
            if limit and counts["records"] >= limit:
                break
            counts["records"] += 1
            try:
                rec = _loads(line)          # orjson where available: ~2× on 15.8M lines
            except ValueError:
                counts["errors"] += 1
                continue
            # No link, no key — such a record can never be matched to an edge, so
            # it cannot take part in a diff. Counted, not silently dropped.
            if not sink.add_record(rec):
                counts["skipped"] += 1
            if counts["records"] % 200000 == 0:
                bar.render(counts["records"], 0)
    except BaseException:
        sink.abandon()
        raise
    finally:
        raw.close()
    sink.finish()

    counts["entry"] = entry
    counts["snapshot_date"] = snapshot_date(entry)
    counts["digest"] = out_path
    bar.finish(f"{counts['records']:,} records digested → {os.path.basename(out_path)}")
    log.info("PSC digest done: %s", counts)
    return counts


# ── the diff ──────────────────────────────────────────────────────────────────

@dataclass
class DiffResult:
    """What moved between two snapshots. `touched` is the membership set pass B
    tests each line against; `vanished` needs no snapshot, only the graph."""
    touched: set[str] = field(default_factory=set)     # added + changed
    vanished: list[str] = field(default_factory=list)
    added: int = 0
    changed: int = 0
    prev_records: int = 0
    new_records: int = 0

    @property
    def total(self) -> int:
        return self.added + self.changed + len(self.vanished)


def _digest_rows(path: str) -> Iterator[tuple[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            link, _, digest = line.rstrip("\n").partition("\t")
            if link:
                yield link, digest


def diff_digests(prev_path: str, new_path: str) -> DiffResult:
    """Two-cursor merge over two sorted digest files.

    Both are byte-sorted, so this walks each exactly once and holds only the change
    set — the reason the whole design fits in ~50 MB whatever the register's size.
    """
    out = DiffResult()
    prev, new = _digest_rows(prev_path), _digest_rows(new_path)
    p, n = next(prev, None), next(new, None)
    while p is not None or n is not None:
        if n is None or (p is not None and p[0] < n[0]):
            out.vanished.append(p[0])
            out.prev_records += 1
            p = next(prev, None)
        elif p is None or n[0] < p[0]:
            out.touched.add(n[0])
            out.added += 1
            out.new_records += 1
            n = next(new, None)
        else:
            if p[1] != n[1]:
                out.touched.add(n[0])
                out.changed += 1
            out.prev_records += 1
            out.new_records += 1
            p, n = next(prev, None), next(new, None)
    return out


def churn_pct(diff: DiffResult) -> float:
    """Changes as a percentage of the previous register size."""
    base = diff.prev_records or diff.new_records
    return 0.0 if not base else diff.total / base * 100.0


#: However long the gap, never wave through a rewrite of this much of the register.
#: Scaling by the gap is right — a week's changes really are about seven days'
#: worth — but unbounded scaling disables the guard exactly when it matters, since
#: a month's gap would permit 150%. Measured against reality: 24 days of real
#: change is 1.26% (82,921 added, 114,933 changed, 319 vanished of 15.85M), so
#: ~0.05%/day. This cap is 500× that, and still catches a garbage file.
MAX_CHURN_CEILING_PCT = 25.0


def churn_allowed(diff: DiffResult, max_pct: float, days: int) -> tuple[bool, str]:
    """Whether this much movement is plausible for the gap it covers.

    A snapshot diff can rewrite the whole graph in one run if something upstream
    shifts — a schema change, a truncated file, a projection edit. The guard is
    scaled by the gap, because a week's changes really are about seven days' worth,
    but capped, because otherwise a long enough gap allows anything.
    """
    allowed = min(max_pct * max(1, days), MAX_CHURN_CEILING_PCT)
    pct = churn_pct(diff)
    if pct <= allowed:
        return True, f"{pct:.2f}% of {diff.prev_records:,} records"
    return False, (f"{pct:.2f}% of {diff.prev_records:,} records changed, over the "
                   f"{allowed:.2f}% allowed for a {days}-day gap "
                   f"({diff.added:,} added, {diff.changed:,} changed, "
                   f"{len(diff.vanished):,} vanished) — rerun with --force if this is real")


# ── the graph ─────────────────────────────────────────────────────────────────

_LEI_ID_PAGE = 20000


def existing_company_ids() -> set[str]:
    """Every `gb-coh:` Entity id in the graph — the gate for `only_existing`.

    Companies, not persons: a PSC person node exists only because of a PSC edge, so
    the controlled company is the thing to ask about. Paged by @rid for the reason
    `existing_lei_ids` is — an unpaged select over millions blows the query heap.
    """
    ids: set[str] = set()
    last: str | None = None
    while True:
        where = "WHERE id LIKE 'gb-coh:%'" + (f" AND @rid > {last}" if last else "")
        rows = run_sql(f"SELECT @rid AS rid, id FROM Entity {where} "
                       f"ORDER BY @rid LIMIT {_LEI_ID_PAGE}")
        if not rows:
            break
        ids.update(r["id"] for r in rows)
        last = rows[-1]["rid"]
        if len(rows) < _LEI_ID_PAGE:
            break
    return ids


def _claim_stmt(k: int, mapped, params: dict, now: str) -> str:
    """One batched UPSERT for the claim behind a PSC edge.

    The refresh previously touched only the edge, so a claim's `last_seen_at`
    stayed frozen at bulk-import time and a NEW appointment got no claim at
    all — while `close_vanished` diligently closed claims this path had never
    created. Same batching as the edge writes, for the same 60s-proxy reason.
    """
    from app.claims import claim_key, claim_props

    props = claim_props(
        kind=KIND_OWNS, from_id=mapped.owner_id, to_id=mapped.company_id,
        source_id=mapped.edge_props.get("source_id"),
        stake_percent=mapped.edge_props.get("stake_percent"),
        voting_power_pct=mapped.edge_props.get("voting_power_pct"),
        ownership_type=mapped.edge_props.get("ownership_type"),
        since=mapped.edge_props.get("since"),
        until=mapped.edge_props.get("until"),
        source_url=mapped.edge_props.get("source_url"),
        source_date=mapped.edge_props.get("source_date"),
        credibility_score=mapped.edge_props.get("credibility_score") or 97,
        filing_type=mapped.edge_props.get("filing_type"),
    )
    sets = []
    for name, value in props.items():
        pk = f"cl_{name}__{k}"
        params[pk] = value
        sets.append(f"{name} = :{pk}")
    key = claim_key(KIND_OWNS, mapped.owner_id, mapped.company_id,
                    props["source_id"])
    params[f"clkey__{k}"] = key
    return (f"UPDATE Claim SET {', '.join(sets)}, "
            f"first_seen_at = COALESCE(first_seen_at, :cl_last_seen_at__{k}) "
            f"UPSERT WHERE claim_key = :clkey__{k};")


class _PscEdgeWriter:
    """Buffers PSC edge writes and applies them without duplicating anything.

    The bulk importer's `CREATE EDGE` is not idempotent — a re-import doubles every
    edge, and the cleanup is a whole-database dedup pass. That is acceptable once,
    for a load into an empty graph; it is not acceptable nightly.

    GLEIF's per-record `_owns_edge_upsert` is the other tempting answer and is also
    wrong here: fine for its ~2k relationships a night, but PSC moves 10k–300k, and
    one-to-two round-trips each is hours behind a 60s proxy timeout.

    So: batch on the indexed `psc_self_link`. One `IN :links` probe per 1000 sorts
    the batch into updates and creates; updates go out as one script; only the rare
    creates need an edge built. Verified against a real ArcadeDB that an edge type
    is queryable and updatable by property — SQL cannot reach an edge through its
    endpoints, so without that this design would not exist.
    """

    def __init__(self, batch_size: int = _PROBE_CHUNK):
        self._batch = batch_size
        self._pending: list = []          # (mapped, owner_label)
        self.counts = {"created": 0, "updated": 0, "adopted": 0}

    def add(self, mapped) -> None:
        self._pending.append(mapped)
        if len(self._pending) >= self._batch:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        links = [m.self_link for m in self._pending]
        known = {r["psc_self_link"] for r in run_sql(
            "SELECT psc_self_link FROM OWNS WHERE psc_self_link IN :links", {"links": links})}

        updates = [m for m in self._pending if m.self_link in known]
        creates = [m for m in self._pending if m.self_link not in known]
        if updates:
            self._flush_updates(updates)
        for mapped in creates:
            self._create(mapped)
        self._pending.clear()

    def _flush_updates(self, batch: list) -> None:
        stmts, params = [], {}
        now = _now_iso()
        for k, m in enumerate(batch):
            props = {**m.edge_props, "last_scraped_at": now, "until_reason": None}
            sets = []
            for name, value in props.items():
                pk = f"{name}__{k}"
                params[pk] = value
                sets.append(f"{name} = :{pk}")
            params[f"link__{k}"] = m.self_link
            # Written unconditionally, never COALESCEd: a snapshot record is the
            # whole current truth about that appointment, so a correction that
            # removes `ceased_on` has to reopen the edge. GLEIF coalesces because
            # its delta records are partial statements; copying that here would
            # leave a corrected PSC closed forever.
            stmts.append(f"UPDATE OWNS SET {', '.join(sets)} WHERE psc_self_link = :link__{k};")
            stmts.append(_claim_stmt(k, m, params, now))
        _flush_script("\n".join(stmts), params)
        self.counts["updated"] += len(batch)

    def _create(self, mapped) -> None:
        # A live edge already on this pair means the same control stated under a new
        # appointment link — adopt it rather than opening a second one beside it, or
        # a later dedup pass deletes one and the next refresh recreates it forever.
        adopted = run_command(
            f"MATCH (a:{mapped.owner_label} {{id:$o}})-[r:OWNS]->(b:Entity {{id:$c}}) "
            "WHERE r.until IS NULL AND r.psc_self_link IS NOT NULL "
            "SET r.psc_self_link = $link RETURN r.psc_self_link AS l",
            {"o": mapped.owner_id, "c": mapped.company_id, "link": mapped.self_link})
        if adopted:
            self.counts["adopted"] += 1
            self._flush_updates([mapped])
            self.counts["updated"] -= 1      # counted as adopted, not as an update
            return
        cparams: dict = {}
        _flush_script(_claim_stmt(0, mapped, cparams, _now_iso()), cparams)
        props = {**mapped.edge_props, "last_scraped_at": _now_iso()}
        sets = ", ".join(f"{k} = :{k}" for k in props)
        run_sql(
            f"CREATE EDGE OWNS FROM (SELECT FROM {mapped.owner_label} WHERE id = :__from) "
            f"TO (SELECT FROM Entity WHERE id = :__to) SET {sets}",
            {**props, "__from": mapped.owner_id, "__to": mapped.company_id})
        self.counts["created"] += 1


def close_vanished(links: list[str], until: str, chunk: int = _PROBE_CHUNK) -> int:
    """Close edges whose snapshot record disappeared.

    Not a cessation: Companies House gives no date and states no reason, so this is
    a withdrawal or a correction. Marked as such — a ceased PSC really did hold
    control until its end date, a withdrawn one is the register saying the record
    was wrong, and an analysis that cannot tell them apart is a worse analysis.

    Dated with the **snapshot's** date rather than now(), so applying the same file
    twice, or a week late, produces the same graph.

    Nothing is deleted. The edge, the Person and the Claim all survive — deleting
    would also destroy the provenance of what the register once said.
    """
    closed = 0
    for i in range(0, len(links), chunk):
        batch = links[i:i + chunk]
        stmts, params = [], {}
        for k, link in enumerate(batch):
            params[f"l__{k}"] = link
            params[f"u__{k}"] = until
            params[f"n__{k}"] = _now_iso()
            stmts.append(
                f"UPDATE OWNS SET until = :u__{k}, until_reason = 'withdrawn', "
                f"last_scraped_at = :n__{k} WHERE psc_self_link = :l__{k} AND until IS NULL;")
        _flush_script("\n".join(stmts), params)
        # The Claim carries the same assertion and would otherwise still say the
        # holding is live — a close path that only touches the edge leaves the
        # provenance contradicting the graph.
        #
        # A Claim is keyed on (kind, from, to, source), not on the appointment link,
        # so it has to be reached through the edge's endpoints rather than by the
        # link directly. Read after the edge update, which is fine: the edge is the
        # only thing that knows which nodes a link connects, and closing it does not
        # move them.
        endpoints = run_command(
            "MATCH (a)-[r:OWNS]->(b) WHERE r.psc_self_link IN $links "
            "RETURN a.id AS from_id, b.id AS to_id, r.source_id AS source_id",
            {"links": batch})
        if endpoints:
            cstmts, cparams = [], {}
            for k, e in enumerate(endpoints):
                cparams.update({f"cf__{k}": e["from_id"], f"ct__{k}": e["to_id"],
                                f"cs__{k}": e.get("source_id"), f"cu__{k}": until,
                                f"ck__{k}": KIND_OWNS})
                cstmts.append(
                    f"UPDATE Claim SET until = :cu__{k} WHERE from_id = :cf__{k} "
                    f"AND to_id = :ct__{k} AND kind = :ck__{k} AND source_id = :cs__{k};")
            _flush_script("\n".join(cstmts), cparams)
        closed += len(batch)
    return closed


# ── state ─────────────────────────────────────────────────────────────────────

def mark_psc_load_done(scope: str = "full") -> None:
    """Record that a full `ch-psc` import has established a baseline.

    `scope` is "full" for a complete snapshot pass and "subset" for anything
    narrowed by --limit or --only. Only a full load justifies refreshing the whole
    register; a subset refreshes in only-existing mode, and the refusal can say why
    rather than claim PSC was never loaded.
    """
    run_sql("UPDATE ImportState SET key = :k, last_run_at = :now, scope = :scope, "
            "edge_key_version = :v UPSERT WHERE key = :k",
            {"k": _FULL_LOAD_KEY, "now": _now_iso(), "scope": scope, "v": PROJECTION_VERSION})


def psc_load_scope() -> str | None:
    rows = run_sql("SELECT scope FROM ImportState WHERE key = :k", {"k": _FULL_LOAD_KEY})
    return (rows[0].get("scope") or "full") if rows else None


def read_last_snapshot() -> dict | None:
    rows = run_sql("SELECT FROM ImportState WHERE key = :k", {"k": _STATE_KEY})
    return {k: v for k, v in rows[0].items() if not k.startswith("@")} if rows else None


def write_last_snapshot(snapshot_date_: str, records: int) -> None:
    run_sql("UPDATE ImportState SET key = :k, last_run_at = :now, snapshot_date = :d, "
            "record_count = :n, projection_version = :v UPSERT WHERE key = :k",
            {"k": _STATE_KEY, "now": _now_iso(), "d": snapshot_date_, "n": records,
             "v": PROJECTION_VERSION})


def days_since(iso_date: str | None) -> int:
    """Whole days between a stored snapshot date and today. 1 when unknown, so the
    churn guard neither widens nor divides by nothing."""
    if not iso_date:
        return 1
    try:
        then = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return 1
    return max(1, (date.today() - then).days)


# ── apply ─────────────────────────────────────────────────────────────────────

def apply_diff(filepath: str, diff: DiffResult, source_id: str, credibility_score: int,
               until_date: str, only_existing: bool = False,
               batch_size: int = _PROBE_CHUNK) -> dict:
    """Write the diff to the graph: touched records re-mapped, vanished ones closed."""
    counts = {"touched": 0, "not_here": 0, "skipped": 0, "errors": 0,
              "created": 0, "updated": 0, "adopted": 0, "closed": 0}
    known = existing_company_ids() if only_existing else None
    if known is not None:
        log.info("CH PSC refresh: only-existing over %s companies", f"{len(known):,}")

    nodes = _BatchWriter(batch_size=batch_size)
    edges = _PscEdgeWriter(batch_size=batch_size)
    raw, _, _ = _open_snapshot(filepath)
    bar = _ProgressBar("PSC refresh")
    seen = 0
    try:
        for line in raw:
            seen += 1
            if seen % 500000 == 0:
                bar.render(seen, 0)
            link = self_link_of(line)
            if link is None or link not in diff.touched:
                continue
            try:
                mapped = psc_record(_loads(line), source_id, credibility_score)
                if mapped is None:
                    counts["skipped"] += 1
                    continue
                if known is not None and mapped.company_id not in known:
                    counts["not_here"] += 1
                    continue
                nodes.entity(mapped.company_id, mapped.company_props)
                if mapped.kind_cat == "person":
                    nodes.person(mapped.owner_id, mapped.owner_props)
                else:
                    _write_entity_owner(nodes, mapped, source_id, credibility_score)
                counts["touched"] += 1
                # Nodes before edges: an edge create resolves its endpoints by id.
                nodes.flush()
                edges.add(mapped)
            except Exception as exc:  # noqa: BLE001 - one bad line mustn't abort
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("CH PSC refresh record error: %s", exc)
    finally:
        raw.close()
    nodes.flush()
    edges.flush()
    counts.update(edges.counts)

    if diff.vanished:
        counts["closed"] = close_vanished(diff.vanished, until_date, chunk=batch_size)
    bar.finish(f"{counts['touched']:,} applied, {counts['created']:,} created, "
               f"{counts['updated']:,} updated, {counts['closed']:,} closed"
               + (f", {counts['not_here']:,} not in this database" if known is not None else ""))
    log.info("CH PSC refresh done: %s", counts)
    return counts


def _write_entity_owner(batch: _BatchWriter, mapped, source_id: str, credibility_score: int) -> None:
    from app.scraper.bulk_import import _entity
    p = mapped.owner_props
    _entity(batch, mapped.owner_id, name=p["name"], entity_type=p["entity_type"],
            country=p["country"], founded=None, lei_id=None,
            companies_house_id=p["companies_house_id"], source_id=source_id,
            credibility_score=credibility_score, registered_address=p["registered_address"],
            hq_address=p["hq_address"], hq_city=p["hq_city"], hq_country=p["hq_country"])


def default_digest_path(filepath: str) -> str:
    return os.path.join(os.path.dirname(filepath) or ".", "psc-digest.tsv.gz")


def rotate_digest(new_path: str, live_path: str) -> None:
    """Install the new digest as the baseline, keeping one generation back.

    Only ever called after a clean apply. Rotating first would lose those changes
    permanently on a crash; rotating last leaves tomorrow's diff computed against
    the older digest — a superset, redone idempotently. That asymmetry is why the
    writes must be idempotent even though the diff itself is exact.

    One generation back is kept as `.prev`, so a run applied against the wrong
    baseline can be re-derived instead of requiring a full re-import.
    """
    if os.path.exists(live_path):
        os.replace(live_path, f"{live_path}.prev")
    os.replace(new_path, live_path)


def new_digest_tempfile(live_path: str) -> str:
    fd, path = tempfile.mkstemp(prefix="psc-digest-", suffix=".tsv.gz",
                                dir=os.path.dirname(live_path) or ".")
    os.close(fd)
    os.unlink(path)
    return path
