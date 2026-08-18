"""
GLEIF reporting exceptions — why a company reports no parent.

`repex` is GLEIF's own abbreviation for *reporting exceptions*, and it names the
golden copy's third file. The obligation behind it is what makes the data exist:
every LEI holder must report its parent company, and if it will not or cannot, it
must file a **reason why not**. Silence is not an allowed answer.

So GLEIF's Level 2 has three states, and until this importer the graph could only
see two of them:

* **A parent, named.** An `OWNS` edge, from the RR file.
* **A reason, and no parent.** The company declined, formally and in public, and
  said why. Evidence of absence rather than absence of evidence.
* **Nothing at all.** Genuinely unlooked-at.

The middle one is what was being lost — folded in with the third. GITHUB INDIA
PRIVATE LIMITED is the example to hold in mind: no edge, an obvious parent, and
GLEIF holding its declaration that the parent's accounts are not published
(`NON_PUBLIC`). "Nobody has looked" and "a parent exists and is withheld" are
different answers to a user asking who owns it.

The reason is often the more interesting fact for an ownership map. A company
whose parent is `NATURAL_PERSONS` is telling us the trail leads to people rather
than to another company — the exact point where GLEIF stops and the beneficial
ownership registers (UK PSC, SEC) take over. `NON_CONSOLIDATING` says the parent
exists but does not consolidate the accounts, so GLEIF's accounting-based Level 2
would never carry it whatever we did. `NO_LEI` says the parent is real and simply
outside the system, and sometimes names it in `ExceptionReference`.

Two categories are reported separately, because they are separate questions —
the direct (closest) consolidating parent and the ultimate (top of tree) one:

  * ``DIRECT_ACCOUNTING_CONSOLIDATION_PARENT``   → ``no_direct_parent_reason``
  * ``ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT`` → ``no_ultimate_parent_reason``

Most companies filing one file both (1,306 of 2,986 on a day's delta), and the
reasons can differ: the direct parent may be a natural person while the ultimate
one simply has no LEI.

**Nothing is ever created here.** A repex record is a statement *about* a
company, and there are hundreds of thousands of them — including for companies
this database has never heard of. Writing them all would fill the graph with
nodes whose only content is "has no parent, because". So the writes are plain
``UPDATE ... WHERE id`` with no ``UPSERT``: a record for a company we do not
carry updates nothing and is counted as `not_here`.

Reasons are kept as GLEIF's own enum values, joined by ", " when a record gives
more than one (rare — 6 of 2,986). The UI turns them into prose; the graph keeps
what the source said.

Repex JSON quirks: array key `exceptions`; scalars wrapped `{"$": value}` as
elsewhere in the CDF — but ``ExceptionReason`` and ``ExceptionReference`` are
**lists** of those wrappers, even when they hold one item.
"""
import logging
import os
import zipfile
from typing import IO, Iterator

import ijson

from app.scraper.bulk_import import _flush_script, _ProgressBar, _ProgressStream
from app.scraper.gleif_succession import _v

log = logging.getLogger(__name__)

# GLEIF's two parent questions → the property that records why each went
# unanswered. A category outside this pair is ignored rather than guessed at.
CATEGORY_PROPS = {
    "DIRECT_ACCOUNTING_CONSOLIDATION_PARENT": "no_direct_parent_reason",
    "ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT": "no_ultimate_parent_reason",
}

#: Every reason GLEIF publishes, in rough order of how often it appears. Not used
#: to filter — an unknown reason is stored as-is, since the list has grown before
#: (``NO_KNOWN_PERSON`` is newer than the original schema) and dropping a reason
#: we do not recognise would be worse than carrying it.
KNOWN_REASONS = (
    "NATURAL_PERSONS",          # the parent is a person, not a company
    "NON_CONSOLIDATING",        # a parent exists but does not consolidate accounts
    "NO_LEI",                   # the parent has no LEI (sometimes named in the reference)
    "NO_KNOWN_PERSON",          # no person or entity controls it
    "NON_PUBLIC",               # the accounts are not published
    "CONSENT_NOT_OBTAINED",     # the parent has not agreed to be named
    "LEGAL_OBSTACLES",          # naming it would break a local law
    "BINDING_LEGAL_COMMITMENTS",
    "DETRIMENT_NOT_EXCLUDED",   # harm to either party cannot be ruled out
    "DISCLOSURE_DETRIMENTAL",   # naming it would harm one of them
)

_BATCH = 400


def _values(field) -> list[str]:
    """The strings in a repex field, which is a list of ``{"$": …}`` wrappers.

    Tolerates the bare object and the bare string too. The CDF has moved fields
    between those shapes before, and a parser that only knows the current one
    turns a schema change into silent data loss rather than an error.
    """
    if field is None:
        return []
    items = field if isinstance(field, list) else [field]
    out = []
    for item in items:
        # `_v` already strips and returns None for a blank; the bare-string branch
        # has to do its own, or a field of spaces becomes an empty "reason".
        value = _v(item) if isinstance(item, dict) else str(item).strip()
        if value:
            out.append(value)
    return out


def _exception_props(rec: dict) -> tuple[str, dict] | None:
    """``(node_id, props)`` for one exception record, or None when it carries no
    LEI, no recognised category or no reason — all three are needed for the
    statement to mean anything."""
    lei = _v(rec.get("LEI"))
    prop = CATEGORY_PROPS.get(_v(rec.get("ExceptionCategory")) or "")
    reasons = _values(rec.get("ExceptionReason"))
    if not lei or not prop or not reasons:
        return None
    props = {prop: ", ".join(reasons)}
    # A reference is where the filer points at the parent it did not name — a
    # register entry, usually, for a parent with no LEI. Rare (7 of 2,986) and
    # worth keeping: it is the only lead the record offers.
    references = _values(rec.get("ExceptionReference"))
    if references:
        props[f"{prop}_reference"] = ", ".join(references)
    return f"lei:{lei}", props


def _iter_exception_records(stream: IO[bytes]) -> Iterator[dict]:
    """Yield each record from a repex golden-copy JSON (``exceptions`` array)."""
    yield from ijson.items(stream, "exceptions.item")


def _open_json(filepath: str) -> tuple[IO[bytes], int]:
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        entry = next(n for n in zf.namelist() if n.lower().endswith(".json"))
        return zf.open(entry), zf.getinfo(entry).file_size
    return open(filepath, "rb"), os.path.getsize(filepath)  # noqa: WPS515


def _flush(buf: list[tuple[str, dict]]) -> int:
    """Apply buffered updates, returning how many hit a company we hold.

    ``UPDATE … WHERE id`` **without** ``UPSERT``, which is the whole safety
    property of this importer: a statement about a company we do not carry
    updates nothing instead of conjuring a node for it.

    The batch ends with a ``SELECT count(*) … WHERE id IN`` over the same ids,
    because an ArcadeDB script returns only its **last** statement's result — the
    per-``UPDATE`` row counts never come back, so counting the hits means asking
    for them. One extra statement per 400, on an indexed id. The ids must be
    distinct for that count to mean anything, which is why the caller merges a
    company's two exceptions before they get here.
    """
    if not buf:
        return 0
    stmts, params = [], {}
    for k, (node_id, props) in enumerate(buf):
        sets = []
        for name, value in props.items():
            pk = f"{name}__{k}"
            params[pk] = value
            sets.append(f"{name} = :{pk}")
        params[f"id__{k}"] = node_id
        stmts.append(f"UPDATE Entity SET {', '.join(sets)} WHERE id = :id__{k};")
    params["hit_ids"] = [node_id for node_id, _ in buf]
    stmts.append("SELECT count(*) AS applied FROM Entity WHERE id IN :hit_ids;")
    rows = _flush_script("\n".join(stmts), params) or []
    return int(rows[0].get("applied") or 0) if rows else 0


def import_repex(filepath: str, limit: int | None = None) -> dict:
    """Apply a GLEIF reporting-exceptions file (full copy or delta) to the graph.

    Idempotent: each record sets a property on one existing entity, so re-running
    a file — or applying a delta twice — lands on the same state. Nothing is
    created, and nothing is deleted: an exception GLEIF withdraws is superseded
    when the parent relationship it was standing in for arrives in the RR file.

    Returns ``{records, writes, applied, not_here, skipped, errors}`` — `records`
    counts exception statements and `writes` the updates they became. The two
    differ when a company's direct and ultimate exceptions land in the same batch
    and merge into one update; in the full golden copy they never do (it is not
    ordered by LEI), so there the two are equal.
    """
    raw, total = _open_json(filepath)
    counts = {"records": 0, "writes": 0, "applied": 0, "not_here": 0,
              "skipped": 0, "errors": 0}
    # Keyed by company: two records about one node must not become two ids in a
    # batch, because the flush counts its hits with one `IN` over them and a
    # repeated id would be counted once. Most filers do state both categories,
    # though in the full copy the pair rarely lands in the same batch — it is not
    # ordered by LEI — so this mostly earns its keep on the deltas.
    buf: dict[str, dict] = {}
    bar = _ProgressBar("GLEIF repex")

    def flush() -> None:
        pending = list(buf.items())
        applied = _flush(pending)
        counts["writes"] += len(pending)
        counts["applied"] += applied
        counts["not_here"] += len(pending) - applied
        buf.clear()

    try:
        for rec in _iter_exception_records(_ProgressStream(raw, total, bar)):
            if limit and counts["records"] >= limit:
                break
            counts["records"] += 1
            try:
                built = _exception_props(rec)
                if not built:
                    counts["skipped"] += 1
                    continue
                node_id, props = built
                buf.setdefault(node_id, {}).update(props)
                if len(buf) >= _BATCH:
                    flush()
            except Exception as exc:  # noqa: BLE001 - one bad record mustn't abort
                counts["errors"] += 1
                if counts["errors"] <= 5:
                    log.warning("repex record error: %s", exc)
        flush()
        bar.finish(f"{counts['records']:,} exceptions, {counts['applied']:,} applied, "
                   f"{counts['not_here']:,} not in this database")
    finally:
        raw.close()
    log.info("GLEIF repex done: %s", counts)
    return counts
