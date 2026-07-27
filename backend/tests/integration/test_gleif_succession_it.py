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
        {"LEI": wrap("ACTIVELEI00000000003"),
         "Entity": {"LegalName": wrap("Acme AG")},
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
    assert result["records"] == 3
    assert result["pairs"] == 1
    assert result["nodes"] == 2          # predecessor + successor (not the active one)

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
        "type: 'holding', country: 'US', verified: true})"
    )

    import_lei_cdf_succession(_lei_cdf_zip(tmp_path), "gleif-src", 92)

    row = it_db.run_sql(
        "SELECT type, country, verified FROM Entity WHERE id = 'lei:XCORPLEI000000000002'"
    )[0]
    assert row["type"] == "holding"      # preserved
    assert row["country"] == "US"        # preserved
    assert row["verified"] is True       # preserved
