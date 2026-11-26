"""The shared object store: provider-agnostic S3 client, off when unset,
best-effort on failure — a store outage degrades to a miss, never a crash."""
from unittest.mock import patch

import pytest

from app import objectstore
from app.config import settings


class FakeS3:
    """Just enough of boto3's S3 client: a dict of key → bytes."""
    def __init__(self, fail: bool = False):
        self.objects: dict[str, bytes] = {}
        self.fail = fail
        self.calls: list[str] = []

    def _boom(self):
        if self.fail:
            raise RuntimeError("store down")

    def get_object(self, Bucket, Key):
        self.calls.append(f"get {Key}")
        self._boom()
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        import io
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.calls.append(f"put {Key}")
        self._boom()
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.calls.append(f"del {Key}")
        self._boom()
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        store = self
        class P:
            def paginate(self, Bucket, Prefix):
                store._boom()
                yield {"Contents": [{"Key": k, "Size": len(v)} for k, v in store.objects.items()
                                    if k.startswith(Prefix)]}
        return P()


@pytest.fixture
def fake():
    s3 = FakeS3()
    with patch.object(settings, "OBJECT_STORE_BUCKET", "b"), \
         patch.object(settings, "OBJECT_STORE_ENDPOINT", "fsn1.example.test"), \
         patch.object(objectstore, "_get_client", lambda: s3):
        yield s3


class TestObjectStore:
    def test_off_when_the_bucket_is_unset(self):
        with patch.object(settings, "OBJECT_STORE_BUCKET", ""):
            assert not objectstore.enabled()
            assert objectstore.get("x") is None
            assert objectstore.put("x", b"1") is False

    def test_endpoint_gets_a_scheme_when_given_bare(self):
        with patch.object(settings, "OBJECT_STORE_ENDPOINT", "fsn1.your-objectstorage.com"):
            assert objectstore._endpoint() == "https://fsn1.your-objectstorage.com"
        with patch.object(settings, "OBJECT_STORE_ENDPOINT", "http://localhost:9000"):
            assert objectstore._endpoint() == "http://localhost:9000"

    def test_put_get_delete_round_trip(self, fake):
        assert objectstore.put("edgar/1/2/f.gz", b"bytes") is True
        assert objectstore.get("edgar/1/2/f.gz") == b"bytes"
        assert objectstore.delete("edgar/1/2/f.gz") is True
        assert objectstore.get("edgar/1/2/f.gz") is None, "a missing key is a quiet None"

    def test_summary_counts_only_the_prefix(self, fake):
        objectstore.put("edgar/1/2/a.gz", b"12345")
        objectstore.put("edgar/1/3/b.gz", b"12")
        objectstore.put("golden/x.zip", b"1234567890")
        assert objectstore.summary("edgar/") == {"prefix": "edgar/", "objects": 2, "bytes": 7}

    def test_an_outage_degrades_to_a_miss_not_a_crash(self):
        s3 = FakeS3(fail=True)
        with patch.object(settings, "OBJECT_STORE_BUCKET", "b"), \
             patch.object(settings, "OBJECT_STORE_ENDPOINT", "e"), \
             patch.object(objectstore, "_get_client", lambda: s3):
            assert objectstore.get("k") is None
            assert objectstore.put("k", b"1") is False
            assert "error" in objectstore.summary("edgar/")

    @pytest.mark.parametrize("raised", [
        # the rebuild of 2026-11-26: a slow bucket, the exception's `response` is None
        lambda: __import__("botocore.exceptions", fromlist=["x"]).ReadTimeoutError(endpoint_url="https://e/b/k"),
        lambda: __import__("botocore.exceptions", fromlist=["x"]).EndpointConnectionError(endpoint_url="https://e"),
        lambda: __import__("botocore.exceptions", fromlist=["x"]).ClientError({}, "GetObject"),   # no Error block
        lambda: __import__("botocore.exceptions", fromlist=["x"]).ClientError({"Error": None}, "GetObject"),
    ])
    def test_a_timeout_or_an_odd_answer_is_an_outage_not_a_crash(self, raised):
        class Slow(FakeS3):
            def get_object(self, Bucket, Key):
                raise raised()
        with patch.object(settings, "OBJECT_STORE_BUCKET", "b"), \
             patch.object(settings, "OBJECT_STORE_ENDPOINT", "e"), \
             patch.object(objectstore, "_get_client", lambda: Slow()), \
             patch.object(objectstore.source_calls, "count_bucket") as counted:
            assert objectstore.get("geocode/abc") is None
        counted.assert_called_once_with("geocode/abc", "error")       # an outage, not "not there"

    def test_only_the_stores_no_such_key_is_a_miss(self):
        from botocore.exceptions import ClientError
        assert objectstore._is_missing(ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"))
        assert objectstore._is_missing(ClientError({"Error": {"Code": "404"}}, "HeadObject"))
        assert not objectstore._is_missing(ClientError({"Error": {"Code": "SlowDown"}}, "GetObject"))
        assert not objectstore._is_missing(RuntimeError("store down"))
        # an HTTP error from requests' family carries the HTTP response object itself
        failed = RuntimeError("read timeout")
        failed.response = type("Response", (), {"status_code": 504})()
        assert not objectstore._is_missing(failed)

