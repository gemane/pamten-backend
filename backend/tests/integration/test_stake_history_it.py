"""Real-ArcadeDB tests for stake history (app.stake_history).

The unit tests cover the rules as pure logic with the SQL layer mocked. These
cover what only a real database can answer: that the pre-read's `IN :keys`
finds the stored claim, that `CREATE VERTEX StakeHistory SET …` lands both as a
single statement and inside the bulk importer's script, that the numbers come
back as numbers, that the two-row first change and the one-row later change
really end up as three rows, that a merge re-points them, and that the three
readers (time travel, the timeline's crossings, the pair endpoint) read them
back through the API.
"""
import pytest

from app.claims import KIND_OWNS, record_claim
from app.db.arcadedb import run_sql
from app.stake_history import history_for_entity, history_for_pair

pytestmark = pytest.mark.integration


def _rows() -> list[dict]:
    return run_sql("SELECT FROM StakeHistory")


def _entity(it_db, eid: str, name: str) -> None:
    it_db.run_command("CREATE (e:Entity {id: $id, name: $name, name_normalized: $n, type: 'company', "
                      "search_text: $name})", {"id": eid, "name": name, "n": name.lower()})


@pytest.fixture
def it_client(it_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


# ── Writes ────────────────────────────────────────────────────────────────────

def test_a_first_statement_writes_no_row(it_db):
    record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec",
                 stake_percent=7.3, source_date="2025-02-13", filing_type="13G/A")
    assert _rows() == []


def test_the_first_change_writes_the_replaced_statement_and_the_new_one(it_db):
    record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec",
                 stake_percent=4.8, source_date="2024-02-13", filing_type="13G",
                 source_url="https://example.com/13g")
    record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec",
                 stake_percent=7.3, source_date="2025-02-13", filing_type="13G/A",
                 source_url="https://example.com/13ga")
    rows = history_for_pair("br", "ms")               # newest first
    assert [(r["stake_percent"], r["previous_percent"], r["as_of"], r["filing_type"]) for r in rows] == [
        (7.3, 4.8, "2025-02-13", "13G/A"), (4.8, None, "2024-02-13", "13G")]
    assert isinstance(rows[0]["stake_percent"], float)     # a number, not "7.3"
    assert rows[1]["source_url"] == "https://example.com/13g"


def test_a_later_change_adds_one_row_and_a_restatement_none(it_db):
    for pct, day in ((4.8, "2024-02-13"), (7.3, "2025-02-13"), (7.3, "2025-05-01"), (6.1, "2025-08-14")):
        record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec",
                     stake_percent=pct, source_date=day)
    assert [(r["stake_percent"], r["as_of"]) for r in history_for_pair("br", "ms")] == [
        (6.1, "2025-08-14"), (7.3, "2025-02-13"), (4.8, "2024-02-13")]
    # the claim itself holds the latest, as before
    assert run_sql("SELECT stake_percent FROM Claim")[0]["stake_percent"] == 6.1


def test_below_the_line_nothing_is_kept(it_db):
    for pct in (1.0, 2.0, 4.9):
        record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec", stake_percent=pct)
    assert _rows() == []


def test_the_bulk_importer_writes_the_rows_in_its_script(it_db):
    from app.scraper.bulk_import import _BatchWriter
    _entity(it_db, "e1", "Parent"); _entity(it_db, "e2", "Child")
    for pct, day in ((60.0, "2025-01-01"), (60.0, "2025-06-01"), (75.0, "2026-01-01")):
        b = _BatchWriter(batch_size=100)
        b.owns("e1", "Entity", "e2", {"stake_percent": pct, "source_id": "gleif", "source_date": day})
        b.flush()
    # the replaced statement carries the date the source LAST stated that number:
    # the claim's own date moved with the June restatement, and nothing keeps
    # the first — before it, time travel falls back to the earliest row anyway
    assert [(r["stake_percent"], r["previous_percent"], r["as_of"]) for r in history_for_pair("e1", "e2")] == [
        (75.0, 60.0, "2026-01-01"), (60.0, None, "2025-06-01")]


def test_a_merge_re_points_the_rows(it_db):
    from app.claims import migrate_claims
    record_claim(kind=KIND_OWNS, from_id="br", to_id="dup", source_id="sec", stake_percent=4.8, source_date="2024-02-13")
    record_claim(kind=KIND_OWNS, from_id="br", to_id="dup", source_id="sec", stake_percent=7.3, source_date="2025-02-13")
    migrate_claims("dup", "ms")
    assert history_for_pair("br", "dup") == []
    assert len(history_for_pair("br", "ms")) == 2


# ── Readers, through the API ──────────────────────────────────────────────────

@pytest.fixture
def graph(it_db):
    """BlackRock holds Microsoft: the edge says 7.3 % today; SEC's claim went
    4.8 → 7.3 → 6.1. Microsoft holds LinkedIn at 100 % with no history."""
    _entity(it_db, "ms", "Microsoft"); _entity(it_db, "br", "BlackRock"); _entity(it_db, "li", "LinkedIn")
    it_db.run_command("MATCH (a:Entity {id:'br'}), (b:Entity {id:'ms'}) "
                      "CREATE (a)-[:OWNS {stake_percent: 6.1, source_id: 'sec', since: '2023-01-01'}]->(b)")
    it_db.run_command("MATCH (a:Entity {id:'ms'}), (b:Entity {id:'li'}) "
                      "CREATE (a)-[:OWNS {stake_percent: 100.0, source_id: 'gleif', since: '2016-12-08'}]->(b)")
    for pct, day in ((4.8, "2024-02-13"), (7.3, "2025-02-13"), (6.1, "2025-08-14")):
        record_claim(kind=KIND_OWNS, from_id="br", to_id="ms", source_id="sec",
                     stake_percent=pct, source_date=day, filing_type="13G/A")
    record_claim(kind=KIND_OWNS, from_id="ms", to_id="li", source_id="gleif", stake_percent=100.0)


def test_time_travel_shows_the_stake_the_source_stated_on_that_day(graph, it_client):
    def owner_stake(as_of):
        r = it_client.get("/search/entity/ms/full-profile", params={"as_of": as_of} if as_of else None)
        assert r.status_code == 200
        rel = next(o for o in r.json()["owners"] if o["owner"]["id"] == "br")["relationship"]
        assert rel.get("stake_history") is True                   # the row offers the series
        return rel.get("stake_percent"), rel.get("stake_as_of")
    assert owner_stake(None) == (6.1, None)                       # the present: the edge
    assert owner_stake("2025-06-30") == (7.3, "2025-02-13")
    assert owner_stake("2024-06-30") == (4.8, "2024-02-13")
    assert owner_stake("2023-06-30") == (4.8, "2024-02-13")      # held, before the first row: the earliest
    # a holding with no history keeps the edge's number on any date
    r = it_client.get("/search/entity/ms/full-profile", params={"as_of": "2023-06-30"})
    assert [s["relationship"].get("stake_percent") for s in r.json()["subsidiaries"]] == [100.0]
    assert "stake_history" not in r.json()["subsidiaries"][0]["relationship"]


def test_the_timeline_tells_the_crossing_and_nothing_else(graph, it_client):
    events = it_client.get("/relationships/history/ms").json()
    stakes = [e for e in events if e["kind"].startswith("stake_")]
    assert [(e["kind"], e["since"], e["stake_percent"], e["previous_percent"], e["party"]["name"])
            for e in stakes] == [("stake_reached", "2025-02-13", 7.3, 4.8, "BlackRock")]
    # the 7.3 → 6.1 drift is not an event; the holding's own row has it
    assert all(e["since"] != "2025-08-14" for e in events)


def test_the_pair_endpoint_returns_the_series_newest_first(graph, it_client):
    r = it_client.get("/relationships/stake-history", params={"from_id": "br", "to_id": "ms"})
    assert r.status_code == 200
    assert [(x["as_of"], x["stake_percent"], x["previous_percent"]) for x in r.json()] == [
        ("2025-08-14", 6.1, 7.3), ("2025-02-13", 7.3, 4.8), ("2024-02-13", 4.8, None)]
    assert it_client.get("/relationships/stake-history", params={"from_id": "ms", "to_id": "li"}).json() == []


def test_the_export_carries_every_row(graph, it_client):
    import zipfile, io
    r = it_client.get("/export/entity/ms")
    assert r.status_code == 200
    content = zipfile.ZipFile(io.BytesIO(r.content)).read("content.xml").decode()
    assert 'table:name="Stake history"' in content
    assert content.count("BlackRock") >= 3
    assert history_for_entity("ms") and len(history_for_entity("ms")) == 3
