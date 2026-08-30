"""
Shared bulk-import helpers for Owlgraph's dataset scrapers.

Historically this module was the OpenOwnership **BODS** (Beneficial Ownership
Data Standard) importer. Both BODS exports (GLEIF and UK PSC) were frozen at
2025-03, so the ingest was migrated to current sources — the GLEIF golden copy
(``gleif_lei_cdf`` / ``gleif_rr`` / ``gleif_succession``) and the Companies House
snapshots (``companies_house_psc`` / ``basic_company_data``). The BODS
statement-processing engine has been removed; what remains here is the reusable
plumbing those newer importers all build on:

  * ``_BatchWriter`` — buffers node upserts / edge creates and flushes them to
    ArcadeDB in batched ``sqlscript`` requests (the dominant cost of a bulk load).
  * ``_DiskMap`` — SQLite-backed id/name map so a multi-GB import doesn't have to
    fit in RAM.
  * ``_entity`` / ``_owns`` — Entity-upsert and OWNS-edge writers.
  * ``_ProgressBar`` / ``_ProgressStream`` — terminal progress + byte-counting.
  * ``_drop_secondary_indexes`` / ``_rebuild_indexes`` — bulk-load index toggling.
  * ``_ISO2_COUNTRY`` / ``_legal_form_type`` / ``_max_pct`` / ``_now_iso`` — mapping utils.
"""

import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import re
from typing import IO


from datetime import datetime, timezone

from app.claims import claim_props, KIND_OWNS, KIND_ROLE, KIND_SUCCESSION

from app.config import settings
from app.db.arcadedb import run_sqlscript
from app.scraper.mapper import (
    normalize_entity_name, is_nominee_name,
)

log = logging.getLogger(__name__)


def _tmp_dir() -> str | None:
    """Directory for the importer's on-disk id maps and downloads. Uses
    settings.SCRAPER_TMP_DIR when set (creating it) so a large UK PSC import
    doesn't fill a small tmpfs /tmp; otherwise the system default."""
    d = settings.SCRAPER_TMP_DIR
    if d:
        os.makedirs(d, exist_ok=True)
    return d or None


# BODS imports run for hours against a remote, nginx-fronted ArcadeDB. A single
# slow batch (index maintenance on 10M+ row types) can blow past the proxy's
# ~60s timeout and 504 — which otherwise kills the whole run. Retry each flush
# with backoff so a transient timeout costs seconds, not the entire import.
# NOTE: node flushes are idempotent (UPSERT WHERE id); edge flushes are NOT, so a
# retry after a 504 that actually committed server-side can duplicate edges —
# collapse them afterwards with POST /scraper/deduplicate-edges.
_FLUSH_ATTEMPTS = 4
_FLUSH_BASE_DELAY = 2.0


def _flush_script(script: str, params: dict) -> list[dict]:
    for attempt in range(_FLUSH_ATTEMPTS):
        try:
            return run_sqlscript(script, params)
        except (RuntimeError, ConnectionError) as exc:
            if attempt == _FLUSH_ATTEMPTS - 1:
                raise
            delay = _FLUSH_BASE_DELAY * (2 ** attempt)
            log.warning("BODS flush failed (attempt %d/%d): %s — retrying in %.0fs",
                        attempt + 1, _FLUSH_ATTEMPTS, str(exc)[:140], delay)
            time.sleep(delay)

# Fast JSON parse for the (huge) NDJSON BODS files; fall back to stdlib if orjson
# isn't installed. orjson is ~2–3× faster and accepts bytes directly.
try:
    import orjson

    def _loads(data):
        return orjson.loads(data)
except ImportError:  # pragma: no cover - orjson is a declared dependency
    def _loads(data):
        return json.loads(data)


class _DiskMap:
    """
    A dict-like ``str -> str`` mapping backed by a throwaway SQLite file, so the
    BODS-id → node-id (and id → name) maps for a multi-GB import don't have to fit
    in RAM — full UK PSC has tens of millions of entries. Supports the operations
    the import uses: ``m[k] = v``, ``m[k]``, ``m.get(k)``, ``k in m``. Not
    thread-safe; call ``close()`` when done to delete the temp file.
    """

    def __init__(self):
        fd, self._path = tempfile.mkstemp(suffix=".bods-idmap.sqlite", dir=_tmp_dir())
        os.close(fd)
        self._con = sqlite3.connect(self._path)
        self._con.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-131072;"
            "CREATE TABLE m (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;")
        self._pending = 0

    def __setitem__(self, k: str, v: str) -> None:
        self._con.execute("INSERT OR REPLACE INTO m VALUES (?, ?)", (k, v))
        self._pending += 1
        if self._pending >= 20000:
            self._con.commit()
            self._pending = 0

    def get(self, k: str, default=None):
        row = self._con.execute("SELECT v FROM m WHERE k = ?", (k,)).fetchone()
        return row[0] if row else default

    def __getitem__(self, k: str) -> str:
        row = self._con.execute("SELECT v FROM m WHERE k = ?", (k,)).fetchone()
        if row is None:
            raise KeyError(k)
        return row[0]

    def __contains__(self, k: str) -> bool:
        return self._con.execute("SELECT 1 FROM m WHERE k = ? LIMIT 1", (k,)).fetchone() is not None

    def __len__(self) -> int:
        return self._con.execute("SELECT count(*) FROM m").fetchone()[0]

    def close(self) -> None:
        try:
            self._con.close()
        finally:
            try:
                os.unlink(self._path)
            except OSError:
                pass

    def __del__(self):  # backstop: drop the temp file even if close() isn't called
        self.close()


class _BatchWriter:
    """
    Buffers BODS node upserts and edge creates, flushing them to ArcadeDB in
    batched ``sqlscript`` requests instead of one HTTP round-trip per record —
    the dominant cost of a full import (~12× faster in local benchmarks).

    Nodes are keyed on a stable ``id`` (the BODS record id) via ``UPSERT WHERE
    id``, so the writer is idempotent and needs no per-record read. On each flush
    nodes are written before edges, so an edge's endpoints exist by the time it
    is created. Edges are bulk-created (ArcadeDB has no ``CREATE EDGE IF NOT
    EXISTS``); a re-import can duplicate active edges — collapse them with
    ``POST /scraper/deduplicate-edges``.
    """

    def __init__(self, batch_size: int = 400):
        self._batch = batch_size
        self._entities: list[tuple[str, dict]] = []
        self._persons:  list[tuple[str, dict]] = []
        self._edges:    list[tuple] = []   # (etype, from_label, from_id, to_label, to_id, props)
        self._claims:   list[dict] = []    # per-source assertions behind those edges
        self._pending = 0

    def _claim(self, kind: str, from_id: str, to_id: str, props: dict) -> None:
        """Record what this source asserts, alongside the edge itself.

        Emitted from the edge writers rather than by their callers, so an
        importer cannot write an edge and forget the evidence for it. Skipped
        when there is no source_id: the claim key is (kind, from, to, source),
        so a claim without a source would collide with every other unsourced
        claim about the same pair.
        """
        source_id = props.get("source_id")
        if not source_id:
            return
        self._claims.append(claim_props(
            kind=kind, from_id=from_id, to_id=to_id, source_id=source_id,
            stake_percent=props.get("stake_percent"),
            voting_power_pct=props.get("voting_power_pct"),
            ownership_type=props.get("ownership_type"),
            role=props.get("role"),
            since=props.get("since"), until=props.get("until"),
            source_url=props.get("source_url"), source_date=props.get("source_date"),
            credibility_score=props.get("credibility_score") or 80,
            filing_type=props.get("filing_type"),
        ))

    def entity(self, node_id: str, props: dict) -> None:
        self._entities.append((node_id, props))
        self._bump()

    def person(self, node_id: str, props: dict) -> None:
        self._persons.append((node_id, props))
        self._bump()

    def owns(self, owner_id: str, owner_label: str, owned_id: str, props: dict) -> None:
        self._edges.append(("OWNS", owner_label, owner_id, "Entity", owned_id, props))
        self._claim(KIND_OWNS, owner_id, owned_id, props)
        self._bump()

    def role(self, person_id: str, entity_id: str, props: dict) -> None:
        self._edges.append(("HAS_ROLE", "Person", person_id, "Entity", entity_id, props))
        self._claim(KIND_ROLE, person_id, entity_id, props)
        self._bump()

    def succeeded_by(self, predecessor_id: str, successor_id: str, props: dict) -> None:
        self._edges.append(("SUCCEEDED_BY", "Entity", predecessor_id, "Entity", successor_id, props))
        self._claim(KIND_SUCCESSION, predecessor_id, successor_id, props)
        self._bump()

    def _bump(self) -> None:
        self._pending += 1
        if self._pending >= self._batch:
            self.flush()

    def flush(self) -> None:
        self._flush_nodes("Entity", self._entities)
        self._flush_nodes("Person", self._persons)
        self._flush_edges()
        self._flush_claims()
        self._entities.clear()
        self._persons.clear()
        self._edges.clear()
        self._claims.clear()
        self._pending = 0

    @staticmethod
    def _flush_nodes(label: str, buf: list) -> None:
        if not buf:
            return
        stmts, params = [], {}
        for k, (node_id, props) in enumerate(buf):
            sets = []
            for name, val in props.items():
                pk = f"{name}__{k}"
                params[pk] = val
                sets.append(f"{name} = :{pk}")
            params[f"id__{k}"] = node_id
            stmts.append(f"UPDATE {label} SET {', '.join(sets)} UPSERT WHERE id = :id__{k};")
        _flush_script("\n".join(stmts), params)

    def _flush_claims(self) -> None:
        """UPSERT the buffered claims on their UNIQUE claim_key.

        Unlike the edges above — which ArcadeDB can only CREATE, so a re-import
        duplicates them and needs a later dedup pass — a claim is keyed on
        (kind, from, to, source), so a source re-asserting the same relationship
        overwrites its own row. Re-imports are idempotent here by construction.

        first_seen_at uses COALESCE against the stored value so it survives
        those updates and records when we first saw the claim, while
        last_seen_at moves. (Verified against a real ArcadeDB: COALESCE works
        inside a SQL UPDATE, which is not something the Cypher dialect's
        limitations let you assume.)
        """
        if not self._claims:
            return
        stmts, params = [], {}
        for k, claim in enumerate(self._claims):
            sets = []
            for name, val in claim.items():
                pk = f"c_{name}__{k}"
                params[pk] = val
                sets.append(f"{name} = :{pk}")
            sets.append(f"first_seen_at = COALESCE(first_seen_at, :c_last_seen_at__{k})")
            stmts.append(
                f"UPDATE Claim SET {', '.join(sets)} UPSERT WHERE claim_key = :c_claim_key__{k};")
        _flush_script("\n".join(stmts), params)

    def _flush_edges(self) -> None:
        if not self._edges:
            return
        stmts, params = [], {}
        for k, (etype, flabel, fid, tlabel, tid, props) in enumerate(self._edges):
            sets = []
            for name, val in props.items():
                pk = f"e_{name}__{k}"
                params[pk] = val
                sets.append(f"{name} = :{pk}")
            params[f"ef__{k}"] = fid
            params[f"et__{k}"] = tid
            setclause = f" SET {', '.join(sets)}" if sets else ""
            stmts.append(
                f"CREATE EDGE {etype} FROM (SELECT FROM {flabel} WHERE id = :ef__{k}) "
                f"TO (SELECT FROM {tlabel} WHERE id = :et__{k}){setclause};")
        _flush_script("\n".join(stmts), params)


def _now_iso() -> str:
    """UTC timestamp for last_scraped_at provenance."""
    return datetime.now(timezone.utc).isoformat()


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


# ── Interest type → Owlgraph ownership_type ─────────────────────────────────────
# None means "derive from stake_percent via derive_ownership_type()".
# "role" signals a HAS_ROLE edge rather than an OWNS edge.

def _max_pct(a: float | None, b: float | None) -> float | None:
    """max of two possibly-None percentages."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)

# ── BODS entityType → Owlgraph entity type ──────────────────────────────────────

# GLEIF's entityType.type is always "registeredEntity", but the LEGAL FORM
# (entityType.details, free text) names foundations, funds and associations. Map
# it to a finer category by keyword — checked in order (first match wins), so a
# statement like "Stiftung des privaten Rechts" → foundation instead of company.
_LEGAL_FORM_TYPE: list[tuple[str, "re.Pattern[str]"]] = [
    ("foundation", re.compile(r"stiftung|stichting|foundation|fondation|fundaci|fundacja|fundo", re.I)),
    ("fund",       re.compile(r"\bfund\b|fonds|sicav|mutual fund|unit trust|investment trust|\btrust\b|\boeic\b|\bfcp\b", re.I)),
    ("nonprofit",  re.compile(r"verein|\be\.?\s?v\.?\b|association|vereniging|associazione|asociaci|onlus|gemeinnütz|non[- ]?profit|\bngo\b|stowarzyszenie", re.I)),
]


def _legal_form_type(details: str | None) -> str | None:
    """Finer entity category from a GLEIF legal-form string, or None if it doesn't
    match a known foundation/fund/association form."""
    if not details:
        return None
    for etype, pat in _LEGAL_FORM_TYPE:
        if pat.search(details):
            return etype
    return None


# ── Database helpers ──────────────────────────────────────────────────────────

def _entity(batch, node_id, name, entity_type, country, founded,
            lei_id, companies_house_id, source_id, credibility_score,
            registered_address=None, source_statement_ids=None,
            hq_city=None, hq_country=None, hq_address=None):
    """Enqueue an Entity upsert (keyed on the stable node id) and return the id.

    ``source_statement_ids`` lists every BODS statement (recordId) that declared
    this entity. For id-less parties collapsed under one name key it holds all
    contributing PSC statement ids, so per-statement provenance survives the
    collapse (the ownership edges keep their own source_url/date independently).

    ``hq_city``/``hq_country`` (when known) put the entity on the map — only added
    when set, so an existing (e.g. GLEIF) HQ is never clobbered with a null."""
    props = {
        "name": name,
        "name_normalized": normalize_entity_name(name),
        "search_text": name,   # FULL_TEXT-indexed field powering /search
        "name_credibility": credibility_score,
        "source_id": source_id,
        "source_statement_ids": source_statement_ids or [],
        "type": entity_type,
        "country": country,
        "founded": founded,
        "lei_id": lei_id,
        "companies_house_id": companies_house_id,
        "registered_address": registered_address,
        "is_nominee": is_nominee_name(name),   # holder-of-record, not a beneficial owner
        "verified": False,
    }
    if hq_city:
        props["hq_city"] = hq_city
    if hq_country:
        props["hq_country"] = hq_country
    if hq_address:
        props["hq_address"] = hq_address
    batch.entity(node_id, props)
    return node_id


def _owns(batch, owner_id, owned_id, stake_percent, ownership_type, since, until,
          source_id, credibility_score, source_url=None, source_date=None, owner_label="Entity",
          voting_power_pct=None, interest_types=None, direct_or_indirect=None, extra=None):
    """Enqueue an OWNS edge (owner is an Entity or a Person; owned is an Entity).

    `stake_percent` is the *economic* holding (shareholding interest);
    `voting_power_pct` the *voting* rights (votingRights interest) — kept separate
    so voting control isn't conflated with the economic stake. `interest_types`
    is the set of BODS interest types behind the edge (shareholding/votingRights/
    appointmentOfBoard/…) for transparency. `direct_or_indirect` (from GLEIF RR-CDF:
    'direct' = directly-consolidated parent, 'indirect' = ultimate parent) marks
    whether the edge is a direct holding or an indirect/ultimate control summary.

    `extra` carries source-specific properties that only some importers have (the
    RR-CDF fold's `also_ultimate` / `ultimate_since` / `ultimate_until`). It is
    merged in only when non-empty, so every other importer keeps writing exactly
    the property set it wrote before rather than gaining nulls on every edge."""
    if owner_label not in ("Entity", "Person"):
        owner_label = "Entity"
    batch.owns(owner_id, owner_label, owned_id, {
        "stake_percent": stake_percent,
        "ownership_type": ownership_type,
        "voting_power_pct": voting_power_pct,
        "interest_types": interest_types or [],
        "direct_or_indirect": direct_or_indirect,
        "since": since,
        "until": until,
        "source_id": source_id,
        "credibility_score": credibility_score,
        "source_url": source_url,
        "source_date": source_date,
        "last_scraped_at": _now_iso(),
        **(extra or {}),
    })


# ── BODS statement processors ─────────────────────────────────────────────────

# ── Streaming helpers ─────────────────────────────────────────────────────────

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


# ── Bulk-load index handling ──────────────────────────────────────────────────

def _bulk_load_secondary_indexes() -> list[str]:
    """Type-level secondary indexes the import never reads (endpoint resolution is
    by `id` via the on-disk id-map, not DB queries). Dropping them removes the
    per-write index maintenance on the huge Entity/Person types — the dominant
    cost of a full load. _rebuild_indexes() rebuilds them afterwards.

    Includes the FULL_TEXT `search_text` indexes: maintaining a Lucene index per
    insert across millions of rows is slow, and (as a corrupted load showed) leaves
    it incomplete — so drop it and REBUILD cleanly at the end instead."""
    from app.db.schema import _FULLTEXT_INDEXES, _INDEXES
    names = [f"{t}[{p}]" for (t, p, _k) in _INDEXES
             if t in ("Entity", "Person") and p != "id"]
    names += [f"{t}[{p}]" for (t, p) in _FULLTEXT_INDEXES if t in ("Entity", "Person")]
    return names


def _drop_secondary_indexes() -> None:
    from app.db.arcadedb import run_sql
    for name in _bulk_load_secondary_indexes():
        try:
            run_sql(f"DROP INDEX `{name}` IF EXISTS")
            log.info("bulk-load: dropped index %s", name)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("bulk-load: could not drop index %s: %s", name, exc)


def _rebuild_indexes() -> None:
    from app.db.schema import ensure_indexes, rebuild_fulltext_indexes
    log.info("bulk-load: rebuilding indexes…")
    res = ensure_indexes()
    log.info("bulk-load: index rebuild — %d applied, %d failed",
             len(res.get("ok", [])), len(res.get("failed", [])))
    # ensure_indexes() only re-CREATEs the FULL_TEXT indexes (a no-op if they already
    # exist → never backfills), so REBUILD them explicitly to fully repopulate /search.
    ft = rebuild_fulltext_indexes()
    log.info("bulk-load: FULL_TEXT rebuild — %d ok, %d failed",
             len(ft.get("ok", [])), len(ft.get("failed", [])))


# ── Core import engine ────────────────────────────────────────────────────────

# ── Public entry points ───────────────────────────────────────────────────────

