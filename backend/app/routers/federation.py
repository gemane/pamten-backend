"""
Trusted-peer federation — step 1 foundation.

Each instance can PUBLISH its ownership graph as a compact snapshot
(GET /federation/export) and PULL a trusted peer's snapshot
(POST /federation/peers/{id}/pull). A pull is one-way and opt-in: the peer's
nodes are upserted (reconciled on their external ids — Wikidata QID, SEC CIK,
LEI, Companies House — else by normalized name), their ownership edges are
written stamped with a Source that represents the peer (carrying the peer's
credibility), and the duplicate scan then merges any high-confidence overlaps.

Deliberately minimal: Entity + Person nodes and OWNS edges only, so the shape
is easy to reason about. Roles/locations and signed provenance are step 2.
Gated behind FEDERATION_ENABLED.
"""
from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone
import logging
import uuid

import httpx

from app.auth.dependencies import require_admin, require_contributor
from app.config import settings
from app.database import db
from app.models.federation import PeerCreate
from app.scraper.mapper import normalize_entity_name
from app.routers.persons import deduplicate_high_confidence

router = APIRouter(prefix="/federation", tags=["Federation"])
log = logging.getLogger(__name__)

EXPORT_FORMAT = "pamten-federation"
EXPORT_VERSION = 1


def _require_enabled():
    if not settings.FEDERATION_ENABLED:
        raise HTTPException(status_code=403,
            detail="Federation is disabled. Set FEDERATION_ENABLED=true to enable.")


# ── Peer registry ─────────────────────────────────────────────────────────────

@router.post("/peers")
def add_peer(data: PeerCreate, _: dict = Depends(require_admin)):
    """Register a trusted peer to pull from."""
    _require_enabled()
    peer_id = str(uuid.uuid4())
    with db.get_session() as session:
        session.run(
            "CREATE (p:Peer {id:$id, name:$name, base_url:$url, credibility_score:$cred, "
            "auth_token:$tok, enabled:true, created_at:$at})",
            id=peer_id, name=data.name, url=data.base_url.rstrip("/"),
            cred=data.credibility_score, tok=data.auth_token or "",
            at=datetime.now(timezone.utc).isoformat())
    return {"id": peer_id, "name": data.name, "base_url": data.base_url.rstrip("/")}


@router.get("/peers")
def list_peers(_: dict = Depends(require_contributor)):
    """List trusted peers (auth tokens are never returned)."""
    _require_enabled()
    with db.get_session() as session:
        peers = [
            {"id": r.get("id"), "name": r.get("name"), "base_url": r.get("base_url"),
             "credibility_score": r.get("cred"), "enabled": r.get("enabled"),
             "has_token": bool(r.get("tok")), "created_at": r.get("at")}
            for r in session.run(
                "MATCH (p:Peer) RETURN p.id AS id, p.name AS name, p.base_url AS base_url, "
                "p.credibility_score AS cred, p.enabled AS enabled, p.auth_token AS tok, "
                "p.created_at AS at")
        ]
    peers.sort(key=lambda p: p["created_at"] or "")
    return {"count": len(peers), "peers": peers}


@router.delete("/peers/{peer_id}")
def remove_peer(peer_id: str, _: dict = Depends(require_admin)):
    """Remove a trusted peer (does not touch data already pulled from it)."""
    _require_enabled()
    with db.get_session() as session:
        session.run("MATCH (p:Peer {id:$id}) DETACH DELETE p", id=peer_id)
    return {"message": "Peer removed", "id": peer_id}


# ── Export (publish this instance's graph) ────────────────────────────────────

def build_export() -> dict:
    """Serialize this instance's Entity/Person nodes and OWNS edges to a snapshot."""
    with db.get_session() as session:
        entities = [
            {"name": r.get("name"), "type": r.get("type"), "country": r.get("country"),
             "founded": r.get("founded"), "wikidata_id": r.get("wd"), "sec_cik": r.get("cik"),
             "lei_id": r.get("lei"), "companies_house_id": r.get("ch")}
            for r in session.run(
                "MATCH (e:Entity) RETURN e.name AS name, e.type AS type, e.country AS country, "
                "e.founded AS founded, e.wikidata_id AS wd, e.sec_cik AS cik, "
                "e.lei_id AS lei, e.companies_house_id AS ch")
        ]
        persons = [
            {"full_name": r.get("full_name"), "first_name": r.get("first"), "last_name": r.get("last"),
             "wikidata_id": r.get("wd"), "sec_cik": r.get("cik"), "birth_date": r.get("bd"),
             "birth_place": r.get("bp"), "nationality": r.get("nat")}
            for r in session.run(
                "MATCH (p:Person) RETURN p.full_name AS full_name, p.first_name AS first, "
                "p.last_name AS last, p.wikidata_id AS wd, p.sec_cik AS cik, "
                "p.birth_date AS bd, p.birth_place AS bp, p.nationality AS nat")
        ]
        ownerships: list[dict] = []
        for owner_kind, pat in (("entity", "(a:Entity)-[r:OWNS]->(b:Entity)"),
                                ("person", "(a:Person)-[r:OWNS]->(b:Entity)")):
            for r in session.run(
                f"MATCH {pat} RETURN "
                "a.wikidata_id AS a_wd, a.sec_cik AS a_cik, a.lei_id AS a_lei, "
                "a.companies_house_id AS a_ch, a.name AS a_name, a.full_name AS a_full, "
                "b.wikidata_id AS b_wd, b.sec_cik AS b_cik, b.lei_id AS b_lei, "
                "b.companies_house_id AS b_ch, b.name AS b_name, "
                "r.stake_percent AS stake, r.ownership_type AS otype, "
                "r.source_url AS surl, r.source_date AS sdate"):
                ownerships.append({
                    "owner": {"kind": owner_kind, "wikidata_id": r.get("a_wd"),
                              "sec_cik": r.get("a_cik"), "lei_id": r.get("a_lei"),
                              "companies_house_id": r.get("a_ch"),
                              "name": r.get("a_name") or r.get("a_full")},
                    "owned": {"kind": "entity", "wikidata_id": r.get("b_wd"),
                              "sec_cik": r.get("b_cik"), "lei_id": r.get("b_lei"),
                              "companies_house_id": r.get("b_ch"), "name": r.get("b_name")},
                    "stake_percent": r.get("stake"), "ownership_type": r.get("otype"),
                    "source_url": r.get("surl"), "source_date": r.get("sdate"),
                })
    return {
        "format": EXPORT_FORMAT, "version": EXPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entities": entities, "persons": persons, "ownerships": ownerships,
    }


@router.get("/export")
def export_snapshot(_: dict = Depends(require_contributor)):
    """Publish this instance's ownership graph for trusted peers to pull."""
    _require_enabled()
    return build_export()


# ── Pull (import a peer's snapshot) ───────────────────────────────────────────

def _ensure_peer_source(session, name: str, credibility: int) -> str:
    rec = session.run("MATCH (s:Source {name:$name}) RETURN s.id AS id", name=name).single()
    if rec:
        return rec["id"]
    sid = str(uuid.uuid4())
    session.run(
        "CREATE (s:Source {id:$id, name:$name, type:'peer', credibility_score:$cred, url:$url})",
        id=sid, name=name, cred=credibility, url="")
    return sid


def _upsert_entity(session, ref: dict, source_id: str) -> str | None:
    name = (ref.get("name") or "").strip()
    if not name:
        return None
    nn = normalize_entity_name(name)
    rec = session.run(
        "MATCH (e:Entity) WHERE ($wd IS NOT NULL AND e.wikidata_id=$wd) "
        "OR ($cik IS NOT NULL AND e.sec_cik=$cik) OR ($lei IS NOT NULL AND e.lei_id=$lei) "
        "OR ($ch IS NOT NULL AND e.companies_house_id=$ch) OR e.name_normalized=$nn "
        "RETURN e.id AS id LIMIT 1",
        wd=ref.get("wikidata_id"), cik=ref.get("sec_cik"), lei=ref.get("lei_id"),
        ch=ref.get("companies_house_id"), nn=nn).single()
    if rec:
        return rec["id"]
    eid = str(uuid.uuid4())
    session.run(
        "CREATE (e:Entity {id:$id, name:$name, name_normalized:$nn, type:$type, "
        "country:$country, wikidata_id:$wd, sec_cik:$cik, lei_id:$lei, "
        "companies_house_id:$ch, source_id:$sid, verified:false})",
        id=eid, name=name, nn=nn, type=ref.get("type") or "company",
        country=ref.get("country"), wd=ref.get("wikidata_id"), cik=ref.get("sec_cik"),
        lei=ref.get("lei_id"), ch=ref.get("companies_house_id"), sid=source_id)
    return eid


def _upsert_person(session, ref: dict, source_id: str) -> str | None:
    full = (ref.get("full_name") or ref.get("name") or "").strip()
    if not full:
        return None
    rec = session.run(
        "MATCH (p:Person) WHERE ($wd IS NOT NULL AND p.wikidata_id=$wd) "
        "OR ($cik IS NOT NULL AND p.sec_cik=$cik) OR p.full_name=$full "
        "RETURN p.id AS id LIMIT 1",
        wd=ref.get("wikidata_id"), cik=ref.get("sec_cik"), full=full).single()
    if rec:
        return rec["id"]
    pid = str(uuid.uuid4())
    session.run(
        "CREATE (p:Person {id:$id, full_name:$full, first_name:$first, last_name:$last, "
        "wikidata_id:$wd, sec_cik:$cik, birth_date:$bd, birth_place:$bp, "
        "nationality:$nat, source_id:$sid, alias:[], verified:false})",
        id=pid, full=full, first=ref.get("first_name"), last=ref.get("last_name"),
        wd=ref.get("wikidata_id"), cik=ref.get("sec_cik"), bd=ref.get("birth_date"),
        bp=ref.get("birth_place"), nat=ref.get("nationality"), sid=source_id)
    return pid


def import_snapshot(data: dict, source_name: str, credibility: int) -> dict:
    """Upsert a peer snapshot's nodes/edges, attributed to the peer's Source."""
    if data.get("format") != EXPORT_FORMAT:
        raise ValueError(f"Unrecognized export format: {data.get('format')!r}")
    counts = {"entities": 0, "persons": 0, "ownerships": 0, "skipped": 0}
    with db.get_session() as session:
        source_id = _ensure_peer_source(session, source_name, credibility)
        for e in data.get("entities", []):
            if _upsert_entity(session, e, source_id):
                counts["entities"] += 1
        for p in data.get("persons", []):
            if _upsert_person(session, p, source_id):
                counts["persons"] += 1
        for o in data.get("ownerships", []):
            owner, owned = o.get("owner") or {}, o.get("owned") or {}
            oid = (_upsert_person(session, owner, source_id) if owner.get("kind") == "person"
                   else _upsert_entity(session, owner, source_id))
            tid = _upsert_entity(session, owned, source_id)
            if not oid or not tid:
                counts["skipped"] += 1
                continue
            session.run(
                "MATCH (a {id:$oid}), (b {id:$tid}) MERGE (a)-[r:OWNS]->(b) "
                "SET r.stake_percent = COALESCE(r.stake_percent, $stake), "
                "    r.ownership_type = COALESCE(r.ownership_type, $otype), "
                "    r.source_id = $sid, "
                "    r.source_url = COALESCE($surl, r.source_url), "
                "    r.source_date = COALESCE($sdate, r.source_date)",
                oid=oid, tid=tid, stake=o.get("stake_percent"),
                otype=o.get("ownership_type"), sid=source_id,
                surl=o.get("source_url"), sdate=o.get("source_date"))
            counts["ownerships"] += 1
    return counts


@router.post("/peers/{peer_id}/pull")
def pull_peer(peer_id: str, _: dict = Depends(require_admin)):
    """
    Pull a trusted peer's published snapshot, import it (attributed to the peer),
    then reconcile via the high-confidence duplicate merge. One-way and opt-in.
    """
    _require_enabled()
    with db.get_session() as session:
        rec = session.run(
            "MATCH (p:Peer {id:$id}) RETURN p.name AS name, p.base_url AS url, "
            "p.credibility_score AS cred, p.auth_token AS tok, p.enabled AS enabled",
            id=peer_id).single()
    if not rec:
        raise HTTPException(status_code=404, detail="Peer not found")
    if rec.get("enabled") is False:
        raise HTTPException(status_code=400, detail="Peer is disabled")

    url = f"{rec['url'].rstrip('/')}/federation/export"
    headers = {"Authorization": f"Bearer {rec['tok']}"} if rec.get("tok") else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=120, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the caller
        log.warning("Peer pull failed (%s): %s", rec["name"], exc)
        raise HTTPException(status_code=502, detail=f"Could not pull from peer: {exc}")

    try:
        counts = import_snapshot(data, source_name=f"Peer: {rec['name']}",
                                 credibility=rec.get("cred") or 60)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    dedup = deduplicate_high_confidence(apply=True)
    return {"peer": rec["name"], "imported": counts,
            "deduplication": {"merged_count": dedup["merged_count"],
                              "review_count": dedup["review_count"]}}
