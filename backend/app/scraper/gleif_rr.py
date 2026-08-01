"""
GLEIF RR-CDF (Level 2 Relationship Record) importer — the direct-vs-indirect
parent hierarchy the BODS export flattens away.

The GLEIF BODS export we ingest carries parent relationships with
``directOrIndirect = "unknown"``. The native **RR-CDF** golden copy instead states
the relationship explicitly per record:

  * ``IS_DIRECTLY_CONSOLIDATED_BY``   → the *direct* parent (closest consolidator)
  * ``IS_ULTIMATELY_CONSOLIDATED_BY`` → the *ultimate* parent (top of the tree) =
    the *indirect* relationship
  * fund/branch types (IS_FUND-MANAGED_BY, IS_SUBFUND_OF, IS_FEEDER_TO,
    IS_INTERNATIONAL_BRANCH_OF) — not consolidation ownership; skipped for now.

Each record is ``StartNode`` (child LEI) reported-as-consolidated-by ``EndNode``
(parent LEI), so we emit ``(parent)-[:OWNS]->(child)`` keyed ``lei:{LEI}`` — the
same key the LEI-CDF importer uses — carrying ``direct_or_indirect`` so the
direct holding can be told apart from the ultimate/indirect control summary
(which is what lets downstream stop double-counting toward >100%).

JSON quirks (verified): top-level array key ``relations``; each item is
``{"RelationshipRecord": {"Relationship": {...}, "Registration": {...}}}``; every
scalar is wrapped ``{"$": value}``.

Format spec: https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-2-data-relationship-record-rr-cdf-2-1-format
"""
import logging
import os
import zipfile
from typing import IO

import ijson

from app.scraper.bulk_import import _BatchWriter, _owns, _ProgressBar, _ProgressStream

log = logging.getLogger(__name__)

# RR RelationshipType → our direct/indirect marker (only consolidation ownership).
_CONSOLIDATION = {
    "IS_DIRECTLY_CONSOLIDATED_BY":   "direct",
    "IS_ULTIMATELY_CONSOLIDATED_BY": "indirect",
}


def _v(node: object) -> str | None:
    """Unwrap an RR-CDF value: scalars are wrapped as {"$": value}."""
    if isinstance(node, dict):
        node = node.get("$")
    return str(node).strip() if node is not None and str(node).strip() else None


def _node_lei(node: dict | None) -> str | None:
    """The LEI of a Start/End node (only when the node is identified by LEI)."""
    node = node or {}
    if _v(node.get("NodeIDType")) not in (None, "LEI"):
        return None
    return _v(node.get("NodeID"))


def _iso_date(value: str | None) -> str | None:
    """A GLEIF timestamp (``2023-05-17T00:00:00.000Z``) as a plain date (``2023-05-17``)."""
    return value[:10] if value and len(value) >= 10 else None


def _relationship_dates(rel: dict) -> tuple[str | None, str | None]:
    """(since, until) — when the ownership relationship began and (if ended) ended —
    from the ``RELATIONSHIP_PERIOD``. Accounting / document-filing periods are ignored.
    ``RelationshipPeriod`` is an object *or* a list in the CDF."""
    periods = (rel.get("RelationshipPeriods") or {}).get("RelationshipPeriod")
    if isinstance(periods, dict):
        periods = [periods]
    for p in periods or []:
        if _v((p or {}).get("PeriodType")) == "RELATIONSHIP_PERIOD":
            return _iso_date(_v(p.get("StartDate"))), _iso_date(_v(p.get("EndDate")))
    return None, None


def _rr_edge(rec: dict) -> tuple[str, str, str, str | None, str | None] | None:
    """(parent_lei, child_lei, direct_or_indirect, since, until) for an active
    consolidation relationship, else None. ``since``/``until`` come from the
    RELATIONSHIP_PERIOD (the ownership start/end date), when present."""
    rel = (rec.get("RelationshipRecord") or {}).get("Relationship") or {}
    marker = _CONSOLIDATION.get(_v(rel.get("RelationshipType")))
    if not marker:
        return None
    if _v(rel.get("RelationshipStatus")) not in (None, "ACTIVE"):
        return None
    child = _node_lei(rel.get("StartNode"))
    parent = _node_lei(rel.get("EndNode"))
    if not child or not parent or child == parent:
        return None
    since, until = _relationship_dates(rel)
    return parent, child, marker, since, until


def _family_of(seeds: set[str], children: dict, parents: dict) -> set[str]:
    """The corporate family of `seeds`: the seeds plus everything reachable DOWN
    (all descendants via child edges) and UP (all ancestors via parent edges). This
    is the whole vertical tree — bottom to top — around each seed."""
    family = set(seeds)
    down = list(seeds)
    while down:                                   # descendants
        for c in children.get(down.pop(), ()):
            if c not in family:
                family.add(c)
                down.append(c)
    up = list(seeds)
    while up:                                     # ancestors
        for p in parents.get(up.pop(), ()):
            if p not in family:
                family.add(p)
                up.append(p)
    return family


def import_rr_cdf(filepath: str, source_id: str, credibility_score: int,
                  limit: int | None = None, only_leis: set[str] | None = None,
                  emit_leis_path: str | None = None) -> dict:
    """
    Import GLEIF RR-CDF consolidation relationships as direct/indirect OWNS edges.

    Args:
        filepath:          Local RR-CDF golden-copy ``.json`` or ``.zip``.
        source_id:         Owlgraph GLEIF Source node id.
        credibility_score: Source credibility (0–100).
        limit:             Max records to scan (None = all).
        only_leis:         Restrict to the corporate FAMILY of these seed LEIs — every
                           edge among the seeds, their ancestors and their descendants —
                           for a connected test subset. The RR file is small (~34MB), so
                           this loads all edges once and walks the tree in memory.
        emit_leis_path:    When set (with only_leis), write the family's LEIs (one per
                           line) here, so a follow-up ``gleif-lei-cdf --only-file`` can
                           name them (RR edges alone leave the counterparties unnamed).

    Returns dict: {records, direct, indirect, skipped, nodes, edges[, family]}.
    """
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise ValueError(f"No .json file found inside ZIP: {filepath}")
        entry = names[0]
        total_bytes = zf.getinfo(entry).file_size
        raw: IO[bytes] = zf.open(entry)
        log.info("RR-CDF: reading %s (%s bytes uncompressed)", entry, f"{total_bytes:,}")
    else:
        total_bytes = os.path.getsize(filepath)
        raw = open(filepath, "rb")  # noqa: WPS515

    batch = _BatchWriter()
    seen_nodes: set[str] = set()
    counts = {"records": 0, "direct": 0, "indirect": 0, "skipped": 0}

    def _node(lei: str) -> None:
        if lei not in seen_nodes:
            seen_nodes.add(lei)
            # Non-clobbering: ensure the node exists keyed by LEI, don't touch
            # name/type (GLEIF BODS/LEI-CDF imports own those).
            batch.entity(f"lei:{lei}", {"lei_id": lei, "source_id": source_id})

    def _emit_edge(parent: str, child: str, marker: str,
                   since: str | None = None, until: str | None = None) -> None:
        counts["direct" if marker == "direct" else "indirect"] += 1
        _node(parent)
        _node(child)
        _owns(
            batch, owner_id=f"lei:{parent}", owned_id=f"lei:{child}",
            stake_percent=None, ownership_type="controlling",
            since=since, until=until, source_id=source_id,
            credibility_score=credibility_score,
            source_url=f"https://search.gleif.org/#/record/{child}",
            interest_types=["accountingConsolidation"], direct_or_indirect=marker,
        )

    family: set[str] | None = None
    try:
        bar = _ProgressBar("RR-CDF")
        stream = _ProgressStream(raw, total_bytes, bar)
        if only_leis is not None:
            # Load all edges once, keep only the seeds' family (ancestors + descendants).
            from collections import defaultdict
            edges: list[tuple[str, str, str]] = []
            children: dict[str, list[str]] = defaultdict(list)
            parents: dict[str, list[str]] = defaultdict(list)
            for rec in ijson.items(stream, "relations.item"):
                if limit and counts["records"] >= limit:
                    break
                counts["records"] += 1
                edge = _rr_edge(rec)
                if not edge:
                    counts["skipped"] += 1
                    continue
                parent, child, marker, _since, _until = edge
                edges.append(edge)
                children[parent].append(child)
                parents[child].append(parent)
            family = _family_of(only_leis, children, parents)
            for parent, child, marker, since, until in edges:
                if parent in family and child in family:
                    _emit_edge(parent, child, marker, since, until)
        else:
            for rec in ijson.items(stream, "relations.item"):
                if limit and counts["records"] >= limit:
                    break
                counts["records"] += 1
                edge = _rr_edge(rec)
                if not edge:
                    counts["skipped"] += 1
                    continue
                _emit_edge(*edge)
        batch.flush()
        bar.finish(f"{counts['records']:,} records, "
                   f"{counts['direct']:,} direct + {counts['indirect']:,} indirect edges")
    finally:
        raw.close()

    if emit_leis_path and family is not None:
        with open(emit_leis_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(family)) + "\n")
        log.info("RR-CDF: wrote %d family LEIs to %s", len(family), emit_leis_path)

    result = {**counts, "nodes": len(seen_nodes),
              "edges": counts["direct"] + counts["indirect"]}
    if family is not None:
        result["family"] = len(family)
    log.info("RR-CDF import done: %s", result)
    return result
