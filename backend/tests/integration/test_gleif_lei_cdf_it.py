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


def _event(kind, day, status="COMPLETED"):
    return {"@group_type": "STANDALONE", "@event_status": status,
            "LegalEntityEventType": _w(kind), "LegalEntityEventEffectiveDate": _w(f"{day}T00:00:00+01:00")}


def _ended(lei, name, status, *events, updated="2026-09-29T11:33:22Z"):
    return {"LEI": _w(lei),
            "Entity": {"LegalName": _w(name), "LegalJurisdiction": _w("US"), "EntityStatus": _w(status),
                       "LegalEntityEvents": {"LegalEntityEvent": list(events)}},
            "Registration": {"RegistrationStatus": _w("RETIRED" if status == "INACTIVE" else "ISSUED"),
                             "LastUpdateDate": _w(updated)}}


def _load(tmp_path, name, records):
    from app.scraper.gleif_lei_cdf import import_lei_cdf_entities
    zpath = tmp_path / f"{name}.json.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lei2.json", json.dumps({"records": records}))
    return import_lei_cdf_entities(str(zpath), "gleif", 92)


def test_the_end_gleif_dates_in_its_events_closes_what_the_company_held(it_db, tmp_path):
    """Findings item 85(a): the day in the record's COMPLETED end event lands as
    `dissolved_on`, the chain's heal closes the company's open holdings on it;
    the surviving party of the same merger, ACTIVE with the same event, keeps
    its day and its holdings."""
    from app.dissolution import heal_ends_at_dissolution
    gone, survivor, sub = "529900NBET57WDRXAR22", "529900G1S6D7T5ZIX198", "SUBLEI00000000000099"
    for lei in (gone, survivor, sub):
        it_db.run_command("CREATE (:Entity {id:$id, name:$id, name_normalized:$id})", {"id": f"lei:{lei}"})
    for owner in (gone, survivor):
        it_db.run_command("MATCH (a:Entity {id:$a}), (b:Entity {id:$b}) "
                          "CREATE (a)-[:OWNS {since:'2010-01-01', until:null, source_id:'gleif'}]->(b)",
                          {"a": f"lei:{owner}", "b": f"lei:{sub}"})
    counts = _load(tmp_path, "ends", [
        _ended(gone, "Random House LLC", "INACTIVE", _event("MERGERS_AND_ACQUISITIONS", "2014-10-12")),
        _ended(survivor, "Penguin Random House LLC", "ACTIVE", _event("MERGERS_AND_ACQUISITIONS", "2014-10-12")),
    ])
    assert counts["entities"] == 2 and counts["inactive"] == 1 and counts["ends_held"] == 0
    rows = {r["id"]: r for r in it_db.run_sql(
        "SELECT id, dissolved_on, end_kind, active FROM Entity WHERE id IN :ids", {"ids": [f"lei:{gone}", f"lei:{survivor}"]})}
    assert rows[f"lei:{gone}"]["dissolved_on"] == "2014-10-12" and rows[f"lei:{gone}"]["active"] is False
    assert rows[f"lei:{gone}"]["end_kind"] == "merged"                     # how it ended (item 85c)
    assert rows[f"lei:{survivor}"]["dissolved_on"] is None and rows[f"lei:{survivor}"]["active"] is True
    assert rows[f"lei:{survivor}"]["end_kind"] is None
    healed = heal_ends_at_dissolution(apply=True)
    assert healed["closed"] == 1 and healed["companies"] == 1
    ends = {r["a"]: (r["until"], r["why"]) for r in it_db.run_sql(
        "SELECT @out.id AS a, until, until_reason AS why FROM OWNS WHERE @in.id = :b", {"b": f"lei:{sub}"})}
    assert ends[f"lei:{gone}"] == ("2014-10-12", "merged")               # merged into the survivor, not wound up
    assert ends[f"lei:{survivor}"] == (None, None)


def test_the_end_of_a_record_on_a_filers_node_waits_for_edgar(it_db, tmp_path):
    """Items 84 → 85(a): the legacy ONEOK record was joined to the filer while
    it lived; the successor issuer keeps filing under the CIK, so the full
    load holds the end GLEIF now states (no `dissolved_on`, `active` true) —
    while the record of an entity whose filer stopped filing gets its end."""
    from unittest.mock import patch

    from app.config import settings
    from app.scraper.legal_identity import Filer
    oke, viacom = "2T3D6M0JSY48PSZI1Q41", "X0ST5BPF0CZG9FE5OV61"
    it_db.run_command("CREATE (:Entity {id:$id, name:'ONEOK, INC.', name_normalized:'oneok', "
                      "lei_id:$lei, sec_cik:'0001039684', active:true})", {"id": f"lei:{oke}", "lei": oke})
    it_db.run_command("CREATE (:Entity {id:$id, name:'Viacom Inc.', name_normalized:'viacom', "
                      "lei_id:$lei, sec_cik:'0001339947', active:true})", {"id": f"lei:{viacom}", "lei": viacom})
    filers = {"0001039684": Filer("0001039684", "ONEOK INC /NEW/", last_filed="2026-10-28"),
              "0001339947": Filer("0001339947", "Viacom Inc.", last_filed="2019-12-16")}
    with patch.object(settings, "SCRAPER_ENABLED", True), patch.object(settings, "SCRAPER_SEC_EDGAR_ENABLED", True), \
         patch("app.scraper.legal_identity.filer_facts", side_effect=filers.get):
        counts = _load(tmp_path, "filers", [
            _ended(oke, "ONEOK, INC.", "INACTIVE", _event("DISSOLUTION", "2026-08-13")),
            _ended(viacom, "Viacom Inc.", "INACTIVE", _event("MERGERS_AND_ACQUISITIONS", "2019-12-04"),
                   updated="2023-08-04T00:00:00Z"),
        ])
    assert counts["ends_held"] == 1 and counts["inactive"] == 1
    rows = {r["id"]: r for r in it_db.run_sql(
        "SELECT id, dissolved_on, active, gleif_registration_status AS reg FROM Entity WHERE id IN :ids",
        {"ids": [f"lei:{oke}", f"lei:{viacom}"]})}
    assert rows[f"lei:{oke}"]["dissolved_on"] is None and rows[f"lei:{oke}"]["active"] is True
    assert rows[f"lei:{oke}"]["reg"] == "RETIRED"                  # the register's word on the RECORD stays
    assert rows[f"lei:{viacom}"]["dissolved_on"] == "2019-12-04" and rows[f"lei:{viacom}"]["active"] is False


def test_the_registers_proceedings_land_as_dated_states_and_a_reimport_replaces_them(it_db, tmp_path):
    """Findings item 85(d): a liquidation in progress is a `gleif_events` entry
    and a timeline row ("in liquidation since …"), the company stays alive and
    undated; GLEIF's next word replaces the list."""
    from app.routers.relationships import ownership_history_of
    lei = "2138003BFF7ZPQZA6B02"
    counts = _load(tmp_path, "states", [
        _ended(lei, "DIAMOND TRANSMISSION CORPORATION LIMITED", "ACTIVE",
               {**_event("LIQUIDATION", "2025-04-28", "IN_PROGRESS"), "LegalEntityEventRecordedDate": _w("2025-04-28T09:00:00Z")})])
    assert counts["entities"] == 1 and counts["inactive"] == 0
    [row] = it_db.run_sql("SELECT gleif_events, dissolved_on, active FROM Entity WHERE id = :id", {"id": f"lei:{lei}"})
    assert list(row["gleif_events"]) == ["LIQUIDATION|IN_PROGRESS|2025-04-28|2025-04-28"]
    assert row["dissolved_on"] is None and row["active"] is True
    events, _ = ownership_history_of(f"lei:{lei}")
    assert [e for e in events if e["kind"] == "legal_event"] == [
        {"kind": "legal_event", "event": "liquidation", "status": "in_progress", "since": "2025-04-28",
         "dated_by": "effective", "until": None, "active": True, "party": None}]
    # the register's next word: the liquidation was called off — the list is replaced, not appended to
    _load(tmp_path, "states2", [
        _ended(lei, "DIAMOND TRANSMISSION CORPORATION LIMITED", "ACTIVE",
               _event("LIQUIDATION", "2025-04-28", "WITHDRAWN_CANCELLED"))])
    [row] = it_db.run_sql("SELECT gleif_events FROM Entity WHERE id = :id", {"id": f"lei:{lei}"})
    assert list(row["gleif_events"]) == ["LIQUIDATION|WITHDRAWN_CANCELLED|2025-04-28|"]


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


def test_a_duplicate_registration_writes_no_node(it_db, tmp_path):
    # findings item 91: News Corporation's DUPLICATE LEI names the live record
    from app.scraper.gleif_lei_cdf import import_lei_cdf_entities
    records = [
        {"LEI": _w("549300YBCMRP1ZVOWM12"),
         "Entity": {"LegalName": _w("News Corporation"), "LegalJurisdiction": _w("US-DE"),
                    "SuccessorEntity": [{"SuccessorLEI": _w("549300ITS31QK8VRBQ14")}]},
         "Registration": {"RegistrationStatus": _w("DUPLICATE")}},
        {"LEI": _w("549300ITS31QK8VRBQ14"),
         "Entity": {"LegalName": _w("NEWS CORP"), "LegalJurisdiction": _w("US-DE")},
         "Registration": {"RegistrationStatus": _w("ISSUED")}},
    ]
    zpath = tmp_path / "dup.json.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lei2-golden-copy.json", json.dumps({"records": records}))
    result = import_lei_cdf_entities(str(zpath), "gleif", 92)
    assert result["entities"] == 1 and result["not_an_entity"] == 1
    assert [r["id"] for r in it_db.run_sql("SELECT id FROM Entity")] == ["lei:549300ITS31QK8VRBQ14"]
