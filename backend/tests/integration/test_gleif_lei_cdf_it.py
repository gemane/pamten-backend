"""
Real-ArcadeDB test for the GLEIF LEI-CDF entity importer: builds a tiny synthetic
golden-copy zip, runs the import, and asserts entities land with name/country/
legal-form type — and that it NAMES an existing LEI-only node (the RR-CDF
placeholder case that started this).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration


def _w(v):
    return {"$": v}


def _lei_cdf_zip(tmp_path):
    records = [
        {"LEI": _w("PLAINLEI0000000000001"),
         "Entity": {"LegalName": _w("Acme AG"), "LegalJurisdiction": _w("DE"),
                    "LegalAddress": {"City": _w("Berlin"), "Country": _w("DE")}}},
        {"LEI": _w("FUNDLEI00000000000002"),
         "Entity": {"LegalName": _w("Global Growth Fund"), "LegalJurisdiction": _w("LU"),
                    "LegalForm": {"OtherLegalForm": _w("FUND")}}},
        # same LEI as a pre-existing nameless RR placeholder — must gain a name.
        {"LEI": _w("RRCHILDLEI00000000003"),
         "Entity": {"LegalName": _w("Nestlé Subsidiary SA"), "LegalJurisdiction": _w("CH")}},
    ]
    zpath = tmp_path / "lei2.json.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lei2-golden-copy.json", json.dumps({"records": records}))
    return str(zpath)


def test_imports_entities_and_names_existing_placeholder(it_db, tmp_path):
    from app.scraper.gleif_lei_cdf import import_lei_cdf_entities

    # a nameless LEI-only node, as the RR importer leaves behind
    it_db.run_command("CREATE (:Entity {id:'lei:RRCHILDLEI00000000003', "
                      "lei_id:'RRCHILDLEI00000000003', type:'holding', verified:true})")

    result = import_lei_cdf_entities(_lei_cdf_zip(tmp_path), "gleif", 92)
    assert result["records"] == 3 and result["entities"] == 3

    acme = it_db.run_sql("SELECT name, country FROM Entity WHERE id='lei:PLAINLEI0000000000001'")[0]
    assert acme["name"] == "Acme AG" and acme["country"] == "DE"

    fund = it_db.run_sql("SELECT type FROM Entity WHERE id='lei:FUNDLEI00000000000002'")[0]
    assert fund["type"] == "fund"          # refined from the legal form

    # the pre-existing placeholder gained a name; its other fields are preserved
    child = it_db.run_sql("SELECT name, type, verified FROM Entity WHERE id='lei:RRCHILDLEI00000000003'")[0]
    assert child["name"] == "Nestlé Subsidiary SA"
    assert child["type"] == "holding"      # not clobbered (no legal-form refinement)
    assert child["verified"] is True       # preserved
