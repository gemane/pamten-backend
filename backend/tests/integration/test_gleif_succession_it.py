"""
Real-ArcadeDB test for the GLEIF LEI-CDF succession importer: builds a tiny
synthetic golden-copy zip (the {"$": …} wrapping + SuccessorEntity array), runs
the importer, and asserts the SUCCEEDED_BY edge + node names landed — the batch
UPSERT + edge path the mocked unit tests can't validate.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import json
import zipfile

import pytest

pytestmark = pytest.mark.integration


def _lei_cdf_zip(tmp_path):
    """A minimal LEI-CDF golden-copy zip: Twitter (MERGED → X Corp.) + X Corp.
    plus an unrelated active entity that must NOT get a succession edge."""
    def wrap(v):
        return {"$": v}
    records = [
        {"LEI": wrap("TWITTERLEI0000000001"),
         "Entity": {"LegalName": wrap("Twitter, Inc."),
                    "SuccessorEntity": [{"SuccessorLEI": wrap("XCORPLEI000000000002")}]},
         "Registration": {"RegistrationStatus": wrap("MERGED")}},
        {"LEI": wrap("XCORPLEI000000000002"),
         "Entity": {"LegalName": wrap("X Corp.")},
         "Registration": {"RegistrationStatus": wrap("ISSUED")}},
        # a DUPLICATE registration of X Corp. pointing at its live record: not a
        # succession (findings item 88) — no edge, no node minted
        {"LEI": wrap("XCORPDUPLEI000000006"),
         "Entity": {"LegalName": wrap("X Corp."),
                    "SuccessorEntity": [{"SuccessorLEI": wrap("XCORPLEI000000000002")}]},
         "Registration": {"RegistrationStatus": wrap("DUPLICATE")}},
        {"LEI": wrap("ACTIVELEI00000000003"),
         "Entity": {"LegalName": wrap("Acme AG")},
         "Registration": {"RegistrationStatus": wrap("ISSUED")}},
        # a succession that touches none of the curated companies (item 85b)
        {"LEI": wrap("OTHERPREDLEI00000004"),
         "Entity": {"LegalName": wrap("Old Other Ltd"),
                    "SuccessorEntity": [{"SuccessorLEI": wrap("OTHERSUCCLEI00000005")}]},
         "Registration": {"RegistrationStatus": wrap("MERGED")}},
        {"LEI": wrap("OTHERSUCCLEI00000005"),
         "Entity": {"LegalName": wrap("New Other Ltd")},
         "Registration": {"RegistrationStatus": wrap("ISSUED")}},
    ]
    payload = {"records": records}
    zpath = tmp_path / "lei2.json.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lei2-golden-copy.json", json.dumps(payload))
    return str(zpath)


def test_imports_succession_edge_and_names(it_db, tmp_path):
    from app.scraper.gleif_succession import import_lei_cdf_succession
    from app.routers.search import get_full_profile

    it_db.run_command("CREATE (:Source {id: 'gleif-src', name: 'GLEIF', type: 'register'})")

    result = import_lei_cdf_succession(_lei_cdf_zip(tmp_path), "gleif-src", 92)
    assert result["records"] == 6
    assert result["pairs"] == 2 and result["not_a_succession"] == 1
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity WHERE id = 'lei:XCORPDUPLEI000000006'")[0]["n"] == 0
    assert result["nodes"] == 4          # both pairs' endpoints (not the active one)

    # Nodes were created keyed by LEI, with names from the golden copy.
    pred = get_full_profile("lei:TWITTERLEI0000000001")
    assert pred["entity"]["name"] == "Twitter, Inc."
    assert [e["name"] for e in pred["succeeded_by"]] == ["X Corp."]

    succ = get_full_profile("lei:XCORPLEI000000000002")
    assert succ["entity"]["name"] == "X Corp."
    assert [e["name"] for e in succ["replaces"]] == ["Twitter, Inc."]


def test_does_not_clobber_existing_entity_fields(it_db, tmp_path):
    """Upserting an existing successor must keep its type/country, only ensure the
    edge + lei — the importer sets a minimal, non-destructive prop set."""
    from app.scraper.gleif_succession import import_lei_cdf_succession

    it_db.run_command("CREATE (:Source {id: 'gleif-src', name: 'GLEIF', type: 'register'})")
    it_db.run_command(
        "CREATE (:Entity {id: 'lei:XCORPLEI000000000002', name: 'X Corp.', "
        "type: 'holding', country: 'US', description: 'kept'})"
    )

    import_lei_cdf_succession(_lei_cdf_zip(tmp_path), "gleif-src", 92)

    row = it_db.run_sql(
        "SELECT type, country, description FROM Entity WHERE id = 'lei:XCORPLEI000000000002'"
    )[0]
    assert row["type"] == "holding"      # preserved
    assert row["country"] == "US"        # preserved
    assert row["description"] == "kept"  # preserved


def test_the_curated_import_writes_only_the_successions_touching_it(it_db, tmp_path):
    # findings item 85(b): the test import holds X Corp.; the succession pass adds
    # Twitter → X Corp. and nothing of the unrelated pair, and names the new
    # endpoint for the follow-up `gleif-lei-cdf --only-file`
    from app.scraper.gleif_succession import import_lei_cdf_succession
    from app.routers.search import get_full_profile
    it_db.run_command("CREATE (:Source {id: 'gleif-src', name: 'GLEIF', type: 'register'})")
    it_db.run_command("CREATE (:Entity {id: 'lei:XCORPLEI000000000002', lei_id: 'XCORPLEI000000000002', "
                      "name: 'X Corp.', name_normalized: 'x', search_text: 'X Corp.', type: 'company'})")
    out = tmp_path / "new-endpoints.txt"
    result = import_lei_cdf_succession(_lei_cdf_zip(tmp_path), "gleif-src", 92,
                                       only_leis={"XCORPLEI000000000002"}, emit_leis_path=str(out))
    # the duplicate registration touches X Corp. too (as the successor) and is skipped, counted
    assert result == {"records": 2, "pairs": 1, "nodes": 2, "errors": 0, "not_a_succession": 1, "emitted": 1}
    assert out.read_text().split() == ["TWITTERLEI0000000001"]
    assert [e["name"] for e in get_full_profile("lei:XCORPLEI000000000002")["replaces"]] == ["Twitter, Inc."]
    ids = {r["id"] for r in it_db.run_sql("SELECT id FROM Entity WHERE id LIKE 'lei:%'")}
    assert ids == {"lei:XCORPLEI000000000002", "lei:TWITTERLEI0000000001"}      # no Other Ltd pair
    assert it_db.run_sql("SELECT count(*) AS n FROM SUCCEEDED_BY")[0]["n"] == 1
