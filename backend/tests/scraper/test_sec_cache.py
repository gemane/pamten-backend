"""The EDGAR filing cache: immutable Archives files are served from disk,
everything live (submissions, search, indexes) never is."""
import gzip
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.scraper import sec_cache, sec_edgar

FILING = "https://www.sec.gov/Archives/edgar/data/1181412/000118141222000003/primary_doc.xml"


class TestCacheable:
    def test_an_archives_file_is_cacheable(self):
        assert sec_cache.cacheable(FILING) == ("1181412", "000118141222000003", "primary_doc.xml")

    @pytest.mark.parametrize("url", [
        "https://data.sec.gov/submissions/CIK0001181412.json",          # grows as filings arrive
        "https://efts.sec.gov/LATEST/search-index?q=spacex&forms=13F",  # search
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",   # live listing
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260910.idx",
        "https://www.sec.gov/Archives/edgar/data/1181412/000118141222000003/",  # folder listing
        "https://www.sec.gov/Archives/edgar/data/1181412/0001181412-22-000003-index.htm",
    ])
    def test_live_urls_are_never_cacheable(self, url):
        assert sec_cache.cacheable(url) is None

    def test_query_parameters_make_it_a_search(self):
        assert sec_cache.cacheable(FILING, {"page": 2}) is None


class TestReadWrite:
    def test_round_trip_is_gzipped_and_atomic(self, tmp_path):
        key = sec_cache.cacheable(FILING)
        sec_cache.write(str(tmp_path), key, b"<xml>musk</xml>")
        assert sec_cache.read(str(tmp_path), key) == b"<xml>musk</xml>"
        stored = tmp_path / "1181412" / "000118141222000003" / "primary_doc.xml.gz"
        assert gzip.open(stored).read() == b"<xml>musk</xml>"
        assert not list(tmp_path.rglob("*.tmp"))

    def test_a_torn_file_is_a_miss_not_a_crash(self, tmp_path):
        key = sec_cache.cacheable(FILING)
        p = tmp_path / "1181412" / "000118141222000003" / "primary_doc.xml.gz"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"not gzip")
        assert sec_cache.read(str(tmp_path), key) is None


class TestFetchThrough:
    def _client(self, calls, status=200, body=b"<doc/>"):
        class Client:
            def get(self, url, params=None):
                calls.append(url)
                return httpx.Response(status, content=body, request=httpx.Request("GET", url))
        return Client()

    def test_second_fetch_of_a_filing_never_touches_the_network(self, tmp_path):
        calls: list = []
        with patch.object(settings, "SEC_CACHE_DIR", str(tmp_path)), \
             patch.object(sec_edgar, "_get_client", lambda: self._client(calls)), \
             patch.object(sec_edgar, "REQUEST_DELAY", 0):
            assert sec_edgar._get_text(FILING) == "<doc/>"
            assert sec_edgar._get_text(FILING) == "<doc/>"
        assert calls == [FILING], "one network fetch, then the disk"

    def test_live_urls_are_fetched_every_time(self, tmp_path):
        calls: list = []
        subs = "https://data.sec.gov/submissions/CIK0001181412.json"
        with patch.object(settings, "SEC_CACHE_DIR", str(tmp_path)), \
             patch.object(sec_edgar, "_get_client", lambda: self._client(calls, body=b"{}")), \
             patch.object(sec_edgar, "REQUEST_DELAY", 0):
            sec_edgar._get(subs)
            sec_edgar._get(subs)
        assert calls == [subs, subs]
        assert not list(tmp_path.rglob("*.gz")), "nothing written for a live URL"

    def test_off_when_the_dir_is_unset(self, tmp_path):
        calls: list = []
        with patch.object(settings, "SEC_CACHE_DIR", ""), \
             patch.object(sec_edgar, "_get_client", lambda: self._client(calls)), \
             patch.object(sec_edgar, "REQUEST_DELAY", 0):
            sec_edgar._get_text(FILING)
            sec_edgar._get_text(FILING)
        assert calls == [FILING, FILING]

    def test_an_error_response_is_not_cached(self, tmp_path):
        calls: list = []
        with patch.object(settings, "SEC_CACHE_DIR", str(tmp_path)), \
             patch.object(sec_edgar, "_get_client", lambda: self._client(calls, status=404)), \
             patch.object(sec_edgar, "REQUEST_DELAY", 0), \
             pytest.raises(httpx.HTTPStatusError):
            sec_edgar._get_text(FILING)
        assert not list(tmp_path.rglob("*.gz"))
