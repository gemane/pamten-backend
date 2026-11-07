"""Calls to each outside source and to the bucket (app/source_calls.py).

Counted in the httpx transport (one place for every call site — a bare
``httpx.get``, a pooled client, a stream) and in the object store's functions;
kept in memory, written as ``SourceCall`` rows per (UTC day, source, kind)."""
import pathlib
import re
from unittest.mock import patch

import httpcore
import httpx
import pytest

from app import objectstore, source_calls
from app.config import settings
from tests.test_objectstore import FakeS3


@pytest.fixture(autouse=True)
def _clean():
    source_calls.drain()
    yield
    source_calls.drain()


def _counts():
    return {(s, k): n for (_d, s, k), n in source_calls.drain().items()}


class TestHosts:
    @pytest.mark.parametrize("host, source", [
        ("www.sec.gov", "sec-edgar"), ("data.sec.gov", "sec-edgar"), ("efts.sec.gov", "sec-edgar"),
        ("api.gleif.org", "gleif"), ("goldencopy.gleif.org", "gleif"), ("mapping.gleif.org", "gleif"),
        ("www.wikidata.org", "wikidata"), ("query.wikidata.org", "wikidata"),
        ("commons.wikimedia.org", "wikimedia"), ("upload.wikimedia.org", "wikimedia"),
        ("en.wikipedia.org", "wikimedia"),
        ("nominatim.openstreetmap.org", "nominatim"),
        ("find-and-update.company-information.service.gov.uk", "companies-house"),
        ("api.opencorporates.com", "opencorporates"),
        ("WWW.SEC.GOV.", "sec-edgar"),
        # not a source: the database, mail, peers, look-alikes
        ("dev-db.pamten.org", None), ("localhost", None), ("api.resend.com", None),
        ("api.scaleway.com", None), ("notsec.gov", None), ("sec.gov.example.test", None),
        ("", None), (None, None),
    ])
    def test_a_host_belongs_to_its_source(self, host, source):
        assert source_calls.source_for_host(host) == source

    @pytest.mark.parametrize("status, kind", [
        (200, "ok"), (304, "ok"), (404, "4xx"), (429, "429"), (500, "5xx"), (503, "5xx")])
    def test_status_kind(self, status, kind):
        assert source_calls.status_kind(status) == kind

    # hosts the scrapers name that are not fetched from: XML namespaces,
    # licence pages, links shown to users, placeholders
    NOT_FETCHED = {"www.w3.org", "purl.org", "creativecommons.org", "opendatacommons.org",
                   "github.com", "apple.com", "www.gov.uk", "operations.osmfoundation.org",
                   "localhost", "www", "efts", "peer.example.com", "owlgraph.example",
                   "pamten-frontend.onrender.com"}

    def test_every_host_a_scraper_names_is_counted(self):
        # the sibling-path guard: a new source's host must go into SOURCE_HOSTS
        app = pathlib.Path(source_calls.__file__).parent
        files = [*app.glob("scraper/*.py"), app / "media.py"]
        hosts = {h for f in files for h in re.findall(r"https?://([a-zA-Z0-9.-]+)", f.read_text())}
        missing = sorted(h for h in hosts - self.NOT_FETCHED if source_calls.source_for_host(h) is None)
        assert missing == []


class _Pool:
    """Stands in for httpcore's connection pool under a real HTTPTransport."""
    def __init__(self, status=200, fail=False):
        self.status, self.fail = status, fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def close(self):
        pass

    def handle_request(self, request):
        if self.fail:
            raise httpcore.ConnectError("down")
        return httpcore.Response(self.status, content=b"{}")


def _client(**kw):
    transport = httpx.HTTPTransport()
    transport._pool = _Pool(**kw)
    return httpx.Client(transport=transport)


@pytest.fixture
def installed():
    saved = (httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request)
    source_calls.install()
    yield
    httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request = saved


# the conftest guard sits in Client.send, before the transport; the fake pool
# under the real HTTPTransport is what keeps these off the network
@pytest.mark.allow_network
@pytest.mark.allow_db
class TestTransport:
    def test_a_call_to_a_source_is_counted_by_its_status(self, installed):
        with _client() as c:
            c.get("https://www.sec.gov/cgi-bin/browse-edgar")
            c.get("https://data.sec.gov/submissions/CIK0000320193.json")
        with _client(status=429) as c:
            c.get("https://www.sec.gov/x")
        assert _counts() == {("sec-edgar", "ok"): 2, ("sec-edgar", "429"): 1}

    def test_a_failed_call_is_counted_and_still_raises(self, installed):
        with _client(fail=True) as c, pytest.raises(httpx.ConnectError):
            c.get("https://api.gleif.org/api/v1/lei-records/X")
        assert _counts() == {("gleif", "error"): 1}

    def test_the_database_is_not_counted(self, installed):
        with _client() as c:
            c.post("http://localhost:2480/api/v1/command/test", json={})
        assert _counts() == {}

    def test_install_is_idempotent(self, installed):
        source_calls.install()
        with _client() as c:
            c.get("https://www.wikidata.org/w/api.php")
        assert _counts() == {("wikidata", "ok"): 1}

    def test_nothing_is_counted_before_install(self):
        with _client() as c:
            c.get("https://www.wikidata.org/w/api.php")
        assert _counts() == {}

    def test_start_is_off_when_disabled(self):
        # the suite runs with SOURCE_CALLS_ENABLED=false (conftest)
        assert settings.SOURCE_CALLS_ENABLED is False
        with patch.object(source_calls, "install") as inst:
            source_calls.start()
        inst.assert_not_called()


@pytest.fixture
def store():
    s3 = FakeS3()
    with patch.object(settings, "OBJECT_STORE_BUCKET", "b"), \
         patch.object(settings, "OBJECT_STORE_ENDPOINT", "fsn1.example.test"), \
         patch.object(objectstore, "_get_client", lambda: s3):
        yield s3


class TestBucket:
    def test_a_hit_a_miss_and_a_write_per_prefix(self, store):
        objectstore.put("edgar/0001/a.gz", b"1")
        assert objectstore.get("edgar/0001/a.gz") == b"1"
        assert objectstore.get("edgar/0001/b.gz") is None
        assert objectstore.get("geocode/x") is None
        objectstore.delete("images/meta/x")
        objectstore.summary("edgar/")
        assert _counts() == {("bucket:edgar", "put"): 1, ("bucket:edgar", "hit"): 1,
                             ("bucket:edgar", "miss"): 1, ("bucket:geocode", "miss"): 1,
                             ("bucket:images", "delete"): 1, ("bucket:edgar", "list"): 1}

    def test_an_outage_is_an_error_not_a_miss(self, store):
        store.fail = True
        assert objectstore.get("edgar/a") is None
        assert objectstore.put("edgar/a", b"1") is False
        assert _counts() == {("bucket:edgar", "error"): 2}

    def test_an_off_store_counts_nothing(self):
        with patch.object(settings, "OBJECT_STORE_BUCKET", ""):
            objectstore.get("edgar/a")
        assert _counts() == {}


class TestFlush:
    def test_one_upsert_per_day_source_and_kind(self):
        source_calls.count("sec-edgar", "ok", 3)
        source_calls.count("bucket:edgar", "hit")
        with patch("app.db.arcadedb.run_sql") as sql:
            assert source_calls.flush() == 2
        params = sorted((c.args[1]["s"], c.args[1]["kind"], c.args[1]["n"]) for c in sql.call_args_list)
        assert params == [("bucket:edgar", "hit", 1), ("sec-edgar", "ok", 3)]
        first = sql.call_args_list[0].args
        assert "UPSERT WHERE key = :k" in first[0] and "COALESCE(count, 0) + :n" in first[0]
        assert first[1]["k"] == f"{first[1]['d']}|{first[1]['s']}|{first[1]['kind']}"
        assert source_calls.drain() == {}

    def test_a_failed_write_keeps_the_count_for_the_next_flush(self):
        source_calls.count("gleif", "ok", 2)
        with patch("app.db.arcadedb.run_sql", side_effect=RuntimeError("db down")):
            assert source_calls.flush() == 0
        assert _counts() == {("gleif", "ok"): 2}


class TestFormat:
    def test_sources_by_volume_then_the_bucket(self):
        text = source_calls.format_totals({
            "bucket:edgar": {"hit": 40, "miss": 5},
            "gleif": {"ok": 3},
            "sec-edgar": {"ok": 900, "429": 2},
        })
        lines = text.splitlines()
        assert lines[0].split()[:2] == ["sec-edgar", "902"]
        assert lines[1].split()[:2] == ["gleif", "3"]
        assert lines[2].startswith("  bucket:edgar") and "hit 40" in lines[2]

    def test_nothing_counted(self):
        assert source_calls.format_totals({}) == "  none counted\n"


class TestWeeklyReport:
    def test_the_week_sums_sources_and_splits_the_bucket(self):
        from app import weekly_report
        rows = [{"source": "sec-edgar", "kind": "ok", "n": 900}, {"source": "sec-edgar", "kind": "5xx", "n": 3},
                {"source": "gleif", "kind": "ok", "n": 12},
                {"source": "bucket:edgar", "kind": "hit", "n": 400}, {"source": "bucket:edgar", "kind": "miss", "n": 50}]
        with patch.object(weekly_report, "_rows", return_value=rows) as q:
            out = weekly_report._source_calls("2026-W45")
        assert q.call_args.args[1] == {"a": "2026-11-02", "b": "2026-11-09"}
        assert out == {"sources": {"sec-edgar": {"total": 903, "ok": 900, "5xx": 3},
                                   "gleif": {"total": 12, "ok": 12}},
                       "bucket": {"edgar": {"hit": 400, "miss": 50}}}
