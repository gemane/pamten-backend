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
            "name": "Mr Michael Charles Saunders",       # the display string …
            "name_elements": {"forename": "Michael", "middle_name": "Charles",
                              "surname": "Saunders", "title": "Mr"},   # … and the parts
            "nationality": "British", "date_of_birth": {"year": 1951, "month": 8},
            "natures_of_control": ["ownership-of-shares-75-to-100-percent",
                                   "voting-rights-75-to-100-percent"],
            "notified_on": "2016-04-06",
            "links": {"self": "/company/08810260/persons-with-significant-control/individual/ABC"}}},
        # Register of Overseas Entities: an open band, no "N-to-M"
        {"company_number": "OE000123", "data": {
            "kind": "individual-beneficial-owner",
            "name": "Dr Ana Perez", "name_elements": {"forename": "Ana", "surname": "Perez", "title": "Dr"},
            "natures_of_control": ["ownership-of-shares-more-than-25-percent-registered-overseas-entity"],
            "notified_on": "2023-02-01",
            "links": {"self": "/company/OE000123/persons-with-significant-control/individual-beneficial-owner/ROE1"}}},
        {"company_number": "07434180", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Robert Hitchins Limited",
            "identification": {"registration_number": "00686734", "country_registered": "England & Wales"},
            "natures_of_control": ["right-to-appoint-and-remove-directors"],
            "notified_on": "2016-07-01",
            "links": {"self": "/company/07434180/persons-with-significant-control/corporate-entity/XYZ"}}},
        {"company_number": "07434181", "data": {
            "kind": "corporate-entity-person-with-significant-control",
            "name": "Somewhere Holdings",
            "identification": {"registration_number": "77", "country_registered": "Not Specified/Other"},
            "natures_of_control": ["right-to-appoint-and-remove-directors"],
            "notified_on": "2016-07-01",
            "links": {"self": "/company/07434181/persons-with-significant-control/corporate-entity/NS"}}},
        {"company_number": "09999999", "data": {
            "kind": "super-secure-person-with-significant-control"}},   # skipped
    ]
    zpath = tmp_path / "psc.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("persons-with-significant-control-snapshot-2026-10-16.txt",
                    "\n".join(json.dumps(x) for x in lines))
    return str(zpath)


def test_imports_person_and_corporate_pscs(it_db, tmp_path):
    from app.scraper.companies_house_psc import import_ch_psc

    result = import_ch_psc(_psc_zip(tmp_path), "ukpsc", 97)
    assert result["records"] == 5
    assert result["persons"] == 2 and result["entities"] == 2 and result["skipped"] == 1

    # the person is named from the parts — no "Mr" anywhere
    person = it_db.run_command(
        "MATCH (p:Person {full_name:'Michael Charles Saunders'})"
        "-[o:OWNS]->(c:Entity {companies_house_id:'08810260'}) "
        "RETURN p.first_name AS f, p.last_name AS l, p.search_text AS st, "
        "o.stake_percent AS s, o.voting_power_pct AS v, o.ownership_type AS t, "
        "o.since AS since, o.source_date AS sd")
    assert person and person[0]["s"] == 75 and person[0]["v"] == 75 and person[0]["t"] == "controlling"
    # dated by the snapshot (when the register said it), starting at the notification
    assert (person[0]["since"], person[0]["sd"]) == ("2016-04-06", "2026-10-16")
    assert person[0]["f"] == "Michael" and person[0]["l"] == "Saunders"
    assert "Mr" not in person[0]["st"].split()
    assert not it_db.run_sql("SELECT FROM Person WHERE full_name LIKE 'Mr %'")

    # the overseas-entity register's open band floors at 25, written to the edge
    roe = it_db.run_command(
        "MATCH (p:Person {full_name:'Ana Perez'})-[o:OWNS]->(c:Entity {companies_house_id:'OE000123'}) "
        "RETURN o.stake_percent AS s, o.ownership_type AS t")
    assert roe and roe[0]["s"] == 25 and roe[0]["t"] == "controlling"

    # corporate PSC keyed on its own UK company number, controlling appointment
    corp = it_db.run_command(
        "MATCH (e:Entity {id:'gb-coh:00686734'})-[o:OWNS]->(c:Entity {companies_house_id:'07434180'}) "
        "RETURN e.name AS name, o.ownership_type AS t, e.country AS co, e.country_registered AS cr")
    assert corp and corp[0]["name"] == "Robert Hitchins Limited" and corp[0]["t"] == "controlling"
    assert (corp[0]["co"], corp[0]["cr"]) == ("GB", None)       # a code, and no words beside it
    # a filer's country that resolves to no code stays as words, never in `country`
    words = it_db.run_command(
        "MATCH (e:Entity)-[:OWNS]->(c:Entity {companies_house_id:'07434181'}) "
        "RETURN e.country AS co, e.country_registered AS cr")
    assert words and (words[0]["co"], words[0]["cr"]) == (None, "Not Specified/Other")


def test_a_japanese_corporate_psc_merges_with_its_lei_node(it_db, tmp_path):
    """SoftBank Group's real shape: Japan names four registers and the place
    field says "Tokyo Stock Exchange", so only the dashed registration number
    identifies the register — and it is the key GLEIF already put on the
    company's LEI node. Written with the same register_id, the PSC node is a
    hard-id twin and the cross-source dedup folds the pair into ONE node — the
    register's name wins (PSC 97 over GLEIF's 92, the existing survivor rule)
    — carrying both keys, both names and the UK subsidiary."""
    from app.scraper.companies_house_psc import import_ch_psc
    from app.scraper.maintenance import deduplicate_entities

    it_db.run_command(
        "CREATE (:Entity {id: 'lei:5493003BZYYYCDIO0R13', name: 'ソフトバンクグループ株式会社', "
        "name_normalized: 'ソフトバンクグループ株式会社', search_text: 'ソフトバンクグループ株式会社', "
        "type: 'company', country: 'JP', register_id: 'RA000412:0104-01-056795', "
        "lei_id: '5493003BZYYYCDIO0R13', name_credibility: 92})")   # what a GLEIF-written node carries
    lines = [{"company_number": "03115186", "data": {
        "kind": "corporate-entity-person-with-significant-control",
        "name": "Softbank Group Corp",
        "identification": {"registration_number": "0104-01-056795",
                           "country_registered": "Japan",
                           "place_registered": "Tokyo Stock Exchange (First Section)",
                           "legal_authority": "Companies Act"},
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
        "notified_on": "2016-07-01",
        "links": {"self": "/company/03115186/persons-with-significant-control/corporate-entity/SB"}}}]
    zpath = tmp_path / "psc-jp.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("persons-with-significant-control-snapshot-2026-10-16.txt",
                    "\n".join(json.dumps(x) for x in lines))

    result = import_ch_psc(str(zpath), "ukpsc", 97)
    assert result["entities"] == 1
    written = it_db.run_command(
        "MATCH (e:Entity) WHERE e.register_id = 'RA000412:0104-01-056795' "
        "RETURN e.id AS id ORDER BY e.id")
    assert len(written) == 2, "the PSC node carries the LEI node's register_id"

    deduplicate_entities()
    left = it_db.run_command(
        "MATCH (e:Entity) WHERE e.register_id = 'RA000412:0104-01-056795' "
        "RETURN e.id AS id, e.lei_id AS lei, e.aliases AS aliases")
    assert len(left) == 1, "one company"
    assert left[0]["lei"] == "5493003BZYYYCDIO0R13", "the LEI key survives the merge"
    assert "ソフトバンクグループ株式会社" in (left[0]["aliases"] or []), "the losing name becomes an alias"
    owns = it_db.run_command(
        "MATCH (e:Entity {register_id:'RA000412:0104-01-056795'})-[o:OWNS]->(c:Entity {companies_house_id:'03115186'}) "
        "RETURN o.stake_percent AS s")
    assert owns and owns[0]["s"] == 75, "the UK subsidiary hangs off the merged node"


def test_an_unpadded_uk_controller_merges_with_its_lei_node(it_db, tmp_path):
    """Unilever PLC: the PSC filer wrote 41424, GLEIF carries 00041424. Padded
    on import, the two share companies_house_id and the dedup folds them."""
    from app.scraper.companies_house_psc import import_ch_psc
    from app.scraper.maintenance import deduplicate_entities

    it_db.run_command(
        "CREATE (:Entity {id: 'lei:549300MKFYEKVRWML317', name: 'UNILEVER PLC', "
        "name_normalized: 'unilever', search_text: 'UNILEVER PLC', type: 'company', "
        "country: 'GB', companies_house_id: '00041424', lei_id: '549300MKFYEKVRWML317', "
        "name_credibility: 92})")
    lines = [{"company_number": "00017049", "data": {
        "kind": "corporate-entity-person-with-significant-control",
        "name": "Unilever Plc",
        "identification": {"registration_number": "41424", "country_registered": "England"},
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
        "notified_on": "2016-07-01",
        "links": {"self": "/company/00017049/persons-with-significant-control/corporate-entity/UL"}}}]
    zpath = tmp_path / "psc-uk.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("persons-with-significant-control-snapshot-2026-10-16.txt",
                    "\n".join(json.dumps(x) for x in lines))

    import_ch_psc(str(zpath), "ukpsc", 97)
    assert it_db.run_command("MATCH (e:Entity {id:'gb-coh:00041424'}) RETURN e.country AS c")[0]["c"] == "GB"
    deduplicate_entities()
    left = it_db.run_command(
        "MATCH (e:Entity) WHERE e.companies_house_id = '00041424' RETURN e.id AS id, e.lei_id AS lei")
    assert len(left) == 1 and left[0]["lei"] == "549300MKFYEKVRWML317"
    owns = it_db.run_command(
        "MATCH (e:Entity {companies_house_id:'00041424'})-[o:OWNS]->(c:Entity {companies_house_id:'00017049'}) "
        "RETURN o.stake_percent AS s")
    assert owns and owns[0]["s"] == 75
