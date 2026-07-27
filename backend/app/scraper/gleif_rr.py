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
same keying as ``bods._entity_node_id`` — carrying ``direct_or_indirect`` so the
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

from app.scraper.bods import _BatchWriter, _owns, _ProgressBar, _ProgressStream

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


def _rr_edge(rec: dict) -> tuple[str, str, str] | None:
    """(parent_lei, child_lei, direct_or_indirect) for an active consolidation
    relationship, else None."""
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
    return parent, child, marker


def import_rr_cdf(filepath: str, source_id: str, credibility_score: int,
                  limit: int | None = None) -> dict:
    """
    Import GLEIF RR-CDF consolidation relationships as direct/indirect OWNS edges.

    Args:
        filepath:          Local RR-CDF golden-copy ``.json`` or ``.zip``.
        source_id:         Pamten GLEIF Source node id.
        credibility_score: Source credibility (0–100).
        limit:             Max records to scan (None = all).

    Returns dict: {records, direct, indirect, skipped, nodes, edges}.
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

    try:
        bar = _ProgressBar("RR-CDF")
        stream = _ProgressStream(raw, total_bytes, bar)
        for rec in ijson.items(stream, "relations.item"):
            if limit and counts["records"] >= limit:
                break
            counts["records"] += 1
            edge = _rr_edge(rec)
            if not edge:
                counts["skipped"] += 1
                continue
            parent, child, marker = edge
            counts["direct" if marker == "direct" else "indirect"] += 1
            _node(parent)
            _node(child)
            _owns(
                batch, owner_id=f"lei:{parent}", owned_id=f"lei:{child}",
                stake_percent=None, ownership_type="controlling",
                since=None, until=None, source_id=source_id,
                credibility_score=credibility_score,
                source_url=f"https://search.gleif.org/#/record/{child}",
                interest_types=["accountingConsolidation"], direct_or_indirect=marker,
            )
        batch.flush()
        bar.finish(f"{counts['records']:,} records, "
                   f"{counts['direct']:,} direct + {counts['indirect']:,} indirect edges")
    finally:
        raw.close()

    result = {**counts, "nodes": len(seen_nodes),
              "edges": counts["direct"] + counts["indirect"]}
    log.info("RR-CDF import done: %s", result)
    return result
