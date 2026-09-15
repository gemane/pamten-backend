"""The project's object store — one S3-compatible bucket, shared by every
environment, with a prefix per purpose.

Render's filesystem is ephemeral and the production box is a different
machine, so anything that should outlive a deploy and be visible from both
lives here: today the EDGAR filing cache (``edgar/…``, see
``scraper/sec_cache.py``); golden copies and other durable files can follow
under their own prefixes. Sharing one bucket across dev and prod is safe only
for content that is environment-independent — public documents, source
snapshots — never for the graph itself or anything a user wrote.

Provider-agnostic: any S3 API (Hetzner, Scaleway, AWS…) via ``OBJECT_STORE_*``.
Off when the bucket is unset; every call is best-effort at the read/write
level (a store outage degrades to "fetch from the source", never to a failed
scrape) — callers decide what a miss means.
"""
from __future__ import annotations

import logging
import threading

from app.config import settings

logger = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()


def enabled() -> bool:
    return bool(settings.OBJECT_STORE_BUCKET and settings.OBJECT_STORE_ENDPOINT)


def _endpoint() -> str:
    ep = settings.OBJECT_STORE_ENDPOINT.strip()
    return ep if ep.startswith("http") else "https://" + ep


def _get_client():
    """One boto3 client per process (thread-safe to share); imported lazily so
    the app starts without boto3 when the store is off."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                import botocore.config
                _client = boto3.client(
                    "s3",
                    endpoint_url=_endpoint(),
                    aws_access_key_id=settings.OBJECT_STORE_ACCESS_KEY,
                    aws_secret_access_key=settings.OBJECT_STORE_SECRET_KEY,
                    region_name=settings.OBJECT_STORE_REGION or "us-east-1",
                    config=botocore.config.Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "path"},
                        connect_timeout=5, read_timeout=30,
                        retries={"max_attempts": 2}),
                )
    return _client


def reset_client() -> None:
    """Drop the cached client (settings changed, or a test swapped them)."""
    global _client
    with _client_lock:
        _client = None


def get(key: str) -> bytes | None:
    """The object's bytes, or None when absent or the store is unreachable."""
    if not enabled():
        return None
    try:
        return _get_client().get_object(Bucket=settings.OBJECT_STORE_BUCKET, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 - NoSuchKey and outages both mean "not from here"
        if _is_missing(exc):
            return None
        logger.warning("object store: get %s failed: %s", key, exc)
        return None


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
    """Store an object; False (and a warning) when it could not be written."""
    if not enabled():
        return False
    try:
        _get_client().put_object(Bucket=settings.OBJECT_STORE_BUCKET, Key=key,
                                 Body=data, ContentType=content_type)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("object store: put %s failed: %s", key, exc)
        return False


def delete(key: str) -> bool:
    if not enabled():
        return False
    try:
        _get_client().delete_object(Bucket=settings.OBJECT_STORE_BUCKET, Key=key)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("object store: delete %s failed: %s", key, exc)
        return False


def summary(prefix: str) -> dict:
    """Object count and bytes under a prefix (paginated; a big prefix is fine)."""
    out = {"prefix": prefix, "objects": 0, "bytes": 0}
    if not enabled():
        return out
    try:
        paginator = _get_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.OBJECT_STORE_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                out["objects"] += 1
                out["bytes"] += obj.get("Size", 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("object store: list %s failed: %s", prefix, exc)
        out["error"] = str(exc)
    return out


def _is_missing(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
    return code in ("NoSuchKey", "404", "NotFound")
