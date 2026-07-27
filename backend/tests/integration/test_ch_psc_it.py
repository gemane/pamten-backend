"""
Real-ArcadeDB test for the Companies House PSC snapshot importer: builds a tiny
synthetic snapshot (newline-delimited JSON), imports it, and asserts the person/
entity PSCs get OWNS edges to the (number-keyed) company with voting/economic
stakes kept separate.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration


def _psc_zip(tmp_path):
    lines = [
        {"company_number": "08810260", "data": {
            "kind": "individual-person-with-significant-control",
            "name": "Mr Michael Charles Saunders",
            "nationality": "British", "date_of_birth": {"year": 1951, "month": 8},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent",
                                   "voting-rights-75-to-100-percent"],
            "notified_on": "2016-04-06",
            "links": {"self": "/company/08810260/persons-with-significant-control/individual/ABC"}}},
        {"company_number": "07434180", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Robert Hitchins Limited",
            "identification": {"registration_number": "00686734", "country_registered": "England & Wales"},
            "natures_of_control": ["right-to-appoint-and-remove-directors"],
            "notified_on": "2016-07-01",
            "links": {"self": "/company/07434180/persons-with-significant-control/corporate-entity/XYZ"}}},
        {"company_number": "09999999", "data": {
            "kind": "super-secure-person-with-significant-control"}},   # skipped
    ]
    zpath = tmp_path / "psc.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("psc-snapshot.txt", "\n".join(json.dumps(x) for x in lines))
    return str(zpath)


def test_imports_person_and_corporate_pscs(it_db, tmp_path):
    from app.scraper.companies_house_psc import import_ch_psc

    result = import_ch_psc(_psc_zip(tmp_path), "ukpsc", 97)
    assert result["records"] == 3
    assert result["persons"] == 1 and result["entities"] == 1 and result["skipped"] == 1

    person = it_db.run_command(
        "MATCH (p:Person {full_name:'Mr Michael Charles Saunders'})"
        "-[o:OWNS]->(c:Entity {companies_house_id:'08810260'}) "
        "RETURN o.stake_percent AS s, o.voting_power_pct AS v, o.ownership_type AS t")
    assert person and person[0]["s"] == 75 and person[0]["v"] == 75 and person[0]["t"] == "controlling"

    # corporate PSC keyed on its own UK company number, controlling appointment
    corp = it_db.run_command(
        "MATCH (e:Entity {id:'gb-coh:00686734'})-[o:OWNS]->(c:Entity {companies_house_id:'07434180'}) "
        "RETURN e.name AS name, o.ownership_type AS t")
    assert corp and corp[0]["name"] == "Robert Hitchins Limited" and corp[0]["t"] == "controlling"
