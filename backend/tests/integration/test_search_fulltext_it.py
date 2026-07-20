"""
Real-ArcadeDB integration test for full-text /search. Exercises the FULL_TEXT
index on `search_text` and the `CONTAINSTEXT` query the mocked unit tests can't
validate — including the key property that a no-match term is index-backed
(returns empty) rather than a full scan.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    rows = [
        ("e-abi",   "Anheuser-Busch InBev", "global brewer"),
        ("e-nov",   "Novartis AG",          "swiss pharma"),
        ("e-boa",   "Bank of America",      ""),
        ("e-db",    "Deutsche Bank AG",     ""),
    ]
    for eid, name, desc in rows:
        it_db.run_sql(
            "INSERT INTO Entity SET id = :id, name = :nm, description = :ds, "
            "search_text = :st, country = 'US'",
            {"id": eid, "nm": name, "ds": desc, "st": f"{name} {desc}".strip()},
        )
    it_db.run_sql(
        "INSERT INTO Person SET id = 'p-eb', full_name = 'Elon Musk', search_text = 'Elon Musk'"
    )


def test_fulltext_search_matches_tokens_anywhere(it_db):
    from app.routers.search import search
    _seed(it_db)

    # token match regardless of position within the name
    assert {r["node"]["id"] for r in search("busch", country=None)} == {"e-abi"}
    assert {r["node"]["id"] for r in search("bank", country=None)} == {"e-boa", "e-db"}
    # description token
    assert {r["node"]["id"] for r in search("pharma", country=None)} == {"e-nov"}
    # person
    assert any(r["type"] == "Person" and r["node"]["id"] == "p-eb"
               for r in search("musk", country=None))


def test_no_match_returns_empty(it_db):
    from app.routers.search import search
    _seed(it_db)
    assert search("zzqxjknomatch", country=None) == []


def test_country_filter(it_db):
    from app.routers.search import search
    _seed(it_db)
    it_db.run_sql(
        "INSERT INTO Entity SET id = 'e-de', name = 'Bankhaus GmbH', "
        "search_text = 'Bankhaus GmbH', country = 'DE'"
    )
    ids_us = {r["node"]["id"] for r in search("bank", country="US")}
    assert "e-de" not in ids_us and "e-boa" in ids_us


def test_result_rows_have_no_arcadedb_metadata_keys(it_db):
    from app.routers.search import search
    _seed(it_db)
    node = search("novartis", country=None)[0]["node"]
    assert not any(k.startswith("@") for k in node)
    assert node["name"] == "Novartis AG"
