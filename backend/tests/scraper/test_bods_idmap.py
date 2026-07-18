"""Unit tests for the BODS import's disk-backed id map + fast JSON loader."""
import pytest


def test_diskmap_behaves_like_dict():
    from app.scraper.bods import _DiskMap
    m = _DiskMap()
    try:
        assert "x" not in m
        assert m.get("x") is None
        m["x"] = "1"
        m["y"] = "2"
        assert m["x"] == "1"
        assert m.get("y") == "2"
        assert "x" in m and "z" not in m
        m["x"] = "9"                       # overwrite
        assert m["x"] == "9"
        assert len(m) == 2
        with pytest.raises(KeyError):
            _ = m["nope"]
    finally:
        m.close()


def test_diskmap_survives_commit_threshold():
    # >20k inserts crosses the periodic commit; reads must still see everything.
    from app.scraper.bods import _DiskMap
    m = _DiskMap()
    try:
        for i in range(45_000):
            m[str(i)] = str(i * 2)
        assert m["44999"] == str(44999 * 2)
        assert m.get("0") == "0"
        assert len(m) == 45_000
    finally:
        m.close()


def test_loads_parses_bytes_and_str():
    from app.scraper.bods import _loads
    assert _loads(b'{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}
    assert _loads('{"n": null}') == {"n": None}
