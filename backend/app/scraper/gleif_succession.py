"""
GLEIF LEI-CDF (Level 1) succession importer.

The LEI-CDF entity + RR-CDF relationship imports carry no succession data. GLEIF records a
legal entity's succession/merger in the **LEI-CDF golden copy** instead: a record
whose registration is MERGED / DUPLICATE / RETIRED carries ``Entity.SuccessorEntity``
→ ``SuccessorLEI``, the surviving entity that replaced it.

This streams that (multi-GB) file and emits the same
``(predecessor)-[:SUCCEEDED_BY]->(successor)`` edge the Wikidata scraper creates,
keyed ``lei:{LEI}`` → ``lei:{SuccessorLEI}`` — the same `lei:{LEI}` key the other
GLEIF imports use, so both endpoints resolve by LEI with no matching logic.

LEI-CDF JSON quirks (verified against a real golden copy):
  * every scalar is wrapped as ``{"$": value}``,
  * ``SuccessorEntity`` is a repeating array,
  * the succession is signalled by a present ``SuccessorLEI`` (statuses seen:
    MERGED, DUPLICATE, RETIRED) — we key off the SuccessorLEI, not the status.

Format spec: https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-1-data-lei-cdf-3-1-format
"""
import json
import logging
import os
import re
import zipfile
from collections.abc import Iterator, Iterable
from typing import IO

import ijson

from app.scraper.bulk_import import (
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


#: Registration statuses whose SuccessorLEI is not a succession (findings item
#: 88): DUPLICATE — a second registration of the SAME entity pointing at its
#: live record (6,518 of the 35,346 records naming a successor in the 2026-11-08
#: golden copy; Stanbic IBTC Bank, Ecobank Nigeria, the Volkswagen AG and
#: Naspers duplicates on dev) — and ANNULLED, a registration found erroneous
#: pointing at the right one (93). GLEIF's own meaning: "this LEI is not a
#: legal entity's record, that one is", not "this entity ceased and that one
#: followed". Read as successions they minted the duplicate as a predecessor
#: node, the register_id merge folded it into the live one, and a self-loop
#: claim stayed behind (3 on dev after the 2026-11-08b rebuild). The same set
#: `legal_identity.GLEIF_NOT_THE_ENTITY` names for joins, less the statuses
#: of an entity that did end (RETIRED, MERGED).
NOT_A_SUCCESSION = frozenset({"DUPLICATE", "ANNULLED"})


def _registration_status(rec: dict) -> str | None:
    return _v((rec.get("Registration") or {}).get("RegistrationStatus"))


def _pairs_from_record(rec: dict) -> list[tuple[str, str]]:
    """(predecessor_lei, successor_lei) pairs declared by one LEI record.
    Empty when the record has no LEI or no successor, when its registration
    is a DUPLICATE or ANNULLED one (`NOT_A_SUCCESSION`); self-references dropped."""
    lei = _v(rec.get("LEI"))
    if not lei or (_registration_status(rec) or "").upper() in NOT_A_SUCCESSION:
        return []
    return [(lei, slei) for slei in _successor_leis(rec) if slei != lei]


def _iter_lei_records(stream: IO[bytes]) -> Iterator[dict]:
    """Yield each LEI record from a LEI-CDF golden-copy JSON (records array)."""
    yield from ijson.items(stream, "records.item")


_SUCCESSOR_RE = re.compile(rb'"SuccessorLEI"\s*:\s*\{\s*"\$"\s*:\s*"([0-9A-Z]{20})"')


def _iter_succession_records(raw: IO[bytes], targets: set[str], chunk_size: int = 8 << 20) -> Iterator[dict]:
    """The records that declare a successor AND touch ``targets`` — as the
    record's own LEI (a company of ours that was replaced) or as a successor
    (a company of ours that replaced another). The whole stream is read: a
    predecessor can sit anywhere in the file, so unlike the entity importer's
    scan this one cannot stop early — but it parses only the hits. A byte
    scan finds the record starts (`gleif_lei_cdf._LEI_RECORD_RE`); a record
    without the bytes "SuccessorLEI" is skipped unparsed (3.4 M of 3.5 M)."""
    from app.scraper.gleif_lei_cdf import _LEI_RECORD_RE, _extract_json_object
    want = {t.encode() for t in targets}
    buf = b""
    while True:
        chunk = raw.read(chunk_size)
        if chunk:
            buf += chunk
        starts = list(_LEI_RECORD_RE.finditer(buf))
        consumed = 0
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else None
            if end is None and chunk:
                break                                   # the last record may run past buf
            body = buf[m.start():end]
            if b'"SuccessorLEI"' in body and (
                    m.group(1) in want or any(x.group(1) in want for x in _SUCCESSOR_RE.finditer(body))):
                obj = _extract_json_object(buf, m.start())
                if obj is None:
                    break
                try:
                    yield json.loads(obj)
                except ValueError:
                    pass
            consumed = end if end is not None else len(buf)
        buf = buf[consumed:]
        if not chunk:
            break


def import_lei_cdf_succession(
    filepath: str,
    source_id: str,
    credibility_score: int,
    limit: int | None = None,
    only_leis: set[str] | None = None,
    emit_leis_path: str | None = None,
) -> dict:
    """
    Import GLEIF LEI-CDF succession into ``SUCCEEDED_BY`` edges.

    Args:
        filepath:          Local LEI-CDF golden-copy ``.json`` or ``.zip``.
        source_id:         Owlgraph GLEIF Source node id.
        credibility_score: Source credibility (0–100), stamped on each edge.
        limit:             Max records to scan (None = all).
        only_leis:         The curated subset (the test import's seeds and their
                           family): only the successions that touch one of them
                           are written — the full file states 53,027 and
                           `_write` mints both endpoints, so a full pass into a
                           curated database would add ~100k bare nodes
                           (findings item 85b). Until 2026-11-08 no chain ran
                           this importer at all and a rebuilt database held no
                           SUCCEEDED_BY edge.
        emit_leis_path:    With ``only_leis``: write the endpoints the subset did
                           not already hold (one per line) here, so a follow-up
                           ``gleif-lei-cdf --only-file`` names them fully — the
                           predecessor a succession adds is a bare node otherwise.

    Returns dict: {records, pairs, nodes, errors, not_a_succession[, emitted]} —
    ``not_a_succession`` the DUPLICATE / ANNULLED registrations skipped (item 88).
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
    not_a_succession = 0           # DUPLICATE / ANNULLED registrations naming their live record (item 88)
    try:
        bar = _ProgressBar("LEI-CDF")
        stream = _ProgressStream(raw, total_bytes, bar)
        record_iter = _iter_succession_records(stream, only_leis) if only_leis is not None \
            else _iter_lei_records(stream)
        for rec in record_iter:
            if limit and records >= limit:
                break
            records += 1
            try:
                lei = _v(rec.get("LEI"))
                if not lei:
                    continue
                if name := _legal_name(rec):
                    name_map[lei] = name
                if (_registration_status(rec) or "").upper() in NOT_A_SUCCESSION and _successor_leis(rec):
                    not_a_succession += 1
                    continue
                pairs.extend(_pairs_from_record(rec))
            except Exception as exc:  # noqa: BLE001 - one bad record mustn't abort
                errors += 1
                if errors <= 5:
                    log.warning("LEI-CDF record error: %s", exc)
        bar.finish(f"{records:,} records, {len(pairs):,} succession links")

        nodes = _write(pairs, name_map, source_id, credibility_score)
        emitted = None
        if emit_leis_path is not None and only_leis is not None:
            new_ends = sorted({x for pair in pairs for x in pair} - set(only_leis))
            with open(emit_leis_path, "w", encoding="utf-8") as fh:
                fh.write("".join(f"{x}\n" for x in new_ends))
            emitted = len(new_ends)
    finally:
        name_map.close()
        raw.close()

    result = {"records": records, "pairs": len(pairs), "nodes": nodes, "errors": errors,
              "not_a_succession": not_a_succession}
    if emitted is not None:
        result["emitted"] = emitted
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
