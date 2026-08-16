"""
Real-ArcadeDB test that on-demand `ensure_scrape` dedups ACROSS sources in one scope.

Reproduces the Alphabet bug: a Wikidata-style source creates "Larry Page" (alias
"Lawrence Page") and a SEC-style source creates the reversed "Page Lawrence", both owning
the same company. Because both sources now share ONE `@_with_autodedup` scope, the person
auto-merge sees them together and merges them (same name-token set + shared company =
high confidence). Separate per-source scopes — the previous behaviour — left them split.
"""
import pytest

pytestmark = pytest.mark.integration

CO = "co-1"


def _wikidata_like():
    from app.db.arcadedb import run_command, run_sql
    from app.scraper.graph_writer import _record_touched, _record_touched_entity, set_scrape_target
    from app.scraper.mapper import normalize_entity_name

    def _run(query, depth, country=None):
        run_sql("UPDATE Entity SET name = :n, name_normalized = :nn, search_text = :st, "
                "type = 'company' UPSERT WHERE id = :id",
                {"n": query, "nn": normalize_entity_name(query), "st": query.lower(), "id": CO})
        run_sql("UPDATE Person SET first_name='Larry', last_name='Page', full_name='Larry Page', "
                "alias=:al, source_id='wd' UPSERT WHERE id='p-larry'", {"al": ["Lawrence Page"]})
        run_command("MATCH (p:Person {id:'p-larry'}) MATCH (e:Entity {id:$c}) "
                    "CREATE (p)-[:OWNS {ownership_type:'controlling'}]->(e)", {"c": CO})
        _record_touched("p-larry")
        _record_touched_entity(CO)
        set_scrape_target(CO, depth)
        return {"status": "ok", "total": 2, "entity_id": CO}
    return _run


def _sec_like():
    from app.db.arcadedb import run_command, run_sql
    from app.scraper.graph_writer import _record_touched, set_scrape_target

    def _run(query, depth, country=None):
        run_sql("UPDATE Person SET first_name='Page', last_name='Lawrence', "
                "full_name='Page Lawrence', source_id='sec' UPSERT WHERE id='p-pl'")
        run_command("MATCH (p:Person {id:'p-pl'}) MATCH (e:Entity {id:$c}) "
                    "CREATE (p)-[:OWNS {ownership_type:'controlling'}]->(e)", {"c": CO})
        _record_touched("p-pl")
        set_scrape_target(CO, 0)
        return {"status": "ok", "total": 1, "entity_id": CO}
    return _run


def test_ensure_merges_cross_source_person_duplicates(it_db, monkeypatch):
    from app.config import settings
    from app.db.arcadedb import run_command
    from app.scraper import ondemand
    from app.scraper.graph_writer import _with_autodedup
    from app.scraper.scraper_registry import ScraperSpec, register

    monkeypatch.setattr(settings, "SCRAPER_ENABLED", True)
    monkeypatch.setattr(settings, "SCRAPER_AUTODEDUP_ENABLED", True)   # the whole point
    monkeypatch.setattr("app.scraper.scraper_registry._registry", {})
    # Each source is itself @_with_autodedup (like the real runners) → nested under the
    # single ensure scope, feeding one shared collector.
    register(ScraperSpec("wikidata", _with_autodedup(_wikidata_like()), lambda: True,
                         kind="instant", depth_aware=True))
    register(ScraperSpec("sec_edgar", _with_autodedup(_sec_like()), lambda: True,
                         kind="instant", depth_aware=False))

    out = ondemand.ensure_scrape("Acme Holdings", depth=1)
    assert out["scraped"] and out["reason"] == "absent"

    owners = run_command(
        "MATCH (p:Person)-[:OWNS]->(e:Entity {id:$c}) RETURN DISTINCT p.id AS id", {"c": CO})
    ids = {dict(r)["id"] for r in owners}
    # The two cross-source spellings were merged into a single surviving person.
    assert len(ids) == 1, f"expected the duplicates merged, got {ids}"
