"""
BODS (Beneficial Ownership Data Standard) v0.4 importer for Pamten.

Imports GLEIF and UK PSC datasets published by Open Ownership.
Both datasets are CC0 licensed — fully open, no restrictions.

Datasets:
  GLEIF:   https://oo-bodsdata.s3.amazonaws.com/data/gleif_version_0_4/json.zip   (~1.1 GB)
  UK PSC:  https://oo-bodsdata.s3.amazonaws.com/data/uk_version_0_4/json.zip      (~3.3 GB)

Data licence: CC0 1.0 (compatible with ODbL — see DATA_LICENSE.md)

Processing strategy:
  Single streaming pass through the file.
  Entity and person statements are written to the DB immediately.
  Relationship statements are buffered in memory (they are smaller than
  entity/person records) and processed in a second pass once all nodes
  are known, so forward-references resolve correctly.

  For very large datasets use filter_jurisdiction (e.g. "GB") or limit
  to constrain memory and runtime.
"""

import logging
import os
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from typing import IO

import httpx
import ijson

from app.database import db
from app.scraper.mapper import derive_ownership_type, normalize_entity_name, parse_full_name

log = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MB per download chunk

# ── Interest type → Pamten ownership_type ─────────────────────────────────────
# None means "derive from stake_percent via derive_ownership_type()".
# "role" signals a HAS_ROLE edge rather than an OWNS edge.

_INTEREST_OWNERSHIP_TYPE: dict[str, str | None] = {
    "shareholding":                     None,           # derive from stake %
    "votingRights":                     "controlling",
    "appointmentOfBoard":               "controlling",
    "otherInfluenceOrControl":          "controlling",
    "seniorManagingOfficial":           "role",         # → HAS_ROLE
    "trustee":                          "controlling",
    "settlor":                          "partnership",
    "beneficiaryOfLegalArrangement":    "minority",
}

# ── BODS entityType → Pamten entity type ──────────────────────────────────────

_ENTITY_TYPE_MAP: dict[str, str] = {
    "registeredEntity": "company",
    "legalEntity":      "company",
    "arrangement":      "holding",
    "anonymousEntity":  "company",
    "unknownEntity":    "company",
}


# ── Database helpers ──────────────────────────────────────────────────────────

def _upsert_entity_bods(
    name: str,
    entity_type: str,
    country: str | None,
    founded: int | None,
    lei_id: str | None,
    companies_house_id: str | None,
    source_id: str,
    credibility_score: int,
) -> str:
    """Find or create an Entity node, updating identifiers if found."""
    name_norm = normalize_entity_name(name)
    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (e:Entity)
            WHERE ($lei IS NOT NULL AND e.lei_id = $lei)
               OR ($ch  IS NOT NULL AND e.companies_house_id = $ch)
               OR e.name = $name
               OR e.name_normalized = $name_norm
            RETURN e.id AS id, COALESCE(e.name_credibility, 0) AS cred
            LIMIT 1
            """,
            lei=lei_id, ch=companies_house_id, name=name, name_norm=name_norm,
        ).single()

        if rec:
            entity_id   = rec["id"]
            stored_cred = rec["cred"]
            if credibility_score >= stored_cred:
                session.run(
                    """
                    MATCH (e:Entity {id: $id})
                    SET e.name                = $name,
                        e.name_credibility    = $cred,
                        e.country             = COALESCE($country,  e.country),
                        e.founded             = COALESCE($founded,  e.founded),
                        e.lei_id              = COALESCE($lei,      e.lei_id),
                        e.companies_house_id  = COALESCE($ch,       e.companies_house_id)
                    """,
                    id=entity_id, name=name, cred=credibility_score,
                    country=country, founded=founded, lei=lei_id, ch=companies_house_id,
                )
            else:
                # Lower credibility — stamp identifiers only, don't overwrite name
                session.run(
                    """
                    MATCH (e:Entity {id: $id})
                    SET e.lei_id             = COALESCE($lei, e.lei_id),
                        e.companies_house_id = COALESCE($ch,  e.companies_house_id)
                    """,
                    id=entity_id, lei=lei_id, ch=companies_house_id,
                )
            return entity_id

        entity_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (e:Entity {
                id: $id, name: $name, name_normalized: $name_norm,
                name_credibility: $cred,
                type: $type, country: $country, founded: $founded,
                revenue: null, description: null,
                lei_id: $lei, companies_house_id: $ch,
                wikidata_id: null, verified: false
            })
            """,
            id=entity_id, name=name, name_norm=name_norm, cred=credibility_score,
            type=entity_type, country=country, founded=founded,
            lei=lei_id, ch=companies_house_id,
        )
        return entity_id


def _upsert_person_bods(
    full_name: str,
    first_name: str | None,
    last_name: str | None,
    nationality: str | None,
    birth_date: str | None,
) -> str:
    """Find or create a Person node."""
    first = first_name or ""
    last  = last_name  or ""
    if not first and not last:
        first, last = parse_full_name(full_name)

    with db.get_session() as session:
        rec = session.run(
            "MATCH (p:Person) WHERE p.full_name = $name RETURN p.id AS id LIMIT 1",
            name=full_name,
        ).single()
        if rec:
            return rec["id"]

        person_id = str(uuid.uuid4())
        session.run(
            """
            CREATE (p:Person {
                id: $id, first_name: $first, last_name: $last,
                full_name: $full, nationality: $nat, birth_date: $bdate,
                description: '', wikidata_id: null,
                verified: false, alias: [], nationalities: []
            })
            """,
            id=person_id, first=first, last=last, full=full_name,
            nat=nationality or "", bdate=birth_date or "",
        )
        return person_id


def _upsert_owns_bods(
    owner_id: str,
    owned_id: str,
    stake_percent: float | None,
    ownership_type: str,
    since: str | None,
    until: str | None,
    source_id: str,
    credibility_score: int,
):
    """Create an active OWNS edge if one does not already exist."""
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
            WHERE r.until IS NULL RETURN r LIMIT 1
            """,
            oid=owner_id, nid=owned_id,
        ).single()
        if exists:
            return
        session.run(
            """
            MATCH (a {id: $oid}), (b {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent:    $stake,
                ownership_type:   $otype,
                voting_power_pct: null,
                since:            $since,
                until:            $until,
                source_id:        $sid,
                credibility_score: $score
            }]->(b)
            """,
            oid=owner_id, nid=owned_id,
            stake=stake_percent, otype=ownership_type,
            since=since, until=until,
            sid=source_id, score=credibility_score,
        )


def _upsert_role_bods(
    person_id: str,
    entity_id: str,
    role: str,
    since: str | None,
    until: str | None,
    source_id: str,
    credibility_score: int,
):
    """Create a HAS_ROLE edge if one does not already exist."""
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
            WHERE r.role = $role AND r.until IS NULL
            RETURN r LIMIT 1
            """,
            pid=person_id, eid=entity_id, role=role,
        ).single()
        if exists:
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role,
            since=since, until=until,
            sid=source_id, score=credibility_score,
        )


# ── BODS statement processors ─────────────────────────────────────────────────

def _process_entity_statement(
    stmt: dict,
    bods_to_pamten_id: dict,
    source_id: str,
    credibility_score: int,
    filter_jurisdiction: str | None,
) -> str | None:
    """
    Map a BODS entity statement to a Pamten Entity node.
    Returns the Pamten entity id, or None if the statement was skipped.
    """
    record_id = stmt.get("recordId") or stmt.get("statementId")
    if not record_id:
        return None

    details = stmt.get("recordDetails") or {}
    name = (details.get("name") or "").strip()
    if not name:
        return None

    # Jurisdiction filter
    jurisdiction = details.get("jurisdiction") or {}
    country_code = (jurisdiction.get("code") or "").upper()[:2] or None
    if filter_jurisdiction and country_code != filter_jurisdiction.upper():
        return None

    # Entity type
    raw_type    = (details.get("entityType") or {}).get("type", "registeredEntity")
    entity_type = _ENTITY_TYPE_MAP.get(raw_type, "company")

    # Founding year
    founding_date = details.get("foundingDate") or ""
    founded: int | None = None
    if founding_date and len(founding_date) >= 4:
        try:
            founded = int(founding_date[:4])
        except ValueError:
            pass

    # Identifiers
    lei_id             = None
    companies_house_id = None
    for ident in details.get("identifiers") or []:
        scheme = ident.get("scheme", "")
        value  = (ident.get("id") or ident.get("value") or "").strip()
        if not value:
            continue
        if scheme == "XI-LEI":
            lei_id = value
        elif scheme == "GB-COH":
            companies_house_id = value

    entity_id = _upsert_entity_bods(
        name=name,
        entity_type=entity_type,
        country=country_code,
        founded=founded,
        lei_id=lei_id,
        companies_house_id=companies_house_id,
        source_id=source_id,
        credibility_score=credibility_score,
    )
    bods_to_pamten_id[record_id] = entity_id
    return entity_id


def _process_person_statement(
    stmt: dict,
    bods_to_pamten_id: dict,
    source_id: str,
    credibility_score: int,
) -> str | None:
    """
    Map a BODS person statement to a Pamten Person node.
    Returns the Pamten person id, or None if skipped (e.g. anonymousPerson).
    """
    record_id = stmt.get("recordId") or stmt.get("statementId")
    if not record_id:
        return None

    details = stmt.get("recordDetails") or {}

    # Skip redacted beneficial owners — no useful data
    if details.get("personType") == "anonymousPerson":
        return None

    # Name — prefer "legal" type, fall back to first available
    names   = details.get("names") or []
    name_rec = next((n for n in names if n.get("type") == "legal"), None)
    if not name_rec and names:
        name_rec = names[0]
    if not name_rec:
        return None

    full_name  = (name_rec.get("fullName")   or "").strip()
    first_name = (name_rec.get("givenName")  or "").strip() or None
    last_name  = (name_rec.get("familyName") or "").strip() or None

    if not full_name:
        if first_name and last_name:
            full_name = f"{first_name} {last_name}"
        elif first_name:
            full_name = first_name
        elif last_name:
            full_name = last_name
    if not full_name:
        return None

    # Nationality
    nationalities = details.get("nationalities") or []
    nationality   = (nationalities[0].get("code") or "") if nationalities else ""

    # Birth date — may be partial ("1978-07"); store as-is, don't parse
    birth_date = details.get("birthDate") or None

    person_id = _upsert_person_bods(
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        nationality=nationality or None,
        birth_date=birth_date,
    )
    bods_to_pamten_id[record_id] = person_id
    return person_id


def _process_relationship_statement(
    stmt: dict,
    bods_to_pamten_id: dict,
    source_id: str,
    credibility_score: int,
) -> int:
    """
    Map a BODS relationship statement to OWNS or HAS_ROLE edges.
    Returns the number of edges written.
    """
    details       = stmt.get("recordDetails") or {}
    record_status = stmt.get("recordStatus", "new")

    subject_ref  = details.get("subject")         or details.get("subjectId")
    party_ref    = details.get("interestedParty")  or details.get("interestedPartyId")

    if not subject_ref or not party_ref:
        return 0

    # Resolve BODS record ids to Pamten node ids.
    # If either side is unknown, create a minimal placeholder so the edge is preserved.
    owned_id = bods_to_pamten_id.get(subject_ref)
    if not owned_id:
        owned_id = _upsert_entity_bods(
            name=subject_ref[:200], entity_type="company",
            country=None, founded=None, lei_id=None, companies_house_id=None,
            source_id=source_id, credibility_score=0,
        )
        bods_to_pamten_id[subject_ref] = owned_id

    owner_id = bods_to_pamten_id.get(party_ref)
    if not owner_id:
        owner_id = _upsert_entity_bods(
            name=party_ref[:200], entity_type="company",
            country=None, founded=None, lei_id=None, companies_house_id=None,
            source_id=source_id, credibility_score=0,
        )
        bods_to_pamten_id[party_ref] = owner_id

    interests = details.get("interests") or []
    if not interests:
        return 0

    # "closed" record → ownership ended; use statementDate as until date
    closed     = record_status == "closed"
    close_date = stmt.get("statementDate") if closed else None

    edges = 0
    for interest in interests:
        interest_type = interest.get("type", "shareholding")
        start_date    = interest.get("startDate") or None
        end_date      = interest.get("endDate") or (close_date if closed else None)

        mapped = _INTEREST_OWNERSHIP_TYPE.get(interest_type)

        if mapped == "role":
            # seniorManagingOfficial → HAS_ROLE (owner should be a Person)
            _upsert_role_bods(
                person_id=owner_id, entity_id=owned_id,
                role="Senior Managing Official",
                since=start_date, until=end_date,
                source_id=source_id, credibility_score=credibility_score,
            )
            edges += 1
            continue

        # All other types → OWNS edge
        share: dict = interest.get("share") or {}
        stake: float | None = None
        if share.get("exact") is not None:
            try:
                stake = float(share["exact"])
            except (TypeError, ValueError):
                pass
        elif share.get("minimum") is not None:
            # Approximate — use minimum as a floor
            try:
                stake = float(share["minimum"])
            except (TypeError, ValueError):
                pass

        if mapped is None:
            # "shareholding" or unmapped type — derive from stake %
            ownership_type = derive_ownership_type(stake)
        else:
            ownership_type = mapped

        _upsert_owns_bods(
            owner_id=owner_id, owned_id=owned_id,
            stake_percent=stake, ownership_type=ownership_type,
            since=start_date, until=end_date,
            source_id=source_id, credibility_score=credibility_score,
        )
        edges += 1

    return edges


# ── Streaming helpers ─────────────────────────────────────────────────────────

def _iter_statements(stream: IO[bytes]) -> Iterator[dict]:
    """Stream BODS statements from a binary file object using ijson."""
    yield from ijson.items(stream, "item")


def _open_zip_stream(zip_path: str) -> IO[bytes]:
    """Open the first .json file inside a ZIP archive for streaming."""
    zf = zipfile.ZipFile(zip_path)
    json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
    if not json_names:
        raise ValueError(f"No .json file found inside ZIP: {zip_path}")
    log.info("BODS: reading %s from zip", json_names[0])
    return zf.open(json_names[0])


def stream_bods_json(url: str) -> Iterator[dict]:
    """
    Download a BODS ZIP from a URL and stream statements one at a time.

    Downloads to a temp file first (needed for two-pass processing).
    Cleans up the temp file after the iterator is exhausted.
    """
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        log.info("BODS: downloading %s", url)
        with open(tmp_path, "wb") as out:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
                resp.raise_for_status()
                total      = int(resp.headers.get("content-length", 0))
                downloaded = 0
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total and downloaded % (100 * CHUNK_SIZE) == 0:
                        log.info("BODS: %.0f%% downloaded", 100 * downloaded / total)
        log.info("BODS: download complete (%d bytes)", downloaded)

        stream = _open_zip_stream(tmp_path)
        yield from _iter_statements(stream)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Core import engine ────────────────────────────────────────────────────────

def _run_import(
    statements: Iterator[dict],
    source_id: str,
    credibility_score: int,
    limit: int | None,
    filter_jurisdiction: str | None,
) -> dict:
    """
    Single-pass import with relationship buffering.

    Pass 1 (streaming): write entity and person nodes immediately;
                        buffer relationship statements.
    Pass 2 (in-memory): write relationship edges using the completed
                        bods_to_pamten_id lookup.
    """
    bods_to_pamten_id: dict[str, str] = {}
    buffered_rels:     list[dict]     = []
    jur = filter_jurisdiction.upper() if filter_jurisdiction else None

    counts = dict(entities=0, persons=0, relationships=0, skipped=0, errors=0)
    processed = 0

    log.info("BODS: pass 1 — streaming entities and persons%s",
             f" (limit={limit})" if limit else "")

    for stmt in statements:
        if limit and processed >= limit:
            break

        record_type = stmt.get("recordType")

        if record_type == "entity":
            try:
                result = _process_entity_statement(
                    stmt, bods_to_pamten_id, source_id, credibility_score, jur,
                )
                if result:
                    counts["entities"] += 1
                    processed += 1
                else:
                    counts["skipped"] += 1
            except Exception as exc:
                log.warning("BODS entity error: %s", exc)
                counts["errors"] += 1

        elif record_type == "person":
            try:
                result = _process_person_statement(
                    stmt, bods_to_pamten_id, source_id, credibility_score,
                )
                if result:
                    counts["persons"] += 1
                    processed += 1
                else:
                    counts["skipped"] += 1
            except Exception as exc:
                log.warning("BODS person error: %s", exc)
                counts["errors"] += 1

        elif record_type == "relationship":
            buffered_rels.append(stmt)

    log.info(
        "BODS: pass 1 done — %d entities, %d persons, %d relationships buffered",
        counts["entities"], counts["persons"], len(buffered_rels),
    )

    # Pass 2: write edges now that all nodes are known
    log.info("BODS: pass 2 — writing %d relationship edges", len(buffered_rels))
    for stmt in buffered_rels:
        try:
            edges = _process_relationship_statement(
                stmt, bods_to_pamten_id, source_id, credibility_score,
            )
            counts["relationships"] += edges
        except Exception as exc:
            log.warning("BODS relationship error: %s", exc)
            counts["errors"] += 1

    log.info("BODS: import complete — %s", counts)
    return counts


# ── Public entry points ───────────────────────────────────────────────────────

def import_bods_source(
    source_name: str,
    url: str,
    source_id: str,
    credibility_score: int,
    limit: int | None = None,
    filter_jurisdiction: str | None = None,
) -> dict:
    """
    Import a full BODS dataset from a remote ZIP URL into ArcadeDB.

    Args:
        source_name:         Human-readable name, e.g. "GLEIF" or "UK PSC"
        url:                 ZIP download URL
        source_id:           Pamten Source node id (from _ensure_bods_source)
        credibility_score:   92 for GLEIF, 97 for UK PSC
        limit:               Max entity/person statements to process (None = no limit).
                             Relationship statements are always processed for resolved nodes.
        filter_jurisdiction: ISO alpha-2 country code to restrict entity imports,
                             e.g. "GB" to import only UK-registered entities.
                             Persons and relationships are always included.

    Returns:
        dict with keys: entities, persons, relationships, skipped, errors
    """
    log.info("BODS: starting import of %s", source_name)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        log.info("BODS: downloading %s…", url)
        downloaded = 0
        with open(tmp_path, "wb") as out:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total and downloaded % (100 * CHUNK_SIZE) == 0:
                        log.info("BODS: %.0f%% downloaded", 100 * downloaded / total)
        log.info("BODS: download complete — %d bytes", downloaded)

        stream = _open_zip_stream(tmp_path)
        return _run_import(
            _iter_statements(stream),
            source_id=source_id,
            credibility_score=credibility_score,
            limit=limit,
            filter_jurisdiction=filter_jurisdiction,
        )

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def import_bods_file(
    filepath: str,
    source_id: str,
    credibility_score: int,
    limit: int | None = None,
    filter_jurisdiction: str | None = None,
) -> dict:
    """
    Import BODS statements from a local .json or .zip file into ArcadeDB.

    Args:
        filepath:            Absolute path to a .json or .zip file
        source_id:           Pamten Source node id
        credibility_score:   Source credibility (0–100)
        limit:               Max entity/person statements to process (None = no limit)
        filter_jurisdiction: ISO alpha-2 country code filter for entities

    Returns:
        dict with keys: entities, persons, relationships, skipped, errors
    """
    log.info("BODS: importing from local file %s", filepath)

    if filepath.lower().endswith(".zip"):
        stream = _open_zip_stream(filepath)
    else:
        stream = open(filepath, "rb")  # noqa: WPS515

    try:
        return _run_import(
            _iter_statements(stream),
            source_id=source_id,
            credibility_score=credibility_score,
            limit=limit,
            filter_jurisdiction=filter_jurisdiction,
        )
    finally:
        stream.close()
