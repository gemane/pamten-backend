"""
GLEIF LEI-CDF (Level 1) succession importer.

The BODS export we import (``bods.py``) has no succession data. GLEIF records a
legal entity's succession/merger in the **LEI-CDF golden copy** instead: a record
whose registration is MERGED / DUPLICATE / RETIRED carries ``Entity.SuccessorEntity``
→ ``SuccessorLEI``, the surviving entity that replaced it.

This streams that (multi-GB) file and emits the same
``(predecessor)-[:SUCCEEDED_BY]->(successor)`` edge the Wikidata scraper creates,
keyed ``lei:{LEI}`` → ``lei:{SuccessorLEI}`` — exactly how ``bods._entity_node_id``
keys GLEIF entities, so both endpoints resolve by LEI with no matching logic.

LEI-CDF JSON quirks (verified against a real golden copy):
  * every scalar is wrapped as ``{"$": value}``,
  * ``SuccessorEntity`` is a repeating array,
  * the succession is signalled by a present ``SuccessorLEI`` (statuses seen:
    MERGED, DUPLICATE, RETIRED) — we key off the SuccessorLEI, not the status.

Format spec: https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-1-data-lei-cdf-3-1-format
"""
import logging
import os
import zipfile
from collections.abc import Iterator, Iterable
from typing import IO

import ijson

from app.scraper.bods import (
    _BatchWriter, _DiskMap, _now_iso, _ProgressBar, _ProgressStream,
)
from app.scraper.mapper import normalize_entity_name

log = logging.getLogger(__name__)


def _v(node: object) -> str | None:
    """Unwrap a LEI-CDF value: scalars are wrapped as {"$": value}."""
    if isinstance(node, dict):
        node = node.get("$")
    if node is None:
        return None
    return str(node).strip() or None


def _legal_name(rec: dict) -> str | None:
    return _v((rec.get("Entity") or {}).get("LegalName"))


def _successor_leis(rec: dict) -> list[str]:
    """LEIs of the entities that replaced this one (repeating SuccessorEntity)."""
    out: list[str] = []
    for se in (rec.get("Entity") or {}).get("SuccessorEntity") or []:
        if not isinstance(se, dict):
            continue
        slei = _v(se.get("SuccessorLEI"))
        if slei:
            out.append(slei)
    return out


def _pairs_from_record(rec: dict) -> list[tuple[str, str]]:
    """(predecessor_lei, successor_lei) pairs declared by one LEI record.
    Empty when the record has no LEI or no successor; self-references dropped."""
    lei = _v(rec.get("LEI"))
    if not lei:
        return []
    return [(lei, slei) for slei in _successor_leis(rec) if slei != lei]


def _iter_lei_records(stream: IO[bytes]) -> Iterator[dict]:
    """Yield each LEI record from a LEI-CDF golden-copy JSON (records array)."""
    yield from ijson.items(stream, "records.item")


def import_lei_cdf_succession(
    filepath: str,
    source_id: str,
    credibility_score: int,
    limit: int | None = None,
) -> dict:
    """
    Import GLEIF LEI-CDF succession into ``SUCCEEDED_BY`` edges.

    Args:
        filepath:          Local LEI-CDF golden-copy ``.json`` or ``.zip``.
        source_id:         Pamten GLEIF Source node id.
        credibility_score: Source credibility (0–100), stamped on each edge.
        limit:             Max records to scan (None = all).

    Returns dict: {records, pairs, nodes, errors}.
    """
    if filepath.lower().endswith(".zip"):
        zf = zipfile.ZipFile(filepath)
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise ValueError(f"No .json file found inside ZIP: {filepath}")
        entry = names[0]
        total_bytes = zf.getinfo(entry).file_size
        raw: IO[bytes] = zf.open(entry)
        log.info("LEI-CDF: reading %s (%s bytes uncompressed)", entry, f"{total_bytes:,}")
    else:
        total_bytes = os.path.getsize(filepath)
        raw = open(filepath, "rb")  # noqa: WPS515

    name_map = _DiskMap()          # lei -> legal name (for successor labels)
    pairs: list[tuple[str, str]] = []  # (predecessor_lei, successor_lei)
    records = 0
    errors = 0
    try:
        bar = _ProgressBar("LEI-CDF")
        stream = _ProgressStream(raw, total_bytes, bar)
        for rec in _iter_lei_records(stream):
            if limit and records >= limit:
                break
            records += 1
            try:
                lei = _v(rec.get("LEI"))
                if not lei:
                    continue
                if name := _legal_name(rec):
                    name_map[lei] = name
                pairs.extend(_pairs_from_record(rec))
            except Exception as exc:  # noqa: BLE001 - one bad record mustn't abort
                errors += 1
                if errors <= 5:
                    log.warning("LEI-CDF record error: %s", exc)
        bar.finish(f"{records:,} records, {len(pairs):,} succession links")

        nodes = _write(pairs, name_map, source_id, credibility_score)
    finally:
        name_map.close()
        raw.close()

    result = {"records": records, "pairs": len(pairs), "nodes": nodes, "errors": errors}
    log.info("LEI-CDF succession import done: %s", result)
    return result


def _write(pairs: Iterable[tuple[str, str]], name_map: _DiskMap,
           source_id: str, credibility_score: int) -> int:
    """Upsert both endpoints (non-clobbering: only name/lei/source) and the edge."""
    batch = _BatchWriter()
    seen: set[str] = set()

    def _node(lei: str) -> None:
        if lei in seen:
            return
        seen.add(lei)
        props = {"lei_id": lei, "source_id": source_id}
        if nm := name_map.get(lei):
            props |= {"name": nm, "name_normalized": normalize_entity_name(nm), "search_text": nm}
        batch.entity(f"lei:{lei}", props)

    for pred, succ in pairs:
        _node(pred)
        _node(succ)
        batch.succeeded_by(f"lei:{pred}", f"lei:{succ}", {
            "since": None,
            "source_id": source_id,
            "credibility_score": credibility_score,
            "source_url": f"https://search.gleif.org/#/record/{pred}",
            "last_scraped_at": _now_iso(),
        })
    batch.flush()
    return len(seen)
