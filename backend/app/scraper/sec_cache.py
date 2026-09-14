"""Fetch-through cache for SEC EDGAR filings — the golden copy for EDGAR.

A filing is immutable once filed: an accession number never changes content,
and an amendment is a NEW accession. That makes
``https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{file}`` a perfect
cache key, and every rebuild, ``--force`` and second pass used to re-download
exactly those bytes. With ``SEC_CACHE_DIR`` set, the first fetch of a filing
writes it to disk (gzipped) and every later one is served from there — no
rate-limit budget spent, no EFTS 500s, no IPv6 stall.

The line that matters: **only Archives files are cached — never search or
listings.** ``data.sec.gov/submissions`` grows as filings arrive, EFTS results
change, and the daily/full indexes are appended to; caching any of those is
how a scraper silently stops seeing new filings. ``cacheable()`` is the single
place that decides, and it is deliberately narrow.

Content, not a mirror: we store what a scrape actually asked for. Off by
default (empty ``SEC_CACHE_DIR``). Render's filesystem is ephemeral, so the
cache is for servers with a disk (this box, production) — see the docs.
"""
from __future__ import annotations

import gzip
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# cik / 18-digit accession folder / a file with an extension. The folder
# listing (URL ending in "/") and "-index.htm" pages are excluded on purpose —
# harmless in practice, but they are not the filing itself.
_ARCHIVE_FILE = re.compile(
    r"^https://www\.sec\.gov/Archives/edgar/data/(\d+)/(\d{18})/([^/?#]+\.[A-Za-z0-9]+)$")

#: hits/misses since process start — surfaced by ``manage.py sec-cache stats``
#: and useful in a scrape's own summary.
stats = {"hits": 0, "misses": 0, "writes": 0}


def cacheable(url: str, params: dict | None = None) -> tuple[str, str, str] | None:
    """(cik, accession, filename) when the URL is an immutable filing file, else None.

    A URL with query params is never cached — parameters are what make a
    request a search rather than a file.
    """
    if params:
        return None
    m = _ARCHIVE_FILE.match(url)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def _path(cache_dir: str, key: tuple[str, str, str]) -> Path:
    cik, accession, filename = key
    return Path(cache_dir) / cik / accession / (filename + ".gz")


def read(cache_dir: str, key: tuple[str, str, str]) -> bytes | None:
    """The cached bytes, or None on a miss (or an unreadable file — refetch)."""
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
    """Store a filing; best-effort — a full disk must not fail the scrape."""
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


def summary(cache_dir: str) -> dict:
    """Files, filers, filings and bytes on disk — for ``manage.py sec-cache stats``."""
    root = Path(cache_dir)
    files = list(root.rglob("*.gz")) if root.is_dir() else []
    return {
        "dir": str(root),
        "files": len(files),
        "filings": len({f.parent for f in files}),
        "filers": len({f.parent.parent for f in files}),
        "bytes": sum(f.stat().st_size for f in files),
        "session": dict(stats),
    }
