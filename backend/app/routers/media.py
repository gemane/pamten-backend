"""Images with their credit: `GET /media/{kind}/{id}` (the credit and where
the bytes are) and `GET /media/{kind}/{id}/file` (the bytes).

Public and unauthenticated, like the node they belong to. The reader's
browser talks only to this API; the module behind it (`app.media`) talks to
Wikimedia on the server's behalf and caches. See that module for the
licence policy — a node without a servable image answers 404 here, and the
panel simply shows none.
"""
from fastapi import APIRouter, HTTPException, Request, Response

from app import media
from app.database import db
from app.merged_ids import resolve_current_id

router = APIRouter(prefix="/media", tags=["media"])

_LABEL = {"entity": "Entity", "person": "Person"}
_FILE_CACHE = "public, max-age=86400"                 # the plain URL: a day
_FILE_CACHE_PINNED = "public, max-age=31536000, immutable"   # the URL with the bytes' own fingerprint


def _record(kind: str, node_id: str) -> dict:
    """The three fields the lookup needs, following a merge forwarding address
    on a miss (as the node routes do)."""
    label = _LABEL.get(kind)
    if not label:
        raise HTTPException(status_code=404, detail="Unknown kind")
    query = (f"MATCH (n:{label} {{id: $id}}) "
             "RETURN n.logo_url AS logo_url, n.image_url AS image_url, "
             "n.wikipedia_url AS wikipedia_url")
    with db.get_session() as session:
        rec = session.run(query, id=node_id).single()
        if not rec:
            merged_into = resolve_current_id(session, node_id)
            if merged_into:
                rec = session.run(query, id=merged_into).single()
    if not rec:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return {"logo_url": rec.get("logo_url"), "image_url": rec.get("image_url"),
            "wikipedia_url": rec.get("wikipedia_url")}


def _lookup(fn, kind: str, node_id: str):
    try:
        found = fn(kind, node_id, _record(kind, node_id))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - Wikimedia down is not our 500
        raise HTTPException(status_code=503, detail=f"Image source unavailable: {exc}") from exc
    if found is None:
        raise HTTPException(status_code=404, detail="No licensed image")
    return found


@router.get("/{kind}/{node_id:path}/file")
def media_file(kind: str, node_id: str, request: Request, v: str | None = None):
    """The thumbnail bytes. The URL the credit hands out carries the bytes'
    fingerprint (`?v=`), and that URL may be cached for a year: when the
    image changes, so does the URL. The plain URL is cacheable for a day.
    Either way the ETag lets a browser revalidate for free."""
    data, content_type = _lookup(media.image_bytes, kind, node_id)
    etag = media.fingerprint(data)
    quoted = f'"{etag}"'
    cache = _FILE_CACHE_PINNED if v == etag else _FILE_CACHE
    if request.headers.get("if-none-match") == quoted:
        return Response(status_code=304, headers={"ETag": quoted, "Cache-Control": cache})
    return Response(content=data, media_type=content_type,
                    headers={"Cache-Control": cache, "ETag": quoted})


@router.get("/{kind}/{node_id:path}")
def media_info(kind: str, node_id: str):
    """The credit the panel shows under the image, and the file's path."""
    credit = _lookup(media.image_info, kind, node_id)
    return {"url": f"/media/{kind}/{node_id}/file?v={credit.etag}", "author": credit.author,
            "license": credit.license, "license_url": credit.license_url,
            "file_page": credit.file_page, "attribution_required": credit.attribution_required,
            "source": credit.source}
