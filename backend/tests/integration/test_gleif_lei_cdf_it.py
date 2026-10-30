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
                      "lei_id:'RRCHILDLEI00000000003', type:'holding', description:'kept'})")

    result = import_lei_cdf_entities(_lei_cdf_zip(tmp_path), "gleif", 92)
    assert result["records"] == 3 and result["entities"] == 3

    acme = it_db.run_sql("SELECT name, country FROM Entity WHERE id='lei:PLAINLEI0000000000001'")[0]
    assert acme["name"] == "Acme AG" and acme["country"] == "DE"

    fund = it_db.run_sql("SELECT type FROM Entity WHERE id='lei:FUNDLEI00000000000002'")[0]
    assert fund["type"] == "fund"          # refined from the legal form

    # the pre-existing placeholder gained a name; its other fields are preserved
    child = it_db.run_sql("SELECT name, type, description FROM Entity WHERE id='lei:RRCHILDLEI00000000003'")[0]
    assert child["name"] == "Nestlé Subsidiary SA"
    assert child["type"] == "holding"      # not clobbered (no legal-form refinement)
    assert child["description"] == "kept"  # preserved
    # an ordinary company's record states no type: it is `company`, never empty
    assert it_db.run_sql("SELECT type FROM Entity WHERE id='lei:PLAINLEI0000000000001'")[0]["type"] == "company"


def test_the_full_load_stamps_the_registers_own_status(it_db, tmp_path):
    """`active` and `gleif_registration_status` on the full load, as the delta
    has always done — a rebuilt database used to carry neither."""
    from app.scraper.gleif_lei_cdf import import_lei_cdf_entities

    records = [
        {"LEI": _w("ACTIVELEI000000000011"),
         "Entity": {"LegalName": _w("Here Ltd"), "LegalJurisdiction": _w("GB"),
                    "EntityStatus": _w("ACTIVE")},
         "Registration": {"RegistrationStatus": _w("LAPSED")}},
        {"LEI": _w("GONELEI0000000000012"),
         "Entity": {"LegalName": _w("Gone Ltd"), "LegalJurisdiction": _w("GB"),
                    "EntityStatus": _w("INACTIVE")},
         "Registration": {"RegistrationStatus": _w("RETIRED")}},
    ]
    zpath = tmp_path / "lei2-status.json.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lei2.json", json.dumps({"records": records}))
    result = import_lei_cdf_entities(str(zpath), "gleif", 92)
    assert result["entities"] == 2 and result["inactive"] == 1

    rows = {r["id"]: r for r in it_db.run_command(
        "MATCH (e:Entity) WHERE e.id IN ['lei:ACTIVELEI000000000011', 'lei:GONELEI0000000000012'] "
        "RETURN e.id AS id, e.active AS active, e.gleif_registration_status AS reg")}
    assert rows["lei:ACTIVELEI000000000011"]["active"] is True
    assert rows["lei:ACTIVELEI000000000011"]["reg"] == "LAPSED"
    assert rows["lei:GONELEI0000000000012"]["active"] is False
    assert rows["lei:GONELEI0000000000012"]["reg"] == "RETIRED"


def test_previous_and_trading_names_land_apart_and_a_reimport_replaces_them(it_db, tmp_path):
    """Findings item 16a: GLEIF's previous legal names are dated former names,
    its trading names searchable non-legal names; a re-import of a record that
    no longer states them clears them (the bulk writer overwrites)."""
    from app.scraper.gleif_lei_cdf import import_lei_cdf_entities

    def load(names, events, n):
        rec = {"LEI": _w("529900VBK42Y5HHRMD23"),
               "Entity": {"LegalName": _w("BlackRock, Inc."), "LegalJurisdiction": _w("US-DE"),
                          "OtherEntityNames": {"OtherEntityName": names},
                          "LegalEntityEvents": {"LegalEntityEvent": events}}}
        zpath = tmp_path / f"lei2-names-{n}.json.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("lei2.json", json.dumps({"records": [rec]}))
        import_lei_cdf_entities(str(zpath), "gleif", 92)
        return it_db.run_sql("SELECT other_names, gleif_former_names, gleif_trading_names, search_text "
                             "FROM Entity WHERE id = 'lei:529900VBK42Y5HHRMD23'")[0]

    row = load([{"@type": "PREVIOUS_LEGAL_NAME", "$": "BLACKROCK FUNDING, INC."},
                {"@type": "TRADING_OR_OPERATING_NAME", "$": "BlackRock"}],
               [{"@event_status": "COMPLETED", "LegalEntityEventType": _w("CHANGE_LEGAL_NAME"),
                 "LegalEntityEventEffectiveDate": _w("2024-10-01T00:00:00+02:00")}], 1)
    assert list(row["gleif_former_names"]) == ["BLACKROCK FUNDING, INC.|2024-09-30"]
    assert list(row["gleif_trading_names"]) == ["BlackRock"]
    assert list(row["other_names"] or []) == []
    assert "BLACKROCK FUNDING, INC." in row["search_text"]
    row = load([], [], 2)
    assert list(row["gleif_former_names"] or []) == [] and list(row["gleif_trading_names"] or []) == []
