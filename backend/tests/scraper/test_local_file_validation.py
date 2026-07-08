import pytest
from fastapi import HTTPException

from app.scraper.router import _validate_bods_local_file
from app.config import settings


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "BODS_DATA_DIR", str(d))
    return d


def test_none_passes_through(data_dir):
    assert _validate_bods_local_file(None) is None


def test_valid_json_inside_data_dir(data_dir):
    f = data_dir / "gleif.json"
    f.write_text("{}")
    assert _validate_bods_local_file(str(f)) == str(f)


def test_valid_zip_inside_data_dir(data_dir):
    f = data_dir / "psc.zip"
    f.write_bytes(b"PK")
    assert _validate_bods_local_file(str(f)) == str(f)


def test_rejects_path_outside_data_dir(data_dir, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    with pytest.raises(HTTPException) as exc:
        _validate_bods_local_file(str(outside))
    assert exc.value.status_code == 400


def test_rejects_traversal_escape(data_dir, tmp_path):
    outside = tmp_path / "secret.json"
    outside.write_text("{}")
    sneaky = str(data_dir / ".." / "secret.json")
    with pytest.raises(HTTPException) as exc:
        _validate_bods_local_file(sneaky)
    assert exc.value.status_code == 400


def test_rejects_wrong_extension(data_dir):
    f = data_dir / "passwd"
    f.write_text("root:x:0:0")
    with pytest.raises(HTTPException) as exc:
        _validate_bods_local_file(str(f))
    assert exc.value.status_code == 400
    assert ".zip or .json" in exc.value.detail


def test_rejects_missing_file(data_dir):
    with pytest.raises(HTTPException) as exc:
        _validate_bods_local_file(str(data_dir / "nope.json"))
    assert exc.value.status_code == 400
    assert "not found" in exc.value.detail


def test_rejects_symlink_pointing_outside(data_dir, tmp_path):
    outside = tmp_path / "target.json"
    outside.write_text("{}")
    link = data_dir / "link.json"
    link.symlink_to(outside)
    with pytest.raises(HTTPException) as exc:
        _validate_bods_local_file(str(link))
    assert exc.value.status_code == 400
