"""Real-ArcadeDB tests for the industry classification (app.industry).

The unit tests pin the mappings and the rules with the SQL layer mocked.
These cover what only a real database answers: that the `IndustryCode`
UPSERT on `key` lands (once per entity, scheme and position), that the
Entity stamp follows the precedence, that the UK bulk sync writes its
script and dates the statements, that a merge re-keys the rows, and that
the full profile serves the tree with its EN and DE labels.
"""
import pytest

from app import industry
from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration


def _entity(it_db, eid: str, name: str, **props) -> None:
    it_db.run_command("CREATE (e:Entity {id: $id, name: $name, name_normalized: $n, type: 'company', "
                      "search_text: $name})", {"id": eid, "name": name, "n": name.lower()})
    if props:
        sets = ", ".join(f"e.{k} = ${k}" for k in props)
        it_db.run_command(f"MATCH (e:Entity {{id: $id}}) SET {sets}", {"id": eid, **props})


def _source(it_db, sid: str, name: str) -> str:
    it_db.run_command("CREATE (s:Source {id: $id, name: $name, type: 'register', credibility_score: 97})",
                      {"id": sid, "name": name})
    return sid


def _nace(eid: str) -> str | None:
    return run_sql("SELECT nace_code FROM Entity WHERE id = :e", {"e": eid})[0].get("nace_code")


@pytest.fixture
def it_client(it_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


# ── Writes ────────────────────────────────────────────────────────────────────

def test_a_statement_is_written_once_and_stamps_the_entity(it_db):
    _entity(it_db, "e1", "Wienerberger Ltd")
    src = _source(it_db, "s-ch", "Companies House")
    industry.record("e1", "uk-sic", "23320", "Manufacture of bricks", source_id=src,
                    source_url="https://example.com/company/1", source_date="2026-07-01")
    industry.record("e1", "uk-sic", "23320", "Manufacture of bricks", source_id=src,
                    source_url="https://example.com/company/1", source_date="2026-08-01")
    rows = run_sql("SELECT FROM IndustryCode WHERE entity_id = 'e1'")
    assert len(rows) == 1
    assert rows[0]["nace_code"] == "23.32" and rows[0]["source_date"] == "2026-08-01"
    assert rows[0]["first_seen_at"] and rows[0]["first_seen_at"] <= rows[0]["last_seen_at"]
    assert _nace("e1") == "23.32"


def test_the_register_code_outranks_the_crosswalked_one(it_db):
    _entity(it_db, "e2", "Dual Filer plc")
    sec, ch = _source(it_db, "s-sec", "SEC EDGAR"), _source(it_db, "s-ch", "Companies House")
    industry.record("e2", "us-sic", "7370", "Services-Computer Programming", source_id=sec)
    assert _nace("e2") == "J"                                  # the crosswalk reaches the section
    industry.record("e2", "uk-sic", "62012", "Business and domestic software development", source_id=ch)
    assert _nace("e2") == "62.01"                              # the register's own NACE class
    assert len(run_sql("SELECT FROM IndustryCode WHERE entity_id = 'e2'")) == 2


def test_a_uk_only_code_leaves_the_tree_to_a_lesser_scheme(it_db):
    _entity(it_db, "e3", "Dormant Holdings Ltd")
    sec, ch = _source(it_db, "s-sec", "SEC EDGAR"), _source(it_db, "s-ch", "Companies House")
    industry.record("e3", "uk-sic", "99999", "Dormant Company", source_id=ch)
    assert _nace("e3") is None
    industry.record("e3", "us-sic", "3711", "Motor Vehicles", source_id=sec)
    assert _nace("e3") == "29.10"


def test_the_uk_bulk_sync_writes_dated_statements_per_position(it_db):
    _source(it_db, "s-ch", "Companies House Register")
    _entity(it_db, "gb-coh:00000002", "Wienerberger Ltd", companies_house_id="00000002",
            uk_sic_codes=["23320 - Manufacture of bricks, tiles and construction products, in baked clay",
                          "46730 - Wholesale of wood, construction materials and sanitary equipment"])
    _entity(it_db, "gb-coh:00000003", "Nothing Ltd", companies_house_id="00000003")
    written = industry.sync_uk_sic(snapshot_date="2026-07-01", source_id="s-ch")
    assert written == 2
    rows = sorted(run_sql("SELECT FROM IndustryCode WHERE entity_id = 'gb-coh:00000002'"),
                  key=lambda r: r["position"])
    assert [(r["position"], r["code"], r["nace_code"]) for r in rows] == [(1, "23320", "23.32"), (2, "46730", "46.73")]
    assert rows[0]["source_date"] == "2026-07-01"
    assert rows[0]["source_url"].endswith("/company/00000002")
    assert _nace("gb-coh:00000002") == "23.32"                 # the principal activity
    assert run_sql("SELECT FROM IndustryCode WHERE entity_id = 'gb-coh:00000003'") == []
    # a second run restates, never duplicates
    assert industry.sync_uk_sic(snapshot_date="2026-08-01", source_id="s-ch") == 2
    assert len(run_sql("SELECT FROM IndustryCode WHERE entity_id = 'gb-coh:00000002'")) == 2


def test_the_sync_can_be_scoped(it_db):
    _source(it_db, "s-ch", "Companies House Register")
    for n in ("00000004", "00000005"):
        _entity(it_db, f"gb-coh:{n}", f"Co {n}", companies_house_id=n, uk_sic_codes=["01110 - Growing of cereals"])
    assert industry.sync_uk_sic(entity_ids=["gb-coh:00000004"], source_id="s-ch") == 1
    assert _nace("gb-coh:00000004") == "01.11" and _nace("gb-coh:00000005") is None


def test_the_backfill_fetches_sec_filers_without_a_statement(it_db):
    _source(it_db, "s-ch", "Companies House Register")
    _entity(it_db, "e5", "Tesla, Inc.", sec_cik="0001318605")
    _entity(it_db, "e6", "Already Stated Inc", sec_cik="0000000001")
    sec = industry._companies_house_source_id()  # any Source node will do for the pre-existing row
    industry.record("e6", "us-sic", "2834", "Pharmaceutical Preparations", source_id=sec)
    fetched = []
    res = industry.backfill(fetch=lambda cik: fetched.append(cik) or ("3711", "Motor Vehicles & Passenger Car Bodies"))
    assert fetched == ["0001318605"]                           # the stated filer is not re-fetched
    assert res["sec_filled"] == 1 and _nace("e5") == "29.10"


# ── A merge ───────────────────────────────────────────────────────────────────

def test_a_merge_rekeys_the_statements_and_keeps_the_survivors_own(it_db):
    _entity(it_db, "dead", "Twin Ltd")
    _entity(it_db, "keep", "Twin Limited")
    ch, sec = _source(it_db, "s-ch", "Companies House"), _source(it_db, "s-sec", "SEC EDGAR")
    industry.record("dead", "uk-sic", "23320", "Bricks", source_id=ch)
    industry.record("dead", "us-sic", "3250", "Structural Clay Products", source_id=sec)
    industry.record("keep", "uk-sic", "46730", "Wholesale", source_id=ch)
    moved = industry.migrate("dead", "keep")
    assert moved == 1                                          # the US one; the UK slot was taken
    assert run_sql("SELECT FROM IndustryCode WHERE entity_id = 'dead'") == []
    rows = {r["scheme"]: r for r in run_sql("SELECT FROM IndustryCode WHERE entity_id = 'keep'")}
    assert rows["uk-sic"]["code"] == "46730" and rows["us-sic"]["code"] == "3250"
    assert rows["us-sic"]["key"] == industry.statement_key("keep", "us-sic", 1)
    assert _nace("keep") == "46.73"


# ── The reader ────────────────────────────────────────────────────────────────

def test_the_profile_serves_the_tree_and_the_statements(it_db, it_client):
    _entity(it_db, "e7", "Wienerberger Ltd")
    ch = _source(it_db, "s-ch", "Companies House Register")
    industry.record("e7", "uk-sic", "23320", "Manufacture of bricks, tiles and construction products, in baked clay",
                    source_id=ch, source_url="https://example.com/company/7", source_date="2026-07-01")
    ind = it_client.get("/v1/search/entity/e7/full-profile").json()["industry"]
    assert ind["nace_code"] == "23.32"
    assert [t["code"] for t in ind["tree"]] == ["C", "23", "23.3", "23.32"]
    assert ind["tree"][3]["label"]["en"].startswith("Manufacture of bricks")
    assert ind["tree"][3]["label"]["de"].startswith("Herstellung von Ziegeln")
    s = ind["statements"][0]
    assert (s["scheme"], s["scheme_name"], s["code"], s["source_name"]) == (
        "uk-sic", "UK SIC 2007", "23320", "Companies House Register")
    assert [t["code"] for t in s["tree"]] == ["C", "23", "23.3", "23.32"]


def test_a_company_without_a_sector_has_none(it_db, it_client):
    _entity(it_db, "e8", "Unknown Ltd")
    assert it_client.get("/v1/search/entity/e8/full-profile").json()["industry"] is None
