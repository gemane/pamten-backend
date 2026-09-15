"""Against the real bucket — runs only where the backend .env configures the
object store (the dev box, production); CI has no bucket and skips.

Opts out of the no-network guard on purpose: the request IS the point. The
guard covers httpx only, so the conftest also blanks OBJECT_STORE_BUCKET for
every test; this one re-reads the real values from .env for its own duration.
"""
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from app import objectstore
from app.config import settings

_ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")

pytestmark = [
    pytest.mark.allow_network,
    pytest.mark.skipif(
        not (_ENV.get("OBJECT_STORE_BUCKET") and _ENV.get("OBJECT_STORE_ACCESS_KEY"))
        or os.environ.get("OBJECT_STORE_LIVE_TESTS", "1") == "0",
        reason="no object store configured in .env"),
]


def test_the_real_bucket_round_trips_an_object():
    key = f"_tests/{uuid.uuid4().hex}.txt"
    with patch.object(settings, "OBJECT_STORE_BUCKET", _ENV["OBJECT_STORE_BUCKET"]), \
         patch.object(settings, "OBJECT_STORE_ENDPOINT", _ENV["OBJECT_STORE_ENDPOINT"]), \
         patch.object(settings, "OBJECT_STORE_ACCESS_KEY", _ENV["OBJECT_STORE_ACCESS_KEY"]), \
         patch.object(settings, "OBJECT_STORE_SECRET_KEY", _ENV["OBJECT_STORE_SECRET_KEY"]):
        objectstore.reset_client()
        try:
            assert objectstore.put(key, b"owlgraph", content_type="text/plain") is True
            assert objectstore.get(key) == b"owlgraph"
            assert objectstore.summary("_tests/")["objects"] >= 1
        finally:
            objectstore.delete(key)
            assert objectstore.get(key) is None
            objectstore.reset_client()
