"""
BODS (Beneficial Ownership Data Standard) v0.4 importer for Pamten.

Imports GLEIF and UK PSC datasets published by Open Ownership.
Both datasets are CC0 licensed — fully open, no restrictions.

Datasets:
  GLEIF:   https://oo-bodsdata.s3.amazonaws.com/data/gleif_version_0_4/json.zip   (~1.1 GB)
  UK PSC:  https://oo-bodsdata.s3.amazonaws.com/data/uk_version_0_4/json.zip      (~3.3 GB)

Data licence: CC0 1.0 (compatible with ODbL — see DATA_LICENSE.md)

Provenance (per OWNS/HAS_ROLE edge, for later verification):
  source_url   → GLEIF record (search.gleif.org/#/record/<LEI>) for XI-LEI refs,
                 else the statement's own source.url (UK PSC → Companies House)
  source_date  → BODS statementDate
  last_scraped_at → import time (refreshed when an existing edge is re-imported)

Processing strategy:
  Single streaming pass through the file.
  Entity and person statements are written to the DB immediately.
  Relationship statements are buffered in memory (they are smaller than
  entity/person records) and processed in a second pass once all nodes
  are known, so forward-references resolve correctly.

  For very large datasets use filter_jurisdiction (e.g. "GB") or limit
  to constrain memory and runtime.
"""

import json
import logging
import os
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Iterator
from typing import IO

import httpx
import ijson

from datetime import datetime, timezone

from app.database import db
from app.scraper.mapper import derive_ownership_type, normalize_entity_name, parse_full_name

log = logging.getLogger(__name__)


def _now_iso() -> str:
    """UTC timestamp for last_scraped_at provenance."""
    return datetime.now(timezone.utc).isoformat()


def _bods_record_url(subject_ref: str | None, stmt: dict) -> str | None:
    """
    Verifiable per-record URL for a BODS statement.

    GLEIF record ids are "XI-LEI-{LEI}" → link to the public GLEIF record.
    Otherwise fall back to the statement's own declared source URL (UK PSC
    statements carry a Companies House link), or None if neither is available.
    """
    if subject_ref and subject_ref.startswith("XI-LEI-"):
        return f"https://search.gleif.org/#/record/{subject_ref[7:]}"
    src = stmt.get("source") or {}
    url = src.get("url")
    return url or None

CHUNK_SIZE = 1024 * 1024  # 1 MB per download chunk

# ISO 3166-1 alpha-2 → full English country name
_ISO2_COUNTRY: dict[str, str] = {
    "AF": "Afghanistan", "AX": "Åland Islands", "AL": "Albania", "DZ": "Algeria",
    "AS": "American Samoa", "AD": "Andorra", "AO": "Angola", "AI": "Anguilla",
    "AQ": "Antarctica", "AG": "Antigua and Barbuda", "AR": "Argentina",
    "AM": "Armenia", "AW": "Aruba", "AU": "Australia", "AT": "Austria",
    "AZ": "Azerbaijan", "BS": "Bahamas", "BH": "Bahrain", "BD": "Bangladesh",
    "BB": "Barbados", "BY": "Belarus", "BE": "Belgium", "BZ": "Belize",
    "BJ": "Benin", "BM": "Bermuda", "BT": "Bhutan", "BO": "Bolivia",
    "BQ": "Bonaire, Sint Eustatius and Saba", "BA": "Bosnia and Herzegovina",
    "BW": "Botswana", "BV": "Bouvet Island", "BR": "Brazil",
    "IO": "British Indian Ocean Territory", "BN": "Brunei", "BG": "Bulgaria",
    "BF": "Burkina Faso", "BI": "Burundi", "CV": "Cabo Verde", "KH": "Cambodia",
    "CM": "Cameroon", "CA": "Canada", "KY": "Cayman Islands",
    "CF": "Central African Republic", "TD": "Chad", "CL": "Chile", "CN": "China",
    "CX": "Christmas Island", "CC": "Cocos (Keeling) Islands", "CO": "Colombia",
    "KM": "Comoros", "CG": "Congo", "CD": "Congo, Democratic Republic",
    "CK": "Cook Islands", "CR": "Costa Rica", "CI": "Côte d'Ivoire",
    "HR": "Croatia", "CU": "Cuba", "CW": "Curaçao", "CY": "Cyprus",
    "CZ": "Czech Republic", "DK": "Denmark", "DJ": "Djibouti", "DM": "Dominica",
    "DO": "Dominican Republic", "EC": "Ecuador", "EG": "Egypt",
    "SV": "El Salvador", "GQ": "Equatorial Guinea", "ER": "Eritrea",
    "EE": "Estonia", "SZ": "Eswatini", "ET": "Ethiopia",
    "FK": "Falkland Islands", "FO": "Faroe Islands", "FJ": "Fiji",
    "FI": "Finland", "FR": "France", "GF": "French Guiana",
    "PF": "French Polynesia", "TF": "French Southern Territories", "GA": "Gabon",
    "GM": "Gambia", "GE": "Georgia", "DE": "Germany", "GH": "Ghana",
    "GI": "Gibraltar", "GR": "Greece", "GL": "Greenland", "GD": "Grenada",
    "GP": "Guadeloupe", "GU": "Guam", "GT": "Guatemala", "GG": "Guernsey",
    "GN": "Guinea", "GW": "Guinea-Bissau", "GY": "Guyana", "HT": "Haiti",
    "HM": "Heard Island and McDonald Islands", "VA": "Holy See", "HN": "Honduras",
    "HK": "Hong Kong", "HU": "Hungary", "IS": "Iceland", "IN": "India",
    "ID": "Indonesia", "IR": "Iran", "IQ": "Iraq", "IE": "Ireland",
    "IM": "Isle of Man", "IL": "Israel", "IT": "Italy", "JM": "Jamaica",
    "JP": "Japan", "JE": "Jersey", "JO": "Jordan", "KZ": "Kazakhstan",
    "KE": "Kenya", "KI": "Kiribati", "KP": "Korea, North", "KR": "Korea, South",
    "KW": "Kuwait", "KG": "Kyrgyzstan", "LA": "Laos", "LV": "Latvia",
    "LB": "Lebanon", "LS": "Lesotho", "LR": "Liberia", "LY": "Libya",
    "LI": "Liechtenstein", "LT": "Lithuania", "LU": "Luxembourg",
    "MO": "Macao", "MG": "Madagascar", "MW": "Malawi", "MY": "Malaysia",
    "MV": "Maldives", "ML": "Mali", "MT": "Malta", "MH": "Marshall Islands",
    "MQ": "Martinique", "MR": "Mauritania", "MU": "Mauritius", "YT": "Mayotte",
    "MX": "Mexico", "FM": "Micronesia", "MD": "Moldova", "MC": "Monaco",
    "MN": "Mongolia", "ME": "Montenegro", "MS": "Montserrat", "MA": "Morocco",
    "MZ": "Mozambique", "MM": "Myanmar", "NA": "Namibia", "NR": "Nauru",
    "NP": "Nepal", "NL": "Netherlands", "NC": "New Caledonia", "NZ": "New Zealand",
    "NI": "Nicaragua", "NE": "Niger", "NG": "Nigeria", "NU": "Niue",
    "NF": "Norfolk Island", "MK": "North Macedonia",
    "MP": "Northern Mariana Islands", "NO": "Norway", "OM": "Oman",
    "PK": "Pakistan", "PW": "Palau", "PS": "Palestine", "PA": "Panama",
    "PG": "Papua New Guinea", "PY": "Paraguay", "PE": "Peru",
    "PH": "Philippines", "PN": "Pitcairn", "PL": "Poland", "PT": "Portugal",
    "PR": "Puerto Rico", "QA": "Qatar", "RE": "Réunion", "RO": "Romania",
    "RU": "Russia", "RW": "Rwanda", "BL": "Saint Barthélemy",
    "SH": "Saint Helena", "KN": "Saint Kitts and Nevis", "LC": "Saint Lucia",
    "MF": "Saint Martin", "PM": "Saint Pierre and Miquelon",
    "VC": "Saint Vincent and the Grenadines", "WS": "Samoa", "SM": "San Marino",
    "ST": "Sao Tome and Principe", "SA": "Saudi Arabia", "SN": "Senegal",
    "RS": "Serbia", "SC": "Seychelles", "SL": "Sierra Leone", "SG": "Singapore",
    "SX": "Sint Maarten", "SK": "Slovakia", "SI": "Slovenia",
    "SB": "Solomon Islands", "SO": "Somalia", "ZA": "South Africa",
    "GS": "South Georgia and the South Sandwich Islands", "SS": "South Sudan",
    "ES": "Spain", "LK": "Sri Lanka", "SD": "Sudan", "SR": "Suriname",
    "SJ": "Svalbard and Jan Mayen", "SE": "Sweden", "CH": "Switzerland",
    "SY": "Syria", "TW": "Taiwan", "TJ": "Tajikistan", "TZ": "Tanzania",
    "TH": "Thailand", "TL": "Timor-Leste", "TG": "Togo", "TK": "Tokelau",
    "TO": "Tonga", "TT": "Trinidad and Tobago", "TN": "Tunisia", "TR": "Turkey",
    "TM": "Turkmenistan", "TC": "Turks and Caicos Islands", "TV": "Tuvalu",
    "UG": "Uganda", "UA": "Ukraine", "AE": "United Arab Emirates",
    "GB": "United Kingdom", "US": "United States", "UM": "United States Minor Outlying Islands",
    "UY": "Uruguay", "UZ": "Uzbekistan", "VU": "Vanuatu", "VE": "Venezuela",
    "VN": "Vietnam", "VG": "Virgin Islands, British", "VI": "Virgin Islands, U.S.",
    "WF": "Wallis and Futuna", "EH": "Western Sahara", "YE": "Yemen",
    "ZM": "Zambia", "ZW": "Zimbabwe",
    # GLEIF special codes
    "XI": "International",
    "XK": "Kosovo",
}


def _country_name(code: str | None) -> str | None:
    """Convert an ISO alpha-2 code to a full English country name. Returns the
    code unchanged if no mapping is found, and None for empty/None input."""
    if not code:
        return None
    return _ISO2_COUNTRY.get(code.upper(), code)

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


def _ref_id(ref: object) -> str | None:
    """Extract a BODS record-ID from either a bare string or a BODS v0.3 dict ref.

    BODS v0.2 used plain string IDs; v0.3+ wraps them in objects:
      {"describedByEntityStatement": "id"} or {"describedByPersonStatement": "id"}
    """
    if isinstance(ref, dict):
        return ref.get("describedByEntityStatement") or ref.get("describedByPersonStatement") or None
    return ref or None


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
        # Query each indexed property separately so ArcadeDB can use its indexes.
        # A single OR across multiple properties forces a full scan even when indexes exist.
        # Wrap each lookup in a try/except: DELETE FROM Entity removes records but can
        # leave stale index entries pointing to deleted RIDs, causing RecordNotFoundException.
        # Treating that as "not found" is correct — the record is gone.
        rec = None
        if lei_id:
            try:
                rec = session.run(
                    "MATCH (e:Entity {lei_id: $lei}) RETURN e.id AS id, COALESCE(e.name_credibility, 0) AS cred LIMIT 1",
                    lei=lei_id,
                ).single()
            except RuntimeError as exc:
                if "RecordNotFoundException" not in str(exc):
                    raise
        if not rec and companies_house_id:
            try:
                rec = session.run(
                    "MATCH (e:Entity {companies_house_id: $ch}) RETURN e.id AS id, COALESCE(e.name_credibility, 0) AS cred LIMIT 1",
                    ch=companies_house_id,
                ).single()
            except RuntimeError as exc:
                if "RecordNotFoundException" not in str(exc):
                    raise
        if not rec:
            try:
                rec = session.run(
                    "MATCH (e:Entity) WHERE e.name_normalized = $name_norm OR e.name = $name RETURN e.id AS id, COALESCE(e.name_credibility, 0) AS cred LIMIT 1",
                    name_norm=name_norm, name=name,
                ).single()
            except RuntimeError as exc:
                if "RecordNotFoundException" not in str(exc):
                    raise

        if rec:
            entity_id   = rec["id"]
            stored_cred = rec["cred"]
            if credibility_score >= stored_cred:
                session.run(
                    """
                    MATCH (e:Entity {id: $id})
                    SET e.name                = $name,
                        e.name_credibility    = $cred,
                        e.source_id           = $sid,
                        e.country             = COALESCE($country,  e.country),
                        e.founded             = COALESCE($founded,  e.founded),
                        e.lei_id              = COALESCE($lei,      e.lei_id),
                        e.companies_house_id  = COALESCE($ch,       e.companies_house_id)
                    """,
                    id=entity_id, name=name, cred=credibility_score, sid=source_id,
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
                name_credibility: $cred, source_id: $sid,
                type: $type, country: $country, founded: $founded,
                revenue: null, description: null,
                lei_id: $lei, companies_house_id: $ch,
                wikidata_id: null, verified: false
            })
            """,
            id=entity_id, name=name, name_norm=name_norm, cred=credibility_score,
            sid=source_id, type=entity_type, country=country, founded=founded,
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
    source_url: str | None = None,
    source_date: str | None = None,
):
    """Create an active OWNS edge if one does not already exist.

    Stamps per-entry provenance: source_url (GLEIF/Companies House record),
    source_date (BODS statementDate), last_scraped_at (refreshed on re-import).
    """
    now = _now_iso()
    with db.get_session() as session:
        exists = session.run(
            """
            MATCH (a:Entity {id: $oid})-[r:OWNS]->(b:Entity {id: $nid})
            WHERE r.until IS NULL RETURN r LIMIT 1
            """,
            oid=owner_id, nid=owned_id,
        ).single()
        if exists:
            session.run(
                """
                MATCH (a:Entity {id: $oid})-[r:OWNS]->(b:Entity {id: $nid})
                WHERE r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                oid=owner_id, nid=owned_id, now=now,
                surl=source_url, sdate=source_date,
            )
            return
        session.run(
            """
            MATCH (a:Entity {id: $oid}), (b:Entity {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent:    $stake,
                ownership_type:   $otype,
                voting_power_pct: null,
                since:            $since,
                until:            $until,
                source_id:        $sid,
                credibility_score: $score,
                source_url:       $surl,
                source_date:      $sdate,
                last_scraped_at:  $now
            }]->(b)
            """,
            oid=owner_id, nid=owned_id,
            stake=stake_percent, otype=ownership_type,
            since=since, until=until,
            sid=source_id, score=credibility_score,
            surl=source_url, sdate=source_date, now=now,
        )


def _upsert_role_bods(
    person_id: str,
    entity_id: str,
    role: str,
    since: str | None,
    until: str | None,
    source_id: str,
    credibility_score: int,
    source_url: str | None = None,
    source_date: str | None = None,
):
    """Create a HAS_ROLE edge if one does not already exist.

    Stamps per-entry provenance (source_url/source_date/last_scraped_at),
    refreshing last_scraped_at on re-import of an existing edge.
    """
    now = _now_iso()
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
            session.run(
                """
                MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(e:Entity {id: $eid})
                WHERE r.role = $role AND r.until IS NULL
                SET r.last_scraped_at = $now,
                    r.source_url  = COALESCE($surl,  r.source_url),
                    r.source_date = COALESCE($sdate, r.source_date)
                """,
                pid=person_id, eid=entity_id, role=role, now=now,
                surl=source_url, sdate=source_date,
            )
            return
        session.run(
            """
            MATCH (p:Person {id: $pid}), (e:Entity {id: $eid})
            CREATE (p)-[:HAS_ROLE {
                role: $role, since: $since, until: $until,
                source_id: $sid, credibility_score: $score,
                source_url: $surl, source_date: $sdate, last_scraped_at: $now
            }]->(e)
            """,
            pid=person_id, eid=entity_id, role=role,
            since=since, until=until,
            sid=source_id, score=credibility_score,
            surl=source_url, sdate=source_date, now=now,
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
    # Store the ISO-2 code — the canonical Entity.country form shared with the
    # Wikidata scraper, so by-country grouping doesn't split (frontend localizes).
    country = country_code

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
        country=country,
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
    bods_id_to_name: dict,
    source_id: str,
    credibility_score: int,
) -> int:
    """
    Map a BODS relationship statement to OWNS or HAS_ROLE edges.
    Returns the number of edges written.
    """
    details       = stmt.get("recordDetails") or {}
    record_status = stmt.get("recordStatus", "new")

    subject_ref  = _ref_id(details.get("subject")        or details.get("subjectId"))
    party_ref    = _ref_id(details.get("interestedParty") or details.get("interestedPartyId"))

    if not subject_ref or not party_ref:
        return 0

    def _placeholder_name(ref: str) -> str:
        """Return the real entity name when available, else a cleaned-up fallback."""
        if ref in bods_id_to_name:
            return bods_id_to_name[ref]
        # GLEIF BODS record IDs are "XI-LEI-{LEI}" — strip the prefix
        if ref.startswith("XI-LEI-"):
            return f"Unknown [{ref[7:]}]"
        return ref[:200]

    def _placeholder_lei(ref: str) -> str | None:
        if ref.startswith("XI-LEI-"):
            return ref[7:]
        return None

    # Resolve BODS record ids to Pamten node ids.
    # If either side is unknown, create a named placeholder so the edge is preserved.
    owned_id = bods_to_pamten_id.get(subject_ref)
    if not owned_id:
        owned_id = _upsert_entity_bods(
            name=_placeholder_name(subject_ref), entity_type="company",
            country=None, founded=None,
            lei_id=_placeholder_lei(subject_ref), companies_house_id=None,
            source_id=source_id, credibility_score=0,
        )
        bods_to_pamten_id[subject_ref] = owned_id

    owner_id = bods_to_pamten_id.get(party_ref)
    if not owner_id:
        owner_id = _upsert_entity_bods(
            name=_placeholder_name(party_ref), entity_type="company",
            country=None, founded=None,
            lei_id=_placeholder_lei(party_ref), companies_house_id=None,
            source_id=source_id, credibility_score=0,
        )
        bods_to_pamten_id[party_ref] = owner_id

    interests = details.get("interests") or []
    if not interests:
        return 0

    # "closed" record → ownership ended; use statementDate as until date
    closed     = record_status == "closed"
    close_date = stmt.get("statementDate") if closed else None

    # Provenance: verifiable record URL + the statement's own date
    record_url     = _bods_record_url(subject_ref, stmt)
    statement_date = stmt.get("statementDate")

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
                source_url=record_url, source_date=statement_date,
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

        if interest_type not in _INTEREST_OWNERSHIP_TYPE:
            # Unknown / future interest type — fall back to minority
            ownership_type = derive_ownership_type(stake) if stake is not None else "minority"
        elif mapped is None:
            # "shareholding" — derive from stake %
            ownership_type = derive_ownership_type(stake)
        else:
            ownership_type = mapped

        _upsert_owns_bods(
            owner_id=owner_id, owned_id=owned_id,
            stake_percent=stake, ownership_type=ownership_type,
            since=start_date, until=end_date,
            source_id=source_id, credibility_score=credibility_score,
            source_url=record_url, source_date=statement_date,
        )
        edges += 1

    return edges


# ── Streaming helpers ─────────────────────────────────────────────────────────

class _CombinedStream:
    """Prepend a byte-buffer to a binary stream (needed after format-detection peek)."""
    def __init__(self, prefix: bytes, rest: IO[bytes]):
        self._prefix = prefix
        self._offset = 0
        self._rest = rest

    def read(self, n: int = -1) -> bytes:
        prefix_remaining = len(self._prefix) - self._offset
        if prefix_remaining <= 0:
            return self._rest.read(n)
        if n == -1:
            chunk = self._prefix[self._offset:]
            self._offset = len(self._prefix)
            return chunk + self._rest.read()
        if n <= prefix_remaining:
            chunk = self._prefix[self._offset:self._offset + n]
            self._offset += n
            return chunk
        chunk = self._prefix[self._offset:]
        self._offset = len(self._prefix)
        return chunk + self._rest.read(n - len(chunk))

    def readable(self) -> bool:
        return True


class _ProgressBar:
    """Terminal progress bar that writes to stderr via carriage return."""

    _WIDTH = 30

    def __init__(self, label: str) -> None:
        self._label = label
        self._start = time.monotonic()
        self._last  = 0.0
        self._tty   = sys.stderr.isatty()

    def _ftime(self, secs: float) -> str:
        m, s = divmod(int(secs), 60)
        return f"{m:02d}:{s:02d}"

    def render(self, done: int, total: int | None, extra: str = "") -> None:
        now = time.monotonic()
        if now - self._last < 0.25:   # cap at 4 redraws/sec
            return
        self._last = now
        elapsed = now - self._start

        if total:
            pct    = min(100.0, done * 100.0 / total)
            filled = int(self._WIDTH * pct / 100)
            bar    = "█" * filled + "░" * (self._WIDTH - filled)
            line   = f"{self._label}  [{bar}] {pct:5.1f}%"
        else:
            line = f"{self._label}  {done:,} done"

        line += f"  {self._ftime(elapsed)}"
        if extra:
            line += f"  {extra}"

        if self._tty:
            sys.stderr.write(f"\r{line:<79}")
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def finish(self, summary: str = "") -> None:
        elapsed = time.monotonic() - self._start
        line = f"{self._label}  done  {self._ftime(elapsed)}"
        if summary:
            line += f"  {summary}"
        if self._tty:
            sys.stderr.write(f"\r{line:<79}\n")
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()


class _ProgressStream:
    """Byte-counting wrapper that feeds a _ProgressBar as data is read."""

    def __init__(self, stream: IO[bytes], total_bytes: int, bar: _ProgressBar) -> None:
        self._stream = stream
        self._total  = total_bytes
        self._read   = 0
        self._bar    = bar

    def read(self, n: int = -1) -> bytes:
        data = self._stream.read(n)
        if data:
            self._read += len(data)
            self._bar.render(self._read, self._total)
        return data

    def readable(self) -> bool:
        return True


def _iter_ndjson(stream: IO[bytes]) -> Iterator[dict]:
    """Parse a NDJSON (one JSON object per line) binary stream."""
    buf = b""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            line = buf.strip()
            if line:
                yield json.loads(line)
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line:
                yield json.loads(line)


def _iter_statements(stream: IO[bytes]) -> Iterator[dict]:
    """Stream BODS statements. Handles both JSON array ([…]) and NDJSON formats."""
    prefix = b""
    while len(prefix) < 512:
        chunk = stream.read(512)
        if not chunk:
            return
        prefix += chunk

    combined = _CombinedStream(prefix, stream)
    if prefix.lstrip().startswith(b"["):
        yield from ijson.items(combined, "item")
    else:
        yield from _iter_ndjson(combined)


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
    pass1_bar: _ProgressBar | None = None,
) -> dict:
    """
    Single-pass import with relationship buffering.

    Pass 1 (streaming): write entity and person nodes immediately;
                        buffer relationship statements.
    Pass 2 (in-memory): write relationship edges using the completed
                        bods_to_pamten_id lookup.
    """
    bods_to_pamten_id: dict[str, str] = {}
    bods_id_to_name:   dict[str, str] = {}   # all entity names, for placeholder creation
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
            # Cache the name for every entity so foreign-company placeholders
            # get their real name instead of the raw BODS record ID.
            _rid = stmt.get("recordId") or stmt.get("statementId")
            if _rid:
                _det = stmt.get("recordDetails") or {}
                _nm  = (_det.get("name") or "").strip()
                if _nm:
                    bods_id_to_name[_rid] = _nm
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
            # Only buffer if at least one endpoint was imported. This avoids
            # accumulating millions of foreign-to-foreign relationships in RAM
            # when running with a jurisdiction filter against a global file.
            _details = stmt.get("recordDetails") or {}
            _subj  = _ref_id(_details.get("subject")        or _details.get("subjectId"))
            _party = _ref_id(_details.get("interestedParty") or _details.get("interestedPartyId"))
            if _subj in bods_to_pamten_id or _party in bods_to_pamten_id:
                buffered_rels.append(stmt)

    log.info(
        "BODS: pass 1 done — %d entities, %d persons, %d relationships buffered",
        counts["entities"], counts["persons"], len(buffered_rels),
    )
    if pass1_bar:
        pass1_bar.finish(
            f"entities={counts['entities']:,}  persons={counts['persons']:,}"
            f"  {len(buffered_rels):,} rels buffered"
        )

    # Pass 2: write edges now that all nodes are known
    log.info("BODS: pass 2 — writing %d relationship edges", len(buffered_rels))
    bar2 = _ProgressBar("Pass 2")
    total_rels = len(buffered_rels)
    for i, stmt in enumerate(buffered_rels, 1):
        bar2.render(i, total_rels)
        try:
            edges = _process_relationship_statement(
                stmt, bods_to_pamten_id, bods_id_to_name, source_id, credibility_score,
            )
            counts["relationships"] += edges
        except Exception as exc:
            log.warning("BODS relationship error: %s", exc)
            counts["errors"] += 1
    bar2.finish(f"relationships={counts['relationships']:,}  errors={counts['errors']:,}")

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
        dl_bar     = _ProgressBar("Download")
        downloaded = 0
        with open(tmp_path, "wb") as out:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    out.write(chunk)
                    downloaded += len(chunk)
                    dl_bar.render(downloaded, total or None)
        dl_bar.finish(f"{downloaded / 1e6:.0f} MB")
        log.info("BODS: download complete — %d bytes", downloaded)

        zf         = zipfile.ZipFile(tmp_path)
        json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not json_names:
            raise ValueError(f"No .json file found inside ZIP: {tmp_path}")
        entry       = json_names[0]
        total_bytes = zf.getinfo(entry).file_size
        raw_stream  = zf.open(entry)

        bar    = _ProgressBar("Pass 1")
        stream = _ProgressStream(raw_stream, total_bytes, bar)
        return _run_import(
            _iter_statements(stream),
            source_id=source_id,
            credibility_score=credibility_score,
            limit=limit,
            filter_jurisdiction=filter_jurisdiction,
            pass1_bar=bar,
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
        zf         = zipfile.ZipFile(filepath)
        json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not json_names:
            raise ValueError(f"No .json file found inside ZIP: {filepath}")
        entry        = json_names[0]
        total_bytes  = zf.getinfo(entry).file_size
        raw_stream   = zf.open(entry)
        log.info("BODS: reading %s  (%s bytes uncompressed)", entry, f"{total_bytes:,}")
    else:
        total_bytes = os.path.getsize(filepath)
        raw_stream  = open(filepath, "rb")  # noqa: WPS515

    try:
        bar    = _ProgressBar("Pass 1")
        stream = _ProgressStream(raw_stream, total_bytes, bar)
        return _run_import(
            _iter_statements(stream),
            source_id=source_id,
            credibility_score=credibility_score,
            limit=limit,
            filter_jurisdiction=filter_jurisdiction,
            pass1_bar=bar,
        )
    finally:
        raw_stream.close()
