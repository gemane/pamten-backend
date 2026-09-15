"""Fetch-through cache for SEC EDGAR filings — the golden copy for EDGAR.

A filing is immutable once filed: an accession number never changes content,
and an amendment is a NEW accession. That makes
``https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{file}`` a perfect
cache key, and every rebuild, ``--force`` and second pass used to re-download
exactly those bytes. Every fetch of a filing writes it to the cache and every
later one is served from there — no rate-limit budget spent, no EFTS 500s,
no IPv6 stall.

Two layers, both optional and independent:

- **local** (``SEC_CACHE_DIR``): gzipped files on this machine's disk — the
  fast layer for a rebuild that reads thousands of filings (~1 ms a file);
- **shared** (the object store, prefix ``edgar/``): the same keys in the
  project's bucket, visible from every environment — Render, which has no
  disk, reads and writes only this layer; a machine with both backfills its
  local layer from a shared hit.

Read order local → shared → sec.gov; a successful fetch writes every layer
that is on. Both layers hold the same ``cik/accession/file.gz`` layout, so
one can be seeded from the other with a plain sync.

The line that matters: **only Archives files are cached — never search or
listings.** ``data.sec.gov/submissions`` grows as filings arrive, EFTS results
change, and the daily/full indexes are appended to; caching any of those is
how a scraper silently stops seeing new filings. ``cacheable()`` is the single
place that decides, and it is deliberately narrow.

Content, not a mirror: we store what a scrape actually asked for.
"""
from __future__ import annotations

import gzip
import io
import logging
import re
from pathlib import Path

from app import objectstore
from app.config import settings

logger = logging.getLogger(__name__)

#: prefix of the filing cache inside the shared bucket
S3_PREFIX = "edgar/"

# cik / 18-digit accession folder / a file with an extension. The folder
# listing (URL ending in "/") and "-index.htm" pages are excluded on purpose —
# harmless in practice, but they are not the filing itself.
_ARCHIVE_FILE = re.compile(
    r"^https://www\.sec\.gov/Archives/edgar/data/(\d+)/(\d{18})/([^/?#]+\.[A-Za-z0-9]+)$")

#: per-layer hits/misses/writes since process start — surfaced by
#: ``manage.py sec-cache stats`` and useful in a scrape's own summary.
stats = {"hits": 0, "misses": 0, "writes": 0, "s3_hits": 0, "s3_writes": 0}


def cacheable(url: str, params: dict | None = None) -> tuple[str, str, str] | None:
    """(cik, accession, filename) when the URL is an immutable filing file, else None.

    A URL with query params is never cached — parameters are what make a
    request a search rather than a file.
    """
    if params:
        return None
    m = _ARCHIVE_FILE.match(url)
    return (m.group(1), m.group(2), m.group(3)) if m else None


# ── the local layer ─────────────────────────────────────────────────────────

def _path(cache_dir: str, key: tuple[str, str, str]) -> Path:
    cik, accession, filename = key
    return Path(cache_dir) / cik / accession / (filename + ".gz")


def read(cache_dir: str, key: tuple[str, str, str]) -> bytes | None:
    """The locally cached bytes, or None on a miss (or an unreadable file — refetch)."""
    p = _path(cache_dir, key)
    try:
        with gzip.open(p, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        stats["misses"] += 1
        return None
    except (OSError, EOFError) as exc:          # a torn write — treat as a miss
        logger.warning("sec cache: unreadable %s (%s); refetching", p, exc)
        stats["misses"] += 1
        return None
    stats["hits"] += 1
    return data


def write(cache_dir: str, key: tuple[str, str, str], data: bytes) -> None:
    """Store a filing locally; best-effort — a full disk must not fail the scrape."""
    p = _path(cache_dir, key)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            fh.write(data)
        tmp.replace(p)                            # atomic: readers never see a torn file
        stats["writes"] += 1
    except OSError as exc:
        logger.warning("sec cache: could not write %s: %s", p, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


# ── the shared layer ────────────────────────────────────────────────────────

def s3_key(key: tuple[str, str, str]) -> str:
    cik, accession, filename = key
    return f"{S3_PREFIX}{cik}/{accession}/{filename}.gz"


def _gunzip(blob: bytes) -> bytes | None:
    try:
        return gzip.decompress(blob)
    except (OSError, EOFError):
        return None


# ── the tiered façade the fetch layer uses ──────────────────────────────────

def lookup(key: tuple[str, str, str]) -> bytes | None:
    """The filing from the fastest layer that has it, backfilling the local
    layer from a shared hit; None when no layer does (or none is on)."""
    local = settings.SEC_CACHE_DIR
    if local:
        data = read(local, key)
        if data is not None:
            return data
    if objectstore.enabled():
        blob = objectstore.get(s3_key(key))
        if blob is not None:
            data = _gunzip(blob)
            if data is None:
                logger.warning("sec cache: undecodable object %s; refetching", s3_key(key))
            else:
                stats["s3_hits"] += 1
                if local:
                    write(local, key, data)
                return data
    return None


def store(key: tuple[str, str, str], data: bytes) -> None:
    """Write a freshly fetched filing to every layer that is on."""
    if settings.SEC_CACHE_DIR:
        write(settings.SEC_CACHE_DIR, key, data)
    if objectstore.enabled():
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as fh:
            fh.write(data)
        if objectstore.put(s3_key(key), buf.getvalue(), content_type="application/gzip"):
            stats["s3_writes"] += 1


def summary(cache_dir: str | None) -> dict:
    """Files, filers, filings and bytes per layer — for ``manage.py sec-cache stats``."""
    out: dict = {"session": dict(stats)}
    if cache_dir:
        root = Path(cache_dir)
        files = list(root.rglob("*.gz")) if root.is_dir() else []
        out["local"] = {
            "dir": str(root),
            "files": len(files),
            "filings": len({f.parent for f in files}),
            "filers": len({f.parent.parent for f in files}),
            "bytes": sum(f.stat().st_size for f in files),
        }
    if objectstore.enabled():
        out["shared"] = objectstore.summary(S3_PREFIX)
    return out
