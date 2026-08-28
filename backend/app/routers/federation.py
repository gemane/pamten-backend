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

⚠️ ON HOLD — FEDERATION_ENABLED is false, and the published privacy pages say
nothing about federation because of it. That silence is only truthful while this
stays off, so **re-enabling it means updating /legal/privacy.html in the same
change**. A published policy that omits a live disclosure of personal data is
worse than no policy.

What has to be decided before it goes back on: the export below ships every
`Person` in the database — full_name, birth_date, birth_place, nationality — plus
every (Person)-[:OWNS]->(Entity) edge, to any trusted peer. Those people never
gave us their data and have no relationship with this instance, so sharing it
makes each peer an independent controller and brings a controller-to-controller
arrangement, third-country transfer tools, and an Art. 17(2) duty to propagate
erasure to peers. Restricting the snapshot to Entity nodes and entity→entity
edges removes personal data from federation altogether and avoids all of that;
half-measures (e.g. names only) keep every obligation. No peer has ever been
registered, so nothing has been disclosed to date.
"""
from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone
import ipaddress
import logging
import uuid
from urllib.parse import urlparse

import httpx

from app.auth.dependencies import require_admin, require_contributor
from app.config import settings
from app.claims import KIND_OWNS, record_claim
from app.database import db
from app.entity_resolution import resolve_entity_id
from app.models.federation import PeerCreate
from app.scraper.mapper import coherent_ownership_type, normalize_entity_name
from app.routers.persons import deduplicate_high_confidence
from app import federation_keys

router = APIRouter(prefix="/federation", tags=["Federation"])
log = logging.getLogger(__name__)

EXPORT_FORMAT = "owlgraph-federation"
EXPORT_VERSION = 1


#: Hard hold, independent of configuration. While this is True, federation is off
#: no matter what FEDERATION_ENABLED says, and `main.py` does not even mount the
#: router — the endpoints 404 and do not appear in the OpenAPI schema.
#:
#: The env var alone is too thin a guard for what is behind it: the export ships
#: every Person in the database to any registered peer, and the published privacy
#: pages say nothing about federation *because* it is off. An accidental flip in a
#: dashboard would not just disclose personal data, it would make a published
#: policy untrue.
#:
#: Lifting this is therefore deliberately awkward: edit the source, open a PR, get
#: it reviewed, and update /legal/privacy.html in the same change. Read the module
#: docstring above first — it records what has to be decided before federation
#: comes back.
FEDERATION_ON_HOLD = True


def _require_enabled():
    if FEDERATION_ON_HOLD:
        raise HTTPException(status_code=403,
            detail="Federation is on hold in this build and cannot be enabled by configuration.")
    if not settings.FEDERATION_ENABLED:
        raise HTTPException(status_code=403,
            detail="Federation is disabled. Set FEDERATION_ENABLED=true to enable.")


# Internal address ranges that must never be reachable via a peer pull.
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain")
_BLOCKED_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def _validate_peer_url(url: str) -> None:
    """Reject URLs that could be used to probe internal infrastructure (SSRF).

    Rules:
    - Scheme must be ``https`` (no plain HTTP, no ``file://``, ``gopher://``, etc.)
    - Hostname must be present and contain at least one dot (bare labels are
      internal DNS names, e.g. ``arcadedb``, ``postgres``).
    - Known internal hostnames are rejected (``localhost`` and its IPv6 aliases).
    - Hostnames ending with ``.local``, ``.internal``, ``.localhost``, or
      ``.localdomain`` are rejected (mDNS / split-horizon DNS / Docker networks).
    - If the hostname is an IP address, it must be a globally-routable unicast
      address — loopback (127.x, ::1), link-local (169.254.x — AWS/GCP/Azure
      metadata endpoint), private RFC-1918, and reserved ranges are all blocked.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid peer URL: {exc}") from exc

    if parsed.scheme != "https":
        raise HTTPException(
            status_code=400,
            detail="Peer base_url must use HTTPS — plain HTTP and other schemes are not allowed.",
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="Peer base_url must include a hostname.")

    if host in _BLOCKED_HOSTNAMES:
        raise HTTPException(status_code=400,
                            detail="Peer base_url must be a public address.")

    for suffix in _BLOCKED_SUFFIXES:
        if host.endswith(suffix):
            raise HTTPException(status_code=400,
                                detail="Peer base_url must be a public address.")

    # Bare hostnames (no dot) are internal DNS labels (e.g. Docker service names).
    if "." not in host:
        raise HTTPException(status_code=400,
                            detail="Peer base_url must be a public address.")

    # Numeric IP addresses — block all non-globally-routable addresses.
    try:
        addr = ipaddress.ip_address(host)
        if not addr.is_global:
            raise HTTPException(status_code=400,
                                detail="Peer base_url must be a public address.")
    except ValueError:
        pass  # hostname (not a bare IP) — the checks above are sufficient


@router.get("/status")
def federation_status(_: dict = Depends(require_contributor)):
    """Whether federation is on, plus what this instance would publish. Not gated
    by the flag (returns enabled:false) so the UI can decide what to show.

    The hold is checked first and reported as simply off. This endpoint is the one
    place that answers rather than refusing, so it must not say "on" while every
    other route 404s — and it must not count what would be published when nothing
    can be.
    """
    if FEDERATION_ON_HOLD or not settings.FEDERATION_ENABLED:
        return {"enabled": False, "entities": 0, "persons": 0, "ownerships": 0}
    with db.get_session() as session:
        entities   = session.run("MATCH (e:Entity) RETURN count(e) AS c").single().get("c") or 0
        persons    = session.run("MATCH (p:Person) RETURN count(p) AS c").single().get("c") or 0
        ownerships = session.run("MATCH (a)-[r:OWNS]->(b) RETURN count(r) AS c").single().get("c") or 0
    return {"enabled": True, "entities": entities, "persons": persons, "ownerships": ownerships}


# ── Peer registry ─────────────────────────────────────────────────────────────

@router.post("/peers")
def add_peer(data: PeerCreate, _: dict = Depends(require_admin)):
    """Register a trusted peer to pull from."""
    _require_enabled()
    _validate_peer_url(data.base_url)
    peer_id = str(uuid.uuid4())
    with db.get_session() as session:
        session.run(
            "CREATE (p:Peer {id:$id, name:$name, base_url:$url, credibility_score:$cred, "
            "auth_token:$tok, public_key:$pk, enabled:true, created_at:$at})",
            id=peer_id, name=data.name, url=data.base_url.rstrip("/"),
            cred=data.credibility_score, tok=data.auth_token or "",
            pk=(data.public_key or "").strip(),
            at=datetime.now(timezone.utc).isoformat())
    return {"id": peer_id, "name": data.name, "base_url": data.base_url.rstrip("/"),
            "has_public_key": bool((data.public_key or "").strip())}


@router.get("/peers")
def list_peers(_: dict = Depends(require_contributor)):
    """List trusted peers (auth tokens are never returned)."""
    _require_enabled()
    with db.get_session() as session:
        peers = [
            {"id": r.get("id"), "name": r.get("name"), "base_url": r.get("base_url"),
             "credibility_score": r.get("cred"), "enabled": r.get("enabled"),
             "has_token": bool(r.get("tok")), "has_public_key": bool(r.get("pk")),
             "created_at": r.get("at")}
            for r in session.run(
                "MATCH (p:Peer) RETURN p.id AS id, p.name AS name, p.base_url AS base_url, "
                "p.credibility_score AS cred, p.enabled AS enabled, p.auth_token AS tok, "
                "p.public_key AS pk, p.created_at AS at")
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
                # Voting groups are not exported: they carry no identifier, so a
                # peer importing one would resolve it by normalised name onto
                # whatever it already has under that name. They are also local
                # derivations from SEC filings, which any peer can derive itself.
                "MATCH (e:Entity) WHERE e.type <> 'voting_group' "
                "RETURN e.name AS name, e.type AS type, e.country AS country, "
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
    """Publish this instance's ownership graph, signed if a signing key is set."""
    _require_enabled()
    snapshot = build_export()
    snapshot.update(federation_keys.sign(snapshot))   # adds signature/key_id/algorithm, or nothing
    return snapshot


@router.get("/public-key")
def public_key(_: dict = Depends(require_contributor)):
    """This instance's signing public key, for peers to register and verify our exports."""
    _require_enabled()
    pub = federation_keys.public_key_b64()
    if not pub:
        return {"signing_enabled": False}
    return {"signing_enabled": True, "algorithm": federation_keys.ALGORITHM,
            "public_key": pub, "key_id": federation_keys.fingerprint(pub)}


# ── Pull (import a peer's snapshot) ───────────────────────────────────────────

def _ensure_peer_source(session, name: str, credibility: int, verified: bool, key_id: str) -> str:
    rec = session.run("MATCH (s:Source {name:$name}) RETURN s.id AS id", name=name).single()
    if rec:
        session.run("MATCH (s:Source {id:$id}) SET s.verified=$v, s.key_id=$k",
                    id=rec["id"], v=verified, k=key_id)
        return rec["id"]
    sid = str(uuid.uuid4())
    session.run(
        "CREATE (s:Source {id:$id, name:$name, type:'peer', credibility_score:$cred, "
        "url:$url, verified:$v, key_id:$k})",
        id=sid, name=name, cred=credibility, url="", v=verified, k=key_id)
    return sid


def _upsert_entity(session, ref: dict, source_id: str, credibility: int = 60) -> str | None:
    name = (ref.get("name") or "").strip()
    if not name:
        return None
    nn = normalize_entity_name(name)
    # Sequential indexed lookups — an OR across these fields full-scans the
    # Entity type on ArcadeDB (see app.entity_resolution).
    found = resolve_entity_id(
        session,
        wikidata_id=ref.get("wikidata_id"), sec_cik=ref.get("sec_cik"),
        lei_id=ref.get("lei_id"), companies_house_id=ref.get("companies_house_id"),
        name_normalized=nn,
    )
    if found:
        return found
    eid = str(uuid.uuid4())
    session.run(
        # search_text is what the FULL_TEXT index serves /search from — without
        # it a federated company existed but could never be FOUND, which is how
        # every peer-imported node shipped invisible. is_nominee and
        # name_credibility feed the nominee flagging and merge-survivor
        # selection that silently misjudged federated nodes without them.
        "CREATE (e:Entity {id:$id, name:$name, name_normalized:$nn, "
        "search_text:$stext, type:$type, "
        "country:$country, wikidata_id:$wd, sec_cik:$cik, lei_id:$lei, "
        "companies_house_id:$ch, source_id:$sid, verified:false, "
        "is_nominee:false, name_credibility:$namecred})",
        id=eid, name=name, nn=nn, stext=name, type=ref.get("type") or "company",
        country=ref.get("country"), wd=ref.get("wikidata_id"), cik=ref.get("sec_cik"),
        lei=ref.get("lei_id"), ch=ref.get("companies_house_id"), sid=source_id,
        namecred=credibility)
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
        "search_text:$stext, "
        "wikidata_id:$wd, sec_cik:$cik, birth_date:$bd, birth_place:$bp, "
        "nationality:$nat, source_id:$sid, alias:[], verified:false})",
        id=pid, full=full, stext=full, first=ref.get("first_name"), last=ref.get("last_name"),
        wd=ref.get("wikidata_id"), cik=ref.get("sec_cik"), bd=ref.get("birth_date"),
        bp=ref.get("birth_place"), nat=ref.get("nationality"), sid=source_id)
    return pid


def import_snapshot(data: dict, source_name: str, credibility: int,
                    verified: bool = False, key_id: str = "") -> dict:
    """Upsert a peer snapshot's nodes/edges, attributed to the peer's Source."""
    if data.get("format") != EXPORT_FORMAT:
        raise ValueError(f"Unrecognized export format: {data.get('format')!r}")
    counts = {"entities": 0, "persons": 0, "ownerships": 0, "skipped": 0}
    with db.get_session() as session:
        source_id = _ensure_peer_source(session, source_name, credibility, verified, key_id)
        for e in data.get("entities", []):
            if _upsert_entity(session, e, source_id, credibility):
                counts["entities"] += 1
        for p in data.get("persons", []):
            if _upsert_person(session, p, source_id):
                counts["persons"] += 1
        for o in data.get("ownerships", []):
            owner, owned = o.get("owner") or {}, o.get("owned") or {}
            oid = (_upsert_person(session, owner, source_id) if owner.get("kind") == "person"
                   else _upsert_entity(session, owner, source_id, credibility))
            tid = _upsert_entity(session, owned, source_id, credibility)
            if not oid or not tid:
                counts["skipped"] += 1
                continue
            if oid == tid:
                # A company cannot own itself. A peer asserting one has resolved two
                # names to one node; taking it would plant a self-loop here too.
                log.warning("federation: skipping a self-owning edge on %s", oid)
                counts["skipped"] += 1
                continue
            # `COALESCE(r.ownership_type, …)` looked right and was not: 'unknown' is
            # a VALUE, so it survived, while the null stake beside it was filled in
            # from the peer. The edge then held a percentage typed 'unknown' — the
            # grey "Owned" badge on Alphabet's Larry Page.
            stake = o.get("stake_percent")
            # credibility_score and last_scraped_at were missing here, which
            # broke mark_stale_ownership twice over: COALESCE(cred, 0) read a
            # federated edge as community-tier, and a null last_scraped_at
            # meant it could never be judged stale either.
            session.run(
                "MATCH (a {id:$oid}), (b {id:$tid}) MERGE (a)-[r:OWNS]->(b) "
                "SET r.stake_percent = COALESCE(r.stake_percent, $stake), "
                "    r.ownership_type = $otype, "
                "    r.source_id = $sid, "
                "    r.credibility_score = $cred, "
                "    r.last_scraped_at = $now, "
                "    r.source_url = COALESCE($surl, r.source_url), "
                "    r.source_date = COALESCE($sdate, r.source_date)",
                oid=oid, tid=tid, stake=stake,
                otype=coherent_ownership_type(stake, o.get("ownership_type")),
                sid=source_id, cred=credibility,
                now=datetime.now(timezone.utc).isoformat(),
                surl=o.get("source_url"), sdate=o.get("source_date"))
            # The peer's assertion, recorded like any other source's — without
            # it a peer-agreed pair never counted toward corroboration.
            record_claim(kind=KIND_OWNS, from_id=oid, to_id=tid,
                         source_id=source_id, stake_percent=stake,
                         ownership_type=coherent_ownership_type(stake, o.get("ownership_type")),
                         source_url=o.get("source_url"),
                         source_date=o.get("source_date"),
                         credibility_score=credibility)
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
            "p.credibility_score AS cred, p.auth_token AS tok, p.public_key AS pk, "
            "p.enabled AS enabled", id=peer_id).single()
    if not rec:
        raise HTTPException(status_code=404, detail="Peer not found")
    if rec.get("enabled") is False:
        raise HTTPException(status_code=400, detail="Peer is disabled")

    url = f"{rec['url'].rstrip('/')}/federation/export"
    # Re-validate at pull time: guards against URLs stored before this check
    # was introduced and against any future path that bypasses add_peer.
    _validate_peer_url(rec["url"])
    headers = {"Authorization": f"Bearer {rec['tok']}"} if rec.get("tok") else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=120, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the caller
        log.warning("Peer pull failed (%s): %s", rec["name"], exc)
        raise HTTPException(status_code=502, detail=f"Could not pull from peer: {exc}")

    # Verify the signature when we hold the peer's public key. A registered key
    # that doesn't validate means the data isn't provably the peer's — refuse it.
    peer_key = (rec.get("pk") or "").strip()
    verified = False
    if peer_key:
        if not federation_keys.verify(data, peer_key):
            raise HTTPException(status_code=422,
                detail="Signature verification failed — export is not provably from this peer.")
        verified = True

    try:
        counts = import_snapshot(data, source_name=f"Peer: {rec['name']}",
                                 credibility=rec.get("cred") or 60,
                                 verified=verified, key_id=data.get("key_id") or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    dedup = deduplicate_high_confidence(apply=True)
    return {"peer": rec["name"], "verified": verified, "imported": counts,
            "deduplication": {"merged_count": dedup["merged_count"],
                              "review_count": dedup["review_count"]}}
