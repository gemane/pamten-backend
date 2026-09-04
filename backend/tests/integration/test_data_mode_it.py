"""Claims-only mode against a real database: claims recorded, edges withheld,
the sweep removes old structure but keeps claims and nodes, and full mode
plus a re-run restores everything."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.scraper import sources as src_mod
from app.scraper.graph_writer import _ensure_source, _upsert_owns
from app.scraper.maintenance import wipe_source_edges

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fresh_cache():
    src_mod._MODE_CACHE["at"] = 0.0
    src_mod._MODE_CACHE["by_source_id"] = {}
    yield
    src_mod._MODE_CACHE["at"] = 0.0
    src_mod._MODE_CACHE["by_source_id"] = {}


def _two_companies(it_db, sid=None):
    # source_id set when given, so "the sweep keeps nodes" is a BINDING
    # assertion — nodes the source itself created must survive an edge sweep.
    for eid, name in (("e1", "Holder AG"), ("e2", "Held GmbH")):
        it_db.run_command(
            "CREATE (:Entity {id: $id, name: $n, name_normalized: $nn, "
            "search_text: $n, type: 'company', source_id: $sid})",
            {"id": eid, "n": name, "nn": name.lower(), "sid": sid})


def _set_mode(it_db, mode):
    it_db.run_command(
        "MERGE (s:ScraperSource {name: 'wikidata'}) SET s.data_mode = $m, "
        "s.enabled = true, s.kind = 'instant'", {"m": mode})
    src_mod._MODE_CACHE["at"] = 0.0


def test_claims_only_asserts_but_does_not_draw_and_full_restores(it_db):
    _two_companies(it_db)
    sid = _ensure_source("Wikidata", "https://www.wikidata.org", 80)

    _set_mode(it_db, "claims_only")
    _upsert_owns("e1", "e2", sid)
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0, \
        "claims-only must not draw"
    claims = it_db.run_sql("SELECT from_id, to_id FROM Claim WHERE kind = 'owns'")
    assert [(dict(c)["from_id"], dict(c)["to_id"]) for c in claims] == [("e1", "e2")], \
        "…but it must still assert"

    _set_mode(it_db, "full")
    _upsert_owns("e1", "e2", sid)
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1, \
        "full mode plus a re-run restores the edge"


def test_the_sweep_removes_edges_keeps_nodes_and_claims(it_db):
    sid = _ensure_source("Wikidata", "https://www.wikidata.org", 80)
    _two_companies(it_db, sid)
    _set_mode(it_db, "full")
    _upsert_owns("e1", "e2", sid)
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 1

    result = wipe_source_edges("Wikidata")
    assert result["edges"]["OWNS"] == 1
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0
    assert it_db.run_sql("SELECT count(*) AS n FROM Entity")[0]["n"] == 2, \
        "nodes stay — this is not wipe-source"
    assert it_db.run_sql("SELECT count(*) AS n FROM Claim WHERE kind = 'owns'")[0]["n"] == 1, \
        "claims stay — provenance survives the sweep"


def test_the_sweep_endpoint_end_to_end(it_db, make_token):
    _two_companies(it_db)
    sid = _ensure_source("Wikidata", "https://www.wikidata.org", 80)
    _set_mode(it_db, "full")
    _upsert_owns("e1", "e2", sid)
    with TestClient(app) as c:
        r = c.post("/v1/scraper/sources/wikidata/sweep-edges?confirm=wikidata",
                   headers={"Authorization": f"Bearer {make_token(role='admin')}"})
    assert r.status_code == 200
    assert r.json()["edges"]["OWNS"] == 1
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0


def test_the_bulk_batch_writer_honours_claims_only_too(it_db):
    """The sibling path: bulk imports enqueue edges through _BatchWriter, not
    graph_writer — the mode must gate both or a bulk source ignores it."""
    from app.scraper.bulk_import import _BatchWriter
    _two_companies(it_db)
    sid = _ensure_source("Wikidata", "https://www.wikidata.org", 80)
    _set_mode(it_db, "claims_only")
    batch = _BatchWriter()
    batch.owns("e1", "Entity", "e2", {"source_id": sid, "credibility_score": 80,
                                      "source_url": "https://example.com/x"})
    batch.flush()
    assert it_db.run_sql("SELECT count(*) AS n FROM OWNS")[0]["n"] == 0
    assert it_db.run_sql("SELECT count(*) AS n FROM Claim WHERE kind = 'owns'")[0]["n"] == 1
