"""
Tests for the CRUD routers: auth enforcement on writes, pagination caps,
and basic read/write behaviour. ArcadeDB is faked; auth runs for real.
"""

import pytest
from unittest.mock import patch


def auth(make_token, role="contributor"):
    return {"Authorization": f"Bearer {make_token(role=role)}"}


# ── Write endpoints require a contributor ───────────────────────────────────────

WRITE_CASES = [
    ("post", "/entities/", {"name": "Acme", "type": "company"}),
    ("post", "/persons/", {"first_name": "Ada", "last_name": "Lovelace"}),
    ("post", "/sources/", {"name": "SEC", "credibility_score": 90, "type": "register"}),
    ("post", "/relationships/owns", {"owner_id": "a", "owned_id": "b"}),
    ("post", "/relationships/dual-listed", {"entity_a_id": "a", "entity_b_id": "b"}),
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


# ── Generic registry-driven scraper endpoints ────────────────────────────────

def test_scraper_registry_lists_builtins(client, monkeypatch):
    # the built-ins' enabled() consults the per-source toggle (DB) — stub it here
    monkeypatch.setattr("app.scraper.runner.get_source_enabled", lambda name: True)
    # the registry's derived enabled() reads its own binding
    monkeypatch.setattr("app.scraper.scraper_registry.get_source_enabled", lambda name: True)
    r = client.get("/scraper/registry")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["scrapers"]] == ["wikidata", "sec_edgar", "open_corporates"]


def test_scraper_source_status_known_and_unknown(client, monkeypatch):
    monkeypatch.setattr("app.scraper.runner.get_source_enabled", lambda name: True)
    # the registry's derived enabled() reads its own binding
    monkeypatch.setattr("app.scraper.scraper_registry.get_source_enabled", lambda name: True)
    assert client.get("/scraper/source/wikidata/status").status_code == 200
    assert client.get("/scraper/source/nope/status").status_code == 404   # unknown → 404 before any DB check


def test_scraper_source_run_requires_auth(client):
    r = client.post("/scraper/source/wikidata/run", params={"company": "Acme"})
    assert r.status_code in (401, 403)   # no token


def test_scraper_source_run_master_off_and_unknown(client, make_token, monkeypatch):
    from app.config import settings
    tok = {"Authorization": f"Bearer {make_token(role='contributor')}"}
    # master switch off → 403 even for a real scraper
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", False)
    assert client.post("/scraper/source/wikidata/run", params={"company": "Acme"}, headers=tok).status_code == 403
    # master on, but no such scraper → 404
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    assert client.post("/scraper/source/nope/run", params={"company": "Acme"}, headers=tok).status_code == 404


def test_scraper_source_run_dispatches_to_registered(client, make_token, monkeypatch):
    from app.config import settings
    import app.scraper.scraper_registry as reg
    from app.scraper.scraper_registry import ScraperSpec
    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    fake = ScraperSpec("faketest",
                       lambda q, d, c=None: {"status": "ok", "total": 7, "echo": q, "depth": d, "country": c},
                       lambda: True)
    monkeypatch.setattr(reg, "_registry", {**reg._registry, "faketest": fake})

    tok = {"Authorization": f"Bearer {make_token(role='contributor')}"}
    r = client.post("/scraper/source/faketest/run", params={"company": "Acme", "depth": 1}, headers=tok)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "total": 7, "echo": "Acme", "depth": 1, "country": None}

    # The same endpoint can restrict a source to one country, upper-cased on the way in
    # so the ISO-2 comparisons downstream match.
    r = client.post("/scraper/source/faketest/run",
                    params={"company": "Acme", "depth": 1, "country": "de"}, headers=tok)
    assert r.json()["country"] == "DE"

    r = client.post("/scraper/source/faketest/run",
                    params={"company": "Acme", "country": "Germany"}, headers=tok)
    assert r.status_code == 422        # ISO-2 or nothing


# ── Stats endpoint ──────────────────────────────────────────────────────────────

def test_stats_is_public_and_maps_types(client, monkeypatch):
    import app.routers.stats as stats
    monkeypatch.setattr(stats, "_cache", None)  # bypass any cached value
    monkeypatch.setattr(stats, "run_sql", lambda *a, **k: [
        {"name": "Entity", "records": 14156151},
        {"name": "Person", "records": 10712221},
        {"name": "OWNS", "records": 1122319},
        {"name": "Source", "records": 4},
        {"name": "ScrapeRun", "records": 45},   # ignored (not in the map)
    ])
    r = client.get("/stats")   # no auth token — public
    assert r.status_code == 200
    assert r.json() == {"companies": 14156151, "people": 10712221,
                        "relationships": 1122319, "sources": 4}


def test_stats_defaults_missing_types_to_zero(client, monkeypatch):
    import app.routers.stats as stats
    monkeypatch.setattr(stats, "_cache", None)
    monkeypatch.setattr(stats, "run_sql", lambda *a, **k: [{"name": "Entity", "records": 5}])
    body = client.get("/stats").json()
    assert body == {"companies": 5, "people": 0, "relationships": 0, "sources": 0}


def test_stats_never_500s_on_db_error(client, monkeypatch):
    import app.routers.stats as stats
    monkeypatch.setattr(stats, "_cache", None)
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(stats, "run_sql", boom)
    r = client.get("/stats")
    assert r.status_code == 200
    assert r.json() == {"companies": 0, "people": 0, "relationships": 0, "sources": 0}


# ── Search endpoint ────────────────────────────────────────────────────────────

def _patch_search(entities, persons=(), exact=(), notable=(), fallback=()):
    """Patch the search router's SQL layer, dispatching by query shape (not call
    order, since the substring fallback only fires when the entity FULL_TEXT query
    is empty): the exact name_normalized lookup, the notable (wikidata) lookup, the
    entity CONTAINSTEXT query, the person CONTAINSTEXT query, and the LIKE fallback.
    fake_db still backs the suppressed-node lookup after."""
    def _dispatch(sql, params=None):
        if "CONTAINSTEXT" in sql and "Person" in sql:
            return list(persons)
        if "LIKE" in sql:
            return list(fallback)
        if "wikidata_id IS NOT NULL" in sql:
            return list(notable)
        if "name_normalized = :nn" in sql:
            return list(exact)
        if "CONTAINSTEXT" in sql:
            return list(entities)
        return []

    return patch("app.routers.search.run_sql", side_effect=_dispatch)


def test_search_returns_entity_results(client, fake_db):
    entity = {"id": "e1", "name": "AB InBev", "type": "company"}
    with _patch_search([entity]):
        r = client.get("/search/", params={"q": "inbev"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 1
    assert results[0]["node"]["name"] == "AB InBev"


def test_search_returns_person_results(client, fake_db):
    person = {"id": "p1", "full_name": "Tim Cook", "type": "person"}
    with _patch_search([], [person]):
        r = client.get("/search/", params={"q": "tim cook"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 1
    assert results[0]["type"] == "Person"


def test_search_combines_entity_and_person(client, fake_db):
    entity = {"id": "e1", "name": "Apple", "type": "company"}
    person = {"id": "p1", "full_name": "Apple Smith", "type": "person"}
    with _patch_search([entity], [person]):
        r = client.get("/search/", params={"q": "apple"})
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_search_with_country_filter(client, fake_db):
    entity = {"id": "e1", "name": "Heineken", "type": "company", "country": "NL"}
    with _patch_search([entity]):
        r = client.get("/search/", params={"q": "heineken", "country": "NL"})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_search_strips_arcadedb_metadata_from_results(client, fake_db):
    entity = {"@rid": "#1:0", "@type": "Entity", "@cat": "v",
              "id": "e1", "name": "Acme", "type": "company"}
    with _patch_search([entity]):
        r = client.get("/search/", params={"q": "acme"})
    node = r.json()[0]["node"]
    assert not any(k.startswith("@") for k in node)
    assert node["name"] == "Acme"


def test_search_rejects_short_query(client):
    assert client.get("/search/", params={"q": "a"}).status_code == 422


def test_search_falls_back_to_substring_when_fulltext_empty(client, fake_db):
    # FULL_TEXT (CONTAINSTEXT) returns nothing — a degraded index — but the company
    # is in the DB and the LIKE fallback finds it.
    hit = {"id": "e9", "name": "SoftBank Group", "type": "company"}
    with _patch_search(entities=[], persons=[], fallback=[hit]):
        r = client.get("/search/", params={"q": "softbank"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 1 and results[0]["node"]["name"] == "SoftBank Group"


def test_search_no_fallback_when_fulltext_has_results(client, fake_db):
    # When the FULL_TEXT query already matches, the fallback must NOT run (its rows
    # would be ignored anyway) — the FULL_TEXT hit stands alone.
    ft = {"id": "e1", "name": "Apple Inc.", "type": "company"}
    never = {"id": "x", "name": "should-not-appear", "type": "company"}
    with _patch_search(entities=[ft], fallback=[never]):
        r = client.get("/search/", params={"q": "apple"})
    assert r.status_code == 200
    ids = [x["node"]["id"] for x in r.json()]
    assert ids == ["e1"]


def test_search_fallback_disabled_by_setting(client, fake_db):
    from app.config import settings
    hit = {"id": "e9", "name": "SoftBank Group", "type": "company"}
    with patch.object(settings, "SEARCH_SUBSTRING_FALLBACK", False):
        with _patch_search(entities=[], fallback=[hit]):
            r = client.get("/search/", params={"q": "softbank"})
    assert r.status_code == 200 and r.json() == []


def test_search_ranks_notable_wikidata_entity_first(client, fake_db):
    # Same match quality (both contain "heineken", tier 2): the curated Wikidata
    # company should float above raw GLEIF registry entries.
    gleif = {"id": "g1", "name": "HEINEKEN VIETNAM BEER", "type": "company"}
    notable = {"id": "w1", "name": "Heineken Holding", "type": "company", "wikidata_id": "Q1"}
    # DB returns the GLEIF one first; ranking should surface the notable one.
    with _patch_search([gleif, notable]):
        r = client.get("/search/", params={"q": "heineken"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "w1"


def test_search_notable_lookup_guarantees_parent_in_results(client, fake_db):
    # The curated parent is crowded OUT of the main CONTAINSTEXT results (only
    # GLEIF subsidiaries there) but the dedicated notable lookup still fetches it,
    # so it appears and ranks first.
    subs = [{"id": f"g{i}", "name": f"HEINEKEN SUB {i}", "type": "company"} for i in range(5)]
    notable = {"id": "w1", "name": "Heineken Holding", "type": "company", "wikidata_id": "Q1"}
    with _patch_search(subs, notable=[notable]):
        r = client.get("/search/", params={"q": "heineken"})
    assert r.status_code == 200
    ids = [x["node"]["id"] for x in r.json()]
    assert ids[0] == "w1" and "w1" in ids


def test_search_notable_does_not_beat_a_better_name_match(client, fake_db):
    # A GLEIF subsidiary that matches BOTH query words must still beat a notable
    # entity that matches only one — notable is a tiebreaker, not an override.
    notable = {"id": "w1", "name": "Heineken Holding", "type": "company", "wikidata_id": "Q1"}
    subsidiary = {"id": "g1", "name": "Heineken Vietnam Brewery", "type": "company"}
    with _patch_search([notable, subsidiary]):
        r = client.get("/search/", params={"q": "heineken vietnam"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "g1"


def test_search_ranks_exact_match_first(client, fake_db):
    austria = {"id": "e2", "name": "Apple Sales International Austria GmbH", "type": "company"}
    main    = {"id": "e1", "name": "Apple Inc.", "type": "company"}
    # DB returns Austria first (worse match), but ranking should put Apple Inc. first
    with _patch_search([austria, main]):
        r = client.get("/search/", params={"q": "apple inc."})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "e1"


def test_search_ranks_starts_with_before_contains(client, fake_db):
    division = {"id": "e2", "name": "Greater Apple Valley Holdings", "type": "company"}
    main     = {"id": "e1", "name": "Apple Inc.", "type": "company"}
    with _patch_search([division, main]):
        r = client.get("/search/", params={"q": "apple"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "e1"


def test_search_ranks_shorter_starts_with_name_first(client, fake_db):
    long_name  = {"id": "e2", "name": "Apple Sales International Austria GmbH", "type": "company"}
    short_name = {"id": "e1", "name": "Apple Inc.", "type": "company"}
    with _patch_search([long_name, short_name]):
        r = client.get("/search/", params={"q": "apple"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "e1"


def test_search_ranks_more_query_words_in_name_first(client, fake_db):
    # "dangote group" → the name matching BOTH words beats a bare "* Group" that
    # only matched the common word. DB returns the common-word hit first.
    group_noise = {"id": "e2", "name": "BLG Group", "type": "company"}
    both        = {"id": "e1", "name": "Carlsberg Group", "type": "company"}
    with _patch_search([group_noise, both]):
        r = client.get("/search/", params={"q": "carlsberg group"})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "e1"   # matched 2 words, not just "group"


def test_search_exact_name_ranks_first(client, fake_db):
    # "BlackRock, Inc." — CONTAINSTEXT floods with fund variants that share the
    # common token "inc"; the exact company (fetched by the name_normalized
    # lookup) must still lead.
    exact = {"id": "br", "name": "BlackRock, Inc.", "name_normalized": "blackrock", "type": "company"}
    fund1 = {"id": "f1", "name": "BLACKROCK MUNIYIELD FUND, INC.", "name_normalized": "blackrock muniyield fund", "type": "company"}
    fund2 = {"id": "f2", "name": "BLACKROCK CAPITAL HOLDINGS, INC.", "name_normalized": "blackrock capital holdings", "type": "company"}
    # DB returns only the funds from CONTAINSTEXT; the exact node comes from the
    # name_normalized lookup.
    with _patch_search([fund1, fund2], exact=[exact]):
        r = client.get("/search/", params={"q": "BlackRock, Inc."})
    assert r.status_code == 200
    assert r.json()[0]["node"]["id"] == "br"


def test_search_dedupes_repeated_node_id(client, fake_db):
    # ArcadeDB's FULL_TEXT can return a row per index bucket — same node twice.
    dup = {"id": "e1", "name": "Axel Springer SE", "type": "company"}
    with _patch_search([dup, dup]):
        r = client.get("/search/", params={"q": "axel springer"})
    assert r.status_code == 200
    assert [x["node"]["id"] for x in r.json()] == ["e1"]


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


def test_sources_for_entity_excludes_subsidiaries(client, fake_db):
    # An entity's Sources panel must not list a row per subsidiary — that
    # flooded the panel, and a subsidiary's own source shows when you select it.
    #
    # This used to depend on deliberately *not* writing an outbound-ownership
    # query. Relationship provenance now comes from Claim rows selected on
    # `to_id`, so only things asserted *about* this entity match: a claim about
    # a subsidiary carries from_id = this entity and is never selected. The
    # exclusion is structural rather than an omission someone could undo.
    r = client.get("/sources/entity/e1")
    assert r.status_code == 200
    cyphers = [c for c, _ in fake_db.calls]
    assert len(cyphers) == 2  # relationship claims + entity-self
    assert any("MATCH (c:Claim {to_id: $entity_id})" in c for c in cyphers)
    assert not any("from_id: $entity_id" in c for c in cyphers)


def test_sources_for_entity_reads_relationship_provenance_from_claims(client, fake_db):
    # The edges carry one source_id each, so reading provenance from them
    # reported a single source per relationship — when a second source confirmed
    # an ownership it overwrote the first's link and the earlier source vanished.
    r = client.get("/sources/entity/e1")
    assert r.status_code == 200
    cyphers = [c for c, _ in fake_db.calls]
    assert not any("[r:OWNS]" in c or "[r:HAS_ROLE]" in c for c in cyphers)


def test_create_dual_listed_links_two_entities(client, fake_db, make_token):
    fake_db.queue([{"r": {"source_id": "s1"}}])  # MERGE ... RETURN r
    r = client.post(
        "/relationships/dual-listed",
        json={"entity_a_id": "unilever-plc", "entity_b_id": "unilever-nv",
              "source_id": "s1", "source_url": "https://www.wikidata.org/wiki/Q157062"},
        headers=auth(make_token, "contributor"),
    )
    assert r.status_code == 200
    cypher, params = fake_db.calls[-1]
    assert "DUAL_LISTED_WITH" in cypher
    assert params["entity_a_id"] == "unilever-plc"
    assert params["entity_b_id"] == "unilever-nv"
    assert params["last_scraped_at"]  # server-stamped


def test_create_dual_listed_404_when_entity_missing(client, fake_db, make_token):
    fake_db.queue([])  # MERGE matched nothing
    r = client.post(
        "/relationships/dual-listed",
        json={"entity_a_id": "a", "entity_b_id": "missing"},
        headers=auth(make_token, "contributor"),
    )
    assert r.status_code == 404


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


def test_create_owns_records_the_claim(client, fake_db, make_token):
    """The manual API asserts like any scraper: edge + claim, or the entry
    could never corroborate (or be contradicted by) a scraped one."""
    fake_db.queue([{"r": {"source_id": "s1"}}])
    with patch("app.routers.relationships.record_claim") as rc:
        r = client.post(
            "/relationships/owns",
            json={"owner_id": "a", "owned_id": "b", "ownership_type": "majority",
                  "stake_percent": 61.0, "source_id": "s1",
                  "source_url": "https://example.com/x", "source_date": "2025-02-14"},
            headers=auth(make_token, "contributor"),
        )
    assert r.status_code == 200
    kw = rc.call_args.kwargs
    assert (kw["from_id"], kw["to_id"], kw["source_id"]) == ("a", "b", "s1")
    assert kw["stake_percent"] == 61.0
    assert kw["ownership_type"] == "majority"     # the enum's value, not its repr
    assert kw["credibility_score"] == 80          # unstated → claim default, not None


def test_create_owns_without_a_source_records_no_claim(client, fake_db, make_token):
    """A claim is one source's statement; keyed on (kind|from|to|source), an
    unsourced one would collide with every other unsourced claim on the pair."""
    fake_db.queue([{"r": {}}])
    with patch("app.routers.relationships.record_claim") as rc:
        r = client.post(
            "/relationships/owns",
            json={"owner_id": "a", "owned_id": "b", "ownership_type": "majority"},
            headers=auth(make_token, "contributor"),
        )
    assert r.status_code == 200
    rc.assert_not_called()


def test_create_owns_failure_records_no_claim(client, fake_db, make_token):
    """No edge, no claim — a 404 must not leave evidence for a fact that was
    never written."""
    fake_db.queue([])  # CREATE matched nothing
    with patch("app.routers.relationships.record_claim") as rc:
        r = client.post(
            "/relationships/owns",
            json={"owner_id": "a", "owned_id": "missing",
                  "ownership_type": "majority", "source_id": "s1"},
            headers=auth(make_token, "contributor"),
        )
    assert r.status_code == 404
    rc.assert_not_called()


def test_owners_query_drops_proven_shortcuts(fake_db):
    """search.py's node sections already exclude shortcut-stamped edges; the
    owners list must apply the same rule or the two views disagree about who
    owns the company (the exact drift class this refactor exists to end)."""
    from app.routers.relationships import owners_of
    owners_of("e1")
    cypher, _ = fake_db.calls[0]
    assert "r.shortcut IS NULL OR r.shortcut <> true" in cypher


def test_tree_query_drops_proven_shortcuts_on_every_hop(fake_db):
    """COALESCE form, not _NOT_A_SHORTCUT: ArcadeDB rejects parenthesized
    predicates inside ALL() — see the comment at the query site."""
    from app.routers.relationships import ownership_tree_of
    ownership_tree_of("e1", depth=3, include_indirect=True)
    cypher, _ = fake_db.calls[0]
    assert "ALL(e IN r WHERE" in cypher
    assert "COALESCE(e.shortcut, false) <> true" in cypher
    assert "(e.shortcut IS NULL" not in cypher, \
        "parenthesized predicates inside ALL() fail on real ArcadeDB"


def test_tree_query_keeps_the_indirect_filter_composable(fake_db):
    from app.routers.relationships import ownership_tree_of
    ownership_tree_of("e1", depth=3, include_indirect=False)
    cypher, _ = fake_db.calls[0]
    assert "COALESCE(e.shortcut, false) <> true" in cypher
    assert "COALESCE(e.direct_or_indirect, 'direct') <> 'indirect'" in cypher
