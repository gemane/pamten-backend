"""
The source catalogue and its one node per source.

Every source this instance draws on has ONE `Source` node, keyed by a stable
id (`gleif`, `sec-edgar`, `uk-psc`, `wikidata`, `opencorporates`) — the same
id in dev, in production and across federation peers — carrying its
provenance fields (name, url, credibility, type) AND its run-time switches
(`enabled`, `data_mode`). Until 2026-10-24 those were two node types: a
`Source` with a random uuid per environment (so a claim's `source_id` meant
nothing outside the database that minted it) and a `ScraperSource` keyed by
the catalogue slug for the toggle; `manage.py heal-source-ids` folds a
database written that way.

The catalogue KEY (`sec_edgar`, `bods_gleif`, …) stays the toggle name in the
API and the config flags; the node's `id` is the entry's `id`.
The switches are independent of the master SCRAPER_ENABLED env flag.
"""
import logging
import re

from fastapi import APIRouter, HTTPException, Depends
from app.database import db
from app.config import settings
from app.auth.dependencies import require_admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/scraper/sources", tags=["Scraper"])

# The catalogue of everything this instance can draw on. One entry per source, and
# the single definition of its metadata: `runner.py` reads the label/url/credibility
# from here rather than keeping its own copies, so the public catalogue and the
# provenance stamped onto scraped data can never disagree.
#
# KIND — "instant" (query-driven, per-company, run on demand by the search-triggered
# scrape) or "bulk" (whole-dataset import — GLEIF/UK PSC, never triggered by search).
#
# CREDIBILITY — 0-100, the tie-breaker when two sources claim different things about
# the same relationship (see app/claims.py). `quality` is the band it belongs to,
# which is what the UI shows: a bare "98" says nothing without "legally mandated".
#
# ⚠️ `enabled` (a per-source toggle, stored on the Source node) governs whether
# a source RUNS — it says nothing about whether its data is already in the graph. Both
# bulk sources are toggled off while their data is loaded and in active use. Do not
# present the toggle as "this source is not used".
KNOWN_SOURCES = {
    "wikidata": {
        "id": "wikidata",
        "type": "knowledge_base",
        "kind": "instant",
        "label": "Wikidata",
        "url": "https://www.wikidata.org",
        "credibility": 80,
        "quality": "community",
        "description": "Wikidata — structured corporate data via SPARQL",
        "region": "Global",
        "coverage": "Descriptions, people, logos and websites for globally notable "
                    "companies — the soft layer no register carries",
    },
    "sec_edgar": {
        "id": "sec-edgar",
        "type": "register",
        "kind": "instant",
        "label": "SEC EDGAR",
        "url": "https://www.sec.gov/edgar",
        "credibility": 98,
        "quality": "statutory",
        "description": "SEC EDGAR — legally required US ownership filings (SC 13D/13G, Form 3/4)",
        "region": "US",
        "coverage": "Ownership of US-listed companies — 13D/G control stakes, 13F "
                    "institutional holdings, Form 3/4 insider filings, Exhibit 21 "
                    "subsidiary lists — and private-company boards from Form D",
    },
    "open_corporates": {
        "id": "opencorporates",
        "type": "register",
        "kind": "instant",
        "label": "OpenCorporates",
        "url": "https://opencorporates.com",
        "credibility": 85,
        "quality": "aggregated",
        "description": "OpenCorporates — official company registers from 200+ jurisdictions",
        "region": "200+ jurisdictions",
        "coverage": "Company registrations and register numbers, looked up on demand "
                    "for jurisdictions this platform does not read directly",
    },
    "bods_gleif": {
        "id": "gleif",
        "type": "register",
        "kind": "bulk",
        "label": "GLEIF",
        "url": "https://www.gleif.org",
        "credibility": 92,
        "quality": "official",
        "description": "GLEIF – Global Legal Entity Identifier "
                       "(corporate ownership, worldwide, CC0)",
        "region": "Global",
        "coverage": "Legal entities worldwide that hold an LEI, and their accounting "
                    "consolidation relationships (who consolidates whom)",
    },
    "bods_uk_psc": {
        "id": "uk-psc",
        "type": "register",
        "kind": "bulk",
        "label": "UK PSC",
        "url": "https://www.gov.uk/government/publications/persons-with-significant-control-register",
        "credibility": 97,
        "quality": "statutory",
        "description": "UK People with Significant Control Register "
                       "(beneficial ownership, UK companies, CC0)",
        "region": "GB",
        "coverage": "Beneficial owners — the persons and companies with significant "
                    "control over UK-registered companies",
    },
}


#: catalogue key → the Source node's stable id, and back
SOURCE_ID: dict[str, str] = {key: meta["id"] for key, meta in KNOWN_SOURCES.items()}
_KEY_BY_ID: dict[str, str] = {meta["id"]: key for key, meta in KNOWN_SOURCES.items()}
_KEY_BY_LABEL: dict[str, str] = {meta["label"]: key for key, meta in KNOWN_SOURCES.items()}


def source_key_for(label_or_id: str | None) -> str | None:
    """The catalogue key behind a Source node's label ("GLEIF") or id ("gleif");
    None for a source outside the catalogue (a peer, a manual one)."""
    if not label_or_id:
        return None
    return _KEY_BY_ID.get(label_or_id) or _KEY_BY_LABEL.get(label_or_id)


def slug_id(name: str) -> str:
    """A stable id for a source outside the catalogue, from its name:
    lower-case, runs of anything but letters and digits become one hyphen
    ("Peer: Signed" → "peer-signed"). The same name gives the same id on
    every instance, which is the point."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "source"


def source_id_for(name: str, type_: str | None = None) -> str:
    """The id a Source node named `name` gets: the catalogue's for a catalogue
    label, `peer:<slug>` for a federation peer, else the slug of the name."""
    key = source_key_for(name)
    if key:
        return SOURCE_ID[key]
    if type_ == "peer":
        return "peer:" + slug_id(re.sub(r"^peer:\s*", "", name, flags=re.I))
    return slug_id(name)


_DATA_MODES = ("full", "claims_only")


def default_data_mode(name: str) -> str:
    """The mode a source runs in before its catalogue row says otherwise.

    Wikidata's comes from ``SCRAPER_WIKIDATA_DATA_MODE`` and is ``claims_only``
    unless that says ``full``; every other source is ``full``. This is what a
    fresh database seeds the row with AND what the writers obey while no row
    exists yet (a rebuild's scrape chain runs before anything has called
    `_ensure_sources`). An empty or unknown value reads as ``claims_only`` with
    a warning on the unknown one — a typo must not widen what a source may
    draw, and ``claims_only`` is the documented default."""
    if name != "wikidata":
        return "full"
    mode = (settings.SCRAPER_WIKIDATA_DATA_MODE or "claims_only").strip().lower()
    if mode not in _DATA_MODES:
        log.warning("SCRAPER_WIKIDATA_DATA_MODE=%r is not one of %s — reading it as 'claims_only'",
                    settings.SCRAPER_WIKIDATA_DATA_MODE, _DATA_MODES)
        return "claims_only"
    return mode


#: The MERGE every catalogue entry's node goes through: the switches filled
#: only where empty (an admin's later flip in the UI survives restarts, and a
#: node the WRITER created first — stamping provenance before anything listed
#: the catalogue — gets its defaults on the first listing), the catalogue
#: fields refreshed every time.
_ENSURE_SOURCE = """
    MERGE (s:Source {id: $id})
    SET s.enabled = COALESCE(s.enabled, true), s.data_mode = COALESCE(s.data_mode, $mode),
        s.registry_key = $key, s.name = $label, s.url = $url,
        s.credibility_score = $cred, s.type = $type, s.kind = $kind,
        s.description = $desc
"""


def _ensure_sources():
    """One Source node per catalogue entry, keyed by its stable id."""
    with db.get_session() as session:
        for key, meta in KNOWN_SOURCES.items():
            session.run(_ENSURE_SOURCE, id=meta["id"], key=key, label=meta["label"],
                        url=meta["url"], cred=meta["credibility"], type=meta["type"],
                        kind=meta["kind"], desc=meta["description"],
                        mode=default_data_mode(key))


def get_source_enabled(name: str) -> bool:
    _ensure_sources()
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {id: $id}) RETURN s.enabled AS enabled",
            id=SOURCE_ID.get(name, name),
        ).single()
        return bool(rec["enabled"]) if rec else False


@router.get("")
def list_sources():
    _ensure_sources()
    with db.get_session() as session:
        records = session.run(
            "MATCH (s:Source) WHERE s.registry_key IS NOT NULL "
            "RETURN s.registry_key AS name, s.id AS id, s.enabled AS enabled, "
            "s.description AS description, s.kind AS kind, s.data_mode AS data_mode"
        )
        rows = []
        for r in records:
            meta = KNOWN_SOURCES.get(r["name"], {})
            rows.append({
                "name": r["name"],
                "id": r.get("id") or meta.get("id"),
                "enabled": bool(r["enabled"]),
                "description": r["description"],
                # `kind` may be null on rows created before this field existed → fall
                # back to the declared kind (or "instant" for anything unknown).
                "kind": r["kind"] or meta.get("kind", "instant"),
                # Catalogue metadata, served to everyone: this endpoint is public, and
                # where the data comes from and how far to trust it is the case for the
                # whole platform rather than something to keep behind a login.
                "label": meta.get("label", r["name"]),
                "url": meta.get("url"),
                "credibility": meta.get("credibility"),
                "quality": meta.get("quality"),
                # What the source is authoritative for, and where — catalogue-
                # served like label/url, so edits apply without touching the
                # DB node (whose description is ON CREATE only).
                "region": meta.get("region"),
                "coverage": meta.get("coverage"),
                # `full` draws edges; `claims_only` records claims and enriches
                # entities but never draws structure. Null on pre-field rows
                # reads as the source's configured default (full for all but
                # Wikidata: SCRAPER_WIKIDATA_DATA_MODE, claims_only by default).
                "data_mode": r["data_mode"] or default_data_mode(r["name"]),
            })
        return rows



# data_mode per Source-NODE id, cached briefly: the bulk importers consult
# this once per edge, and a 60-second lag on an admin mode flip is invisible
# while a per-edge point read at GLEIF scale is not. Same pattern as the
# stats cache.
# "at" is None when the cache is empty/invalidated — NOT 0.0: time.monotonic()
# counts from boot, so on a fresh container (CI, a new Render instance) it is
# small and `now - 0.0 < TTL` reads an empty cache as fresh. That served a
# stale empty map and made invalidation a no-op for the first minute of
# process life.
_MODE_CACHE: dict = {"at": None, "by_source_id": {}}
_MODE_CACHE_TTL = 60.0


def _mode_by_source_id() -> dict:
    """Source-node id → data_mode, for every catalogue source.

    One read of the Source nodes: a row's own mode when it has one, else the
    source's configured default (`default_data_mode`) — so a rebuilt database
    obeys SCRAPER_WIKIDATA_DATA_MODE from its first write, when the writer
    has created the node but nothing has listed or toggled it yet. A node
    outside the catalogue (a peer's) reads as full — restriction is opt-in.
    The catalogue key is read from the node (`registry_key`) and, on a node
    the writer created before that field existed, from its label."""
    import time
    now = time.monotonic()
    if _MODE_CACHE["at"] is not None and now - _MODE_CACHE["at"] < _MODE_CACHE_TTL:
        return _MODE_CACHE["by_source_id"]
    by_id: dict = {}
    try:
        with db.get_session() as session:
            for r in session.run("MATCH (s:Source) RETURN s.id AS id, s.name AS name, "
                                 "s.registry_key AS key, s.data_mode AS data_mode"):
                key = r.get("key") or source_key_for(r.get("name"))
                if not key:
                    continue
                by_id[r["id"]] = r.get("data_mode") or default_data_mode(key)
    except Exception:  # noqa: BLE001 - fail open, and CACHE the failure:
        # a mocked suite (or a flaky DB) must produce one failed read per TTL,
        # not one per edge — repeated failed reads are how the ArcadeDB
        # brute-force lockout gets triggered from tests.
        by_id = {}
    _MODE_CACHE["at"] = now
    _MODE_CACHE["by_source_id"] = by_id
    return by_id


def edge_writes_suppressed(source_id: str | None) -> bool:
    """True when the source that owns ``source_id`` is in claims-only mode.

    Consulted by every edge writer AFTER the claim is recorded: a claims-only
    source may assert (provenance kept, corroboration visible) but not draw
    structure. Fail-open on any error — a mode lookup must never break a
    scrape."""
    if not source_id:
        return False
    try:
        return _mode_by_source_id().get(source_id) == "claims_only"
    except Exception:  # noqa: BLE001 - never break a write over a mode read
        return False


@router.patch("/{name}/mode")
def set_source_mode(name: str, mode: str, _: dict = Depends(require_admin)):
    """Set a source's data mode: ``full`` draws edges, ``claims_only``
    records claims and enriches entities but never draws OWNERSHIP structure
    (OWNS/SUCCEEDED_BY). People-roles (HAS_ROLE) are always drawn.

    Does NOT touch existing edges — removing what a source already drew is
    the explicitly destructive POST /{name}/sweep-edges. Reverting is
    mode=full plus a re-scrape/re-import."""
    if name not in KNOWN_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    if mode not in _DATA_MODES:
        raise HTTPException(status_code=422,
                            detail=f"mode must be one of {_DATA_MODES}")
    _ensure_sources()
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {id: $id}) SET s.data_mode = $mode "
            "RETURN s.data_mode AS data_mode",
            id=SOURCE_ID[name], mode=mode).single()
        if not rec:
            raise HTTPException(status_code=404, detail="Source not found")
    _MODE_CACHE["at"] = None   # a mode flip should not wait out the cache
    return {"name": name, "data_mode": rec["data_mode"]}


@router.post("/{name}/sweep-edges")
def sweep_source_edges(name: str, confirm: str, _: dict = Depends(require_admin)):
    """Delete every ownership-structure edge this source drew — nodes,
    claims and HAS_ROLE edges stay.

    The retro half of claims-only: flipping the mode stops new structure,
    this removes the old. Destructive, so the wipe-source retype guard,
    HTTP-shaped: ``confirm`` must equal the source name exactly."""
    if name not in KNOWN_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    if confirm != name:
        raise HTTPException(status_code=422,
                            detail="confirm must equal the source name exactly")
    from app.scraper.maintenance import wipe_source_edges
    label = KNOWN_SOURCES[name]["label"]
    try:
        # Ownership STRUCTURE only — HAS_ROLE stays, mirroring what claims-only
        # suppresses (people are not structure; see graph_writer._upsert_role).
        result = wipe_source_edges(label, edge_types=[
            "OWNS", "SUCCEEDED_BY", "RELATED_TO", "DUAL_LISTED_WITH"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"name": name, "source": label, **result}


@router.patch("/{name}/toggle")
def toggle_source(name: str, _: dict = Depends(require_admin)):
    if name not in KNOWN_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {name}")
    _ensure_sources()
    with db.get_session() as session:
        rec = session.run(
            """
            MATCH (s:Source {id: $id})
            SET s.enabled = NOT s.enabled
            RETURN s.enabled AS enabled
            """,
            id=SOURCE_ID[name],
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"name": name, "enabled": bool(rec["enabled"])}
