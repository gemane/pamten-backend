"""
Tests for the CRUD routers: auth enforcement on writes, pagination caps,
and basic read/write behaviour. ArcadeDB is faked; auth runs for real.
"""

import pytest


def auth(make_token, role="contributor"):
    return {"Authorization": f"Bearer {make_token(role=role)}"}


# ── Write endpoints require a contributor ───────────────────────────────────────

WRITE_CASES = [
    ("post", "/entities/", {"name": "Acme", "type": "company"}),
    ("post", "/persons/", {"first_name": "Ada", "last_name": "Lovelace"}),
    ("post", "/sources/", {"name": "SEC", "credibility_score": 90, "type": "register"}),
    ("post", "/relationships/owns", {"owner_id": "a", "owned_id": "b"}),
    ("post", "/locations/", {"country": "US"}),
]


@pytest.mark.parametrize("method,path,body", WRITE_CASES)
def test_write_requires_authentication(client, method, path, body):
    r = getattr(client, method)(path, json=body)
    assert r.status_code == 401


@pytest.mark.parametrize("method,path,body", WRITE_CASES)
def test_write_rejects_viewer_role(client, make_token, method, path, body):
    r = getattr(client, method)(path, json=body, headers=auth(make_token, "viewer"))
    assert r.status_code == 403


# ── A contributor gets past the guard (into the DB layer) ───────────────────────

def test_create_entity_succeeds_for_contributor(client, fake_db, make_token):
    fake_db.queue([{"e": {"name": "Acme", "type": "company", "verified": False}}])
    r = client.post("/entities/", json={"name": "Acme", "type": "company"},
                    headers=auth(make_token, "contributor"))
    assert r.status_code == 200
    assert r.json()["name"] == "Acme"


def test_create_source_succeeds_for_admin(client, fake_db, make_token):
    fake_db.queue([{"s": {"name": "SEC", "credibility_score": 90, "type": "register"}}])
    r = client.post("/sources/", json={"name": "SEC", "credibility_score": 90, "type": "register"},
                    headers=auth(make_token, "admin"))
    assert r.status_code == 200
    assert r.json()["name"] == "SEC"


# ── Read endpoints are public ───────────────────────────────────────────────────

def test_get_entity_is_public(client, fake_db):
    fake_db.queue([{"e": {"id": "e1", "name": "Acme", "type": "company", "verified": True}}])
    r = client.get("/entities/e1")
    assert r.status_code == 200
    assert r.json()["name"] == "Acme"


def test_get_missing_entity_returns_404(client, fake_db):
    fake_db.queue([])  # not found
    assert client.get("/entities/nope").status_code == 404


def test_list_entities_is_public(client, fake_db):
    fake_db.queue([{"e": {"id": "e1", "name": "A", "type": "company", "verified": True}}])
    r = client.get("/entities/")
    assert r.status_code == 200
    assert len(r.json()) == 1


# ── Pagination caps ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/entities/", "/persons/", "/sources/"])
def test_pagination_limit_ceiling_enforced(client, path):
    # limit above the Query(le=100) ceiling is rejected before the handler runs
    assert client.get(path, params={"limit": 999999999}).status_code == 422


@pytest.mark.parametrize("path", ["/entities/", "/persons/", "/sources/"])
def test_pagination_negative_skip_rejected(client, path):
    assert client.get(path, params={"skip": -5}).status_code == 422


def test_by_country_limit_ceiling_enforced(client):
    assert client.get("/entities/by-country/US", params={"limit": 10_000}).status_code == 422


# ── Scraper status endpoint ────────────────────────────────────────────────────

def test_scraper_status_includes_wikidata_enabled(client):
    r = client.get("/scraper/status")
    assert r.status_code == 200
    data = r.json()
    assert "wikidata_enabled" in data


# ── Search endpoint ────────────────────────────────────────────────────────────

def test_search_returns_entity_results(client, fake_db):
    entity = {"id": "e1", "name": "AB InBev", "type": "company"}
    # Two separate queries: entity then person
    fake_db.queue([{"node": entity, "score": 1.0, "type": "Entity"}])
    fake_db.queue([])  # no person results
    r = client.get("/search/", params={"q": "inbev"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 1
    assert results[0]["node"]["name"] == "AB InBev"


def test_search_returns_person_results(client, fake_db):
    person = {"id": "p1", "full_name": "Tim Cook", "type": "person"}
    fake_db.queue([])  # no entity results
    fake_db.queue([{"node": person, "score": 1.0, "type": "Person"}])
    r = client.get("/search/", params={"q": "tim cook"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 1
    assert results[0]["type"] == "Person"


def test_search_combines_entity_and_person(client, fake_db):
    entity = {"id": "e1", "name": "Apple", "type": "company"}
    person = {"id": "p1", "full_name": "Apple Smith", "type": "person"}
    fake_db.queue([{"node": entity, "score": 1.0, "type": "Entity"}])
    fake_db.queue([{"node": person, "score": 1.0, "type": "Person"}])
    r = client.get("/search/", params={"q": "apple"})
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_search_with_country_filter(client, fake_db):
    entity = {"id": "e1", "name": "Heineken", "type": "company", "country": "NL"}
    fake_db.queue([{"node": entity, "score": 1.0, "type": "Entity"}])
    r = client.get("/search/", params={"q": "heineken", "country": "NL"})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_search_rejects_short_query(client):
    assert client.get("/search/", params={"q": "a"}).status_code == 422


def test_search_finds_entity_by_alias(client, fake_db):
    entity = {"id": "e1", "name": "Anheuser-Busch InBev", "type": "company",
              "aliases": ["AB InBev", "ABInBev"]}
    fake_db.queue([{"node": entity, "score": 1.0, "type": "Entity"}])
    fake_db.queue([])
    r = client.get("/search/", params={"q": "ab inbev"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["name"] == "Anheuser-Busch InBev"


def test_search_ranks_exact_match_first(client, fake_db):
    austria = {"id": "e2", "name": "Apple Sales International Austria GmbH", "type": "company"}
    main    = {"id": "e1", "name": "Apple Inc.", "type": "company"}
    # DB returns Austria first (worse match), but ranking should put Apple Inc. first
    fake_db.queue([
        {"node": austria, "score": 1.0, "type": "Entity"},
        {"node": main,    "score": 1.0, "type": "Entity"},
    ])
    fake_db.queue([])
    r = client.get("/search/", params={"q": "apple inc."})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "e1"


def test_search_ranks_starts_with_before_contains(client, fake_db):
    division = {"id": "e2", "name": "Greater Apple Valley Holdings", "type": "company"}
    main     = {"id": "e1", "name": "Apple Inc.", "type": "company"}
    fake_db.queue([
        {"node": division, "score": 1.0, "type": "Entity"},
        {"node": main,     "score": 1.0, "type": "Entity"},
    ])
    fake_db.queue([])
    r = client.get("/search/", params={"q": "apple"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "e1"


def test_search_ranks_shorter_starts_with_name_first(client, fake_db):
    long_name  = {"id": "e2", "name": "Apple Sales International Austria GmbH", "type": "company"}
    short_name = {"id": "e1", "name": "Apple Inc.", "type": "company"}
    fake_db.queue([
        {"node": long_name,  "score": 1.0, "type": "Entity"},
        {"node": short_name, "score": 1.0, "type": "Entity"},
    ])
    fake_db.queue([])
    r = client.get("/search/", params={"q": "apple"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "e1"


# ── Provenance: per-entry source + dates + verifiable link ──────────────────────

def test_sources_for_entity_returns_provenance(client, fake_db):
    # The endpoint runs several simple per-source queries and merges in Python.
    # Rows come back with the RETURN columns (source_url + source_home_url); the
    # router computes `url` (specific record wins over the source home page).
    fake_db.queue([
        {
            "id": "s1", "name": "SEC EDGAR", "type": "register",
            "credibility_score": 95,
            "source_home_url": "https://www.sec.gov",
            "source_url": "https://www.sec.gov/Archives/edgar/data/320193/000.../primary.htm",
            "source_date": "2025-02-14",
            "last_scraped_at": "2026-07-12T09:00:00+00:00",
        },
    ])
    r = client.get("/sources/entity/e1")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["url"].endswith("primary.htm")          # specific record, verifiable
    assert row["source_date"] == "2025-02-14"          # date recorded in the source
    assert row["last_scraped_at"].startswith("2026-07-12")  # when we last checked it


def test_sources_for_entity_falls_back_to_home_url(client, fake_db):
    # Older/manual data has no per-edge source_url → fall back to the source home.
    fake_db.queue([
        {
            "id": "s2", "name": "Wikidata", "type": "knowledge_base",
            "credibility_score": 70,
            "source_home_url": "https://www.wikidata.org",
            "source_url": None, "source_date": None, "last_scraped_at": None,
        },
    ])
    r = client.get("/sources/entity/e1")
    assert r.status_code == 200
    assert r.json()[0]["url"] == "https://www.wikidata.org"


def test_create_owns_persists_provenance(client, fake_db, make_token):
    fake_db.queue([{"r": {"source_id": "s1"}}])  # CREATE ... RETURN r
    r = client.post(
        "/relationships/owns",
        json={
            "owner_id": "a", "owned_id": "b", "ownership_type": "majority",
            "source_id": "s1",
            "source_url": "https://www.sec.gov/Archives/edgar/data/1/x.htm",
            "source_date": "2025-02-14",
        },
        headers=auth(make_token, "contributor"),
    )
    assert r.status_code == 200
    # The write must carry provenance into the DB layer, including a
    # server-stamped last_scraped_at.
    _cypher, params = fake_db.calls[-1]
    assert params["source_url"] == "https://www.sec.gov/Archives/edgar/data/1/x.htm"
    assert params["source_date"] == "2025-02-14"
    assert params["last_scraped_at"]  # non-empty ISO timestamp
