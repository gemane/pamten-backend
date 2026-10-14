"""Portraits and logos from Wikimedia Commons, served with their credit.

Until now the browser fetched a person's photo from Wikipedia and a company's
logo from Wikimedia directly, so the reader's IP address, user agent and the
name they were looking at went to a third party in the United States on
every panel open — a disclosure the privacy page would have had to list —
and the image came without the attribution its licence asks for (almost
every Commons photo is CC BY or CC BY-SA; "reasonable to the medium" means
a visible credit, not a hover).

This module moves the fetch behind the API. The server resolves the Commons
file behind a node (the person's Wikipedia page, the company's stored logo
URL), reads the file's licence metadata from the Commons API once, keeps the
thumbnail bytes and the credit in the shared bucket (`images/` prefix) and in
memory, and serves both from here. Wikimedia sees the server's address and
the file name, nothing about the reader.

What is served is decided by the licence. Only files Commons marks as public
domain or under a CC licence that permits commercial reuse and derivatives
(CC0, CC BY, CC BY-SA) are served; NC and ND variants, GFDL-only files and
anything hosted on a Wikipedia project rather than Commons (fair-use, not
free) are refused and the node simply has no image. A definite "no image" —
no page, no file, licence refused — is remembered for a week so a panel open
does not re-ask; a transient failure is not remembered at all.

Pure parts (file name from a URL, licence policy, the credit from the
metadata) are plain functions; the two network calls go through `_get` so
tests replace one seam.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from urllib.parse import quote, unquote

import httpx

from app import objectstore
from app.scraper.wikidata import HEADERS, REQUEST_DELAY

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
SOURCE_NAME = "Wikimedia Commons"

THUMB_WIDTH = 256          # 2× the 72 px avatar, enough for a 36 px logo on any screen
POSITIVE_TTL = 30 * 86400  # a replaced photo shows up within a month
NEGATIVE_TTL = 7 * 86400   # "no image" is re-asked weekly
_TIMEOUT = 10
_MEMORY_SLOTS = 500
S3_PREFIX = "images/"

KINDS = ("entity", "person")

# Licences whose terms a visible credit satisfies for any reuse. Commons'
# LicenseShortName is the key: "CC BY-SA 4.0", "CC BY 3.0 de", "CC0",
# "Public domain", "PD-US" and friends. NC / ND variants are excluded by the
# pattern not matching them; GFDL-only files likewise.
_LICENSE_OK = re.compile(r"^(CC0(\s.*)?|CC\s?BY(\s?-?\s?SA)?(\s[\d.]+.*)?|Public domain.*|PD[\s-].*|PD)$", re.I)

_COMMONS_PATH = re.compile(r"/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/]+)")


@dataclass
class ImageInfo:
    """What the panel needs to show the image and credit it."""
    file: str                      # Commons file name, underscored
    file_page: str                 # the canonical attribution target
    author: str | None             # plain text (Commons gives HTML)
    license: str
    license_url: str | None
    attribution_required: bool
    content_type: str
    source: str = SOURCE_NAME
    etag: str = ""                 # fingerprint of the bytes; in the file URL, so a changed image is a new URL


def fingerprint(data: bytes) -> str:
    """Twelve hex digits of the bytes' SHA-1: the file URL's `?v=` and the
    ETag. A replaced logo on Commons, or a changed answer from this server,
    is then a different URL — a browser that cached the old one for a day
    never shows it under the new credit."""
    return hashlib.sha1(data).hexdigest()[:12]


# ── pure parts ────────────────────────────────────────────────────────────────

def commons_file_from_url(url: str | None) -> str | None:
    """The Commons file name behind an upload.wikimedia.org URL, or None when
    the URL is not a Commons file (a Wikipedia-hosted fair-use image, a
    company's own site, nothing)."""
    if not url:
        return None
    m = _COMMONS_PATH.search(url)
    if not m:
        return None
    name = unquote(m.group(1)).replace(" ", "_")
    return name or None


def license_allowed(short_name: str | None) -> bool:
    """Whether a visible credit is all the licence asks for."""
    return bool(short_name) and bool(_LICENSE_OK.match(short_name.strip()))


_TAGS = re.compile(r"<[^>]+>")


_HIDDEN = re.compile(r"<(\w+)[^>]*\bdisplay:\s*none[^>]*>.*?</\1>", re.I | re.S)
_NO_AUTHOR = re.compile(r"^(unknown( author| artist)?|anonymous|n/?a|-|\?)$", re.I)
_BARE_URL = re.compile(r"^\s*https?://\S+\s*$", re.I)


def _plain(html: str | None) -> str | None:
    """Commons' Artist/Credit fields are HTML. Elements hidden with
    `display: none` go first (the {{unknown}} template carries a hidden
    duplicate of its own text, which read "Unknown authorUnknown author"
    once the tags were stripped), then the tags."""
    text = _TAGS.sub("", _HIDDEN.sub("", html or "")).strip()
    return re.sub(r"\s+", " ", text) or None


def author_from(artist: str | None, credit: str | None) -> str | None:
    """The name the credit line shows, or None when nobody is named: an
    "Unknown author" marker is nobody, and a Credit that is only a URL is a
    source, not a person (the file page link carries it)."""
    name = _plain(artist)
    if name and not _NO_AUTHOR.match(name):
        return name
    fallback = _plain(credit)
    if fallback and not _NO_AUTHOR.match(fallback) and not _BARE_URL.match(fallback):
        return fallback
    return None


def credit_from_imageinfo(file: str, info: dict) -> ImageInfo | None:
    """The credit from one Commons `imageinfo` entry (with `extmetadata`),
    or None when the licence does not allow serving the file."""
    meta = info.get("extmetadata") or {}
    value = lambda k: (meta.get(k) or {}).get("value")  # noqa: E731
    short = value("LicenseShortName")
    if not license_allowed(short):
        return None
    required = str(value("AttributionRequired") or "").lower() == "true"
    return ImageInfo(
        file=file,
        file_page=info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/File:{quote(file)}",
        author=author_from(value("Artist"), value("Credit")),
        license=short.strip(),
        license_url=value("LicenseUrl") or None,
        attribution_required=required,
        content_type=thumb_content_type(info),
    )


def thumb_content_type(info: dict) -> str:
    """The type of the bytes we serve, which are the THUMBNAIL's. Commons
    renders an SVG original to PNG for the thumbnail and then leaves
    `thumbmime` empty — falling back to the original's `mime` would label
    PNG bytes `image/svg+xml`. So the thumbnail URL's extension decides
    before the original's type does."""
    if info.get("thumbmime"):
        return info["thumbmime"]
    thumb = (info.get("thumburl") or "").split("?")[0].lower()
    for ext, mime in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
                      (".gif", "image/gif"), (".webp", "image/webp"), (".svg", "image/svg+xml")):
        if thumb.endswith(ext):
            return mime
    return info.get("mime") or "image/jpeg"


# ── network seam ──────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> httpx.Response:
    """One Wikimedia GET with the project's User-Agent and the polite spacing
    the Wikidata client keeps. Tests replace this."""
    time.sleep(REQUEST_DELAY)
    r = httpx.get(url, params=params, headers=HEADERS, timeout=_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


def commons_imageinfo(file: str) -> dict | None:
    """The file's `imageinfo` entry (thumbnail URL + licence metadata), or
    None when Commons has no such file."""
    r = _get(COMMONS_API, {
        "action": "query", "titles": f"File:{file}", "prop": "imageinfo",
        "iiprop": "extmetadata|url|mime", "iiurlwidth": THUMB_WIDTH,
        "format": "json", "formatversion": 2,
    })
    pages = (r.json().get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing") or not pages[0].get("imageinfo"):
        return None
    return pages[0]["imageinfo"][0]


def wikipedia_image_file(wikipedia_url: str | None) -> str | None:
    """The Commons file behind a Wikipedia article's lead image, or None.

    The page summary names the original image by its upload URL; a Commons
    path means a free file, a project-local path means a fair-use one we
    must not reuse. No guess by name: a namesake's photo on a person's record
    is worse than no photo."""
    if not wikipedia_url:
        return None
    m = re.search(r"/wiki/([^#?]+)", wikipedia_url)
    if not m:
        return None
    try:
        r = _get(WIKIPEDIA_SUMMARY + quote(unquote(m.group(1)), safe=""))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    data = r.json()
    if data.get("type") != "standard":
        return None
    source = (data.get("originalimage") or {}).get("source")
    return commons_file_from_url(source)


# ── cache ─────────────────────────────────────────────────────────────────────

class _Memory:
    """A small in-process layer in front of the bucket: the same panel is
    opened again and again, and a page open must not cost two Wikimedia calls."""
    def __init__(self, slots: int = _MEMORY_SLOTS):
        self._d: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._slots = slots

    def get(self, key: str):
        hit = self._d.get(key)
        if not hit:
            return None
        expires, value = hit
        if expires < time.time():
            self._d.pop(key, None)
            return None
        self._d.move_to_end(key)
        return value

    def put(self, key: str, value, ttl: float) -> None:
        self._d[key] = (time.time() + ttl, value)
        self._d.move_to_end(key)
        while len(self._d) > self._slots:
            self._d.popitem(last=False)

    def drop(self, key: str) -> None:
        self._d.pop(key, None)

    def clear(self) -> None:
        self._d.clear()


_memory = _Memory()

_NONE = {"none": True}


def _meta_key(kind: str, node_id: str) -> str:
    return f"{S3_PREFIX}{kind}/{quote(node_id, safe='')}.json"


def _bytes_key(kind: str, node_id: str) -> str:
    return f"{S3_PREFIX}{kind}/{quote(node_id, safe='')}.bin"


def _fresh(meta: dict) -> bool:
    ttl = NEGATIVE_TTL if meta.get("none") else POSITIVE_TTL
    return (meta.get("fetched_at") or 0) + ttl > time.time()


def _read_meta(kind: str, node_id: str) -> dict | None:
    hit = _memory.get(_meta_key(kind, node_id))
    if hit is not None:
        return hit
    blob = objectstore.get(_meta_key(kind, node_id))
    if not blob:
        return None
    try:
        meta = json.loads(blob)
    except ValueError:
        return None
    if not _fresh(meta):
        return None
    _memory.put(_meta_key(kind, node_id), meta, NEGATIVE_TTL if meta.get("none") else POSITIVE_TTL)
    return meta


def _write_meta(kind: str, node_id: str, meta: dict) -> None:
    meta = {**meta, "fetched_at": time.time()}
    ttl = NEGATIVE_TTL if meta.get("none") else POSITIVE_TTL
    _memory.put(_meta_key(kind, node_id), meta, ttl)
    objectstore.put(_meta_key(kind, node_id), json.dumps(meta).encode(), "application/json")


# ── the lookup ────────────────────────────────────────────────────────────────

def pointer(kind: str, record: dict) -> str | None:
    """What on the record leads to an image: a company's stored logo URL (the
    Wikidata scraper writes a direct Commons thumbnail), a person's Wikipedia
    page. A record without one has no image and nothing to ask."""
    return record.get("logo_url") if kind == "entity" else record.get("wikipedia_url")


def resolve_file(kind: str, record: dict) -> str | None:
    """The Commons file behind the pointer. Network only for the person."""
    if kind == "entity":
        return commons_file_from_url(pointer(kind, record))
    return wikipedia_image_file(pointer(kind, record))


def image_info(kind: str, node_id: str, record: dict) -> ImageInfo | None:
    """The node's image credit, from the cache or from Wikimedia; None when
    the node has no servable image. Transient failures raise (and are not
    remembered); a definite "no image" is remembered for a week."""
    if not pointer(kind, record):
        return None   # nothing to look up, nothing worth remembering
    meta = _read_meta(kind, node_id)
    if meta is not None and not meta.get("none") and not meta.get("etag"):
        meta = None   # written before the fingerprint existed: fetch again, once
    if meta is not None:
        return None if meta.get("none") else ImageInfo(**{k: meta[k] for k in ImageInfo.__dataclass_fields__})
    file = resolve_file(kind, record)
    info = commons_imageinfo(file) if file else None
    credit = credit_from_imageinfo(file, info) if (file and info) else None
    if credit is None:
        _write_meta(kind, node_id, _NONE)
        return None
    thumb = info.get("thumburl") or info.get("url")
    data = _get(thumb).content
    credit.etag = fingerprint(data)
    objectstore.put(_bytes_key(kind, node_id), data, credit.content_type)
    _memory.put(_bytes_key(kind, node_id), data, POSITIVE_TTL)
    _write_meta(kind, node_id, asdict(credit))
    return credit


def image_bytes(kind: str, node_id: str, record: dict) -> tuple[bytes, str] | None:
    """The thumbnail bytes and their content type, or None without an image."""
    credit = image_info(kind, node_id, record)
    if credit is None:
        return None
    data = _memory.get(_bytes_key(kind, node_id)) or objectstore.get(_bytes_key(kind, node_id))
    if data is None:
        # The credit outlived the bytes (a bucket without them, a restart
        # without a bucket): fetch again by forgetting the credit.
        _memory.drop(_meta_key(kind, node_id))
        objectstore.delete(_meta_key(kind, node_id))
        credit = image_info(kind, node_id, record)
        if credit is None:
            return None
        data = _memory.get(_bytes_key(kind, node_id)) or objectstore.get(_bytes_key(kind, node_id))
        if data is None:
            return None
    _memory.put(_bytes_key(kind, node_id), data, POSITIVE_TTL)
    return data, credit.content_type


def forget(kind: str, node_id: str) -> None:
    """Drop a node's cached image and credit (both layers)."""
    for key in (_meta_key(kind, node_id), _bytes_key(kind, node_id)):
        _memory.drop(key)
        objectstore.delete(key)
