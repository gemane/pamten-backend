"""The general-register audit: which register a country's GENERAL entities sit
on, from the LEI-CDF golden copy, with the thresholds that keep genuinely
split countries out."""
import json
import zipfile

import pytest

from app.scraper import register_audit
from app.scraper.register_audit import audit_registers, scan_registrations


def _record(country: str, ra: str, number: str, category: str | None = "GENERAL") -> str:
    cat = (f',\n      "EntityCategory": {{\n        "$": "{category}"\n      }}' if category else "")
    return (
        '  {\n   "LEI": {"$": "X"},\n   "Entity": {\n      "LegalName": {"$": "Some Co"},\n'
        f'      "RegistrationAuthority": {{\n        "RegistrationAuthorityID": {{\n          "$": "{ra}"\n        }},\n'
        f'        "RegistrationAuthorityEntityID": {{\n          "$": "{number}"\n        }}\n      }},\n'
        f'      "LegalJurisdiction": {{\n        "$": "{country}"\n      }}{cat}\n   }}\n  }}'
    )


def _golden_copy(tmp_path, records: list[str]):
    body = '{\n "records": [\n' + ",\n".join(records) + "\n ]\n}\n"
    z = tmp_path / "gleif-lei2.json.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("gleif-lei2.json", body)
    return str(z)


def test_a_dominant_register_is_mapped_and_a_fund_does_not_vote(tmp_path):
    # Three Dutch companies on the KVK, one fund on the AFM register: the map
    # says KVK at 100% — the fund sits on a fund register by design.
    path = _golden_copy(tmp_path, [_record("NL", "RA000463", "33129581")] * 3
                        + [_record("NL", "RA000462", "F-1", "FUND")])
    res = audit_registers(path, min_share=0.9, min_records=3)
    assert res["countries"]["NL"]["code"] == "RA000463"
    assert res["countries"]["NL"]["share"] == 1.0 and res["countries"]["NL"]["records"] == 3
    assert res["countries"]["NL"]["shapes"] == ["99999999"]
    assert res["records_scanned"] == 4


def test_a_split_country_and_a_thin_one_are_rejected(tmp_path):
    path = _golden_copy(tmp_path,
                        [_record("DE", "RA000257", "HRB1")] * 2 + [_record("DE", "RA000242", "HRB2")] * 2
                        + [_record("SE", "RA000544", "556000-0001")])
    res = audit_registers(path, min_share=0.9, min_records=2)
    assert "DE" not in res["countries"] and res["rejected"]["DE"]["share"] == 0.5
    assert res["rejected"]["DE"]["runner_up"][1] == 0.5
    assert "SE" not in res["countries"], "one record is not an audit"


def test_a_state_jurisdiction_counts_for_its_country_and_placeholders_are_ignored(tmp_path):
    path = _golden_copy(tmp_path, [_record("US-DE", "RA000602", "3903573")] * 2
                        + [_record("US", "RA888888", "n/a")])
    res = audit_registers(path, min_share=0.9, min_records=2)
    assert res["countries"]["US"]["code"] == "RA000602" and res["countries"]["US"]["records"] == 2


def test_records_spanning_a_chunk_boundary_are_not_lost(tmp_path, monkeypatch):
    # The scanner reads the stream in chunks and carries a tail across; with a
    # chunk smaller than a record every record straddles a boundary.
    monkeypatch.setattr(register_audit, "_CHUNK", 200)
    path = _golden_copy(tmp_path, [_record("SE", "RA000544", f"55600{i}-0001") for i in range(25)])
    assert sum(1 for _ in scan_registrations(path)) == 25


def test_the_shipped_map_reads_back(tmp_path, monkeypatch):
    from app.scraper import gleif_reference
    out = tmp_path / "general_registers.json"
    out.write_text(json.dumps({"countries": {"NL": {"code": "RA000463", "share": 1.0}}}))
    monkeypatch.setattr(gleif_reference, "_load", lambda name: json.loads(out.read_text()))
    gleif_reference._general_registers.cache_clear()
    try:
        assert gleif_reference.general_register_for_country("nl") == "RA000463"
        assert gleif_reference.general_register_for_country("DE") is None
        assert gleif_reference.general_register_for_country(None) is None
    finally:
        gleif_reference._general_registers.cache_clear()


@pytest.mark.parametrize("number,shape", [("CHE-105.909.036", "AAA-999.999.999"),
                                          ("0104-01-056795", "9999-99-999999"),
                                          ("HRB 86515", "AAA 99999")])
def test_shapes(number, shape):
    assert register_audit._shape(number) == shape
