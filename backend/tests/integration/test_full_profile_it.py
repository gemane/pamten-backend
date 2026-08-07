"""
Real-ArcadeDB characterisation of /search/entity/{id}/full-profile.

Written BEFORE splitting the single seven-OPTIONAL-MATCH query into per-section
queries, so the refactor has to reproduce the existing output rather than merely
look plausible. Dedupe, suppression, pins, succession and the ownership summary
already have their own integration files; this one pins the overall shape — every
section populated at once — plus the per-section limits the split introduces.

Why the split: the OPTIONAL MATCH chain is a cartesian product. Measured on the
dev database, Microsoft (24 owners x 15 subsidiaries x 33 executives) made
ArcadeDB materialise 11,880 rows so that collect(DISTINCT) could throw away
11,879 of them. It is multiplicative, so it degrades non-linearly as scraper
coverage fills several dimensions on the same company.
"""
import pytest

from app.routers.search import get_full_profile

pytestmark = pytest.mark.integration


def _rich_entity(it_db):
    """One entity wired to every section the profile returns."""
    it_db.run_command("CREATE (e:Entity {id:'ME', name:'Middle Co', type:'company'})")
    # HQ lives on the entity itself now, not on a linked Location node.
    it_db.run_command("MATCH (e:Entity {id:'ME'}) SET e.hq_city = 'Vienna', "
                      "e.hq_country = 'AT', e.hq_address = '1 Ringstrasse, Vienna'")

    for i in range(3):
        it_db.run_command(f"CREATE (o:Entity {{id:'OWN{i}', name:'Owner {i}', type:'company'}})")
        it_db.run_command(
            f"MATCH (o:Entity {{id:'OWN{i}'}}), (e:Entity {{id:'ME'}}) "
            f"CREATE (o)-[:OWNS {{until:null, stake_percent:{10 + i}, source_id:'s'}}]->(e)")

    for i in range(4):
        it_db.run_command(f"CREATE (s:Entity {{id:'SUB{i}', name:'Sub {i}', type:'company'}})")
        it_db.run_command(
            f"MATCH (e:Entity {{id:'ME'}}), (s:Entity {{id:'SUB{i}'}}) "
            f"CREATE (e)-[:OWNS {{until:null, stake_percent:{50 + i}, source_id:'s'}}]->(s)")

    for i in range(2):
        it_db.run_command(f"CREATE (p:Person {{id:'P{i}', full_name:'Person {i}'}})")
        it_db.run_command(
            f"MATCH (p:Person {{id:'P{i}'}}), (e:Entity {{id:'ME'}}) "
            f"CREATE (p)-[:HAS_ROLE {{until:null, role:'CEO', source_id:'s'}}]->(e)")


def test_every_section_is_populated(it_db):
    _rich_entity(it_db)
    profile = get_full_profile("ME")

    assert profile["entity"]["id"] == "ME"
    assert profile["entity"]["hq_city"] == "Vienna"
    assert profile["entity"]["hq_country"] == "AT"
    assert {o["owner"]["id"] for o in profile["owners"]} == {"OWN0", "OWN1", "OWN2"}
    assert {s["entity"]["id"] for s in profile["subsidiaries"]} == {"SUB0", "SUB1", "SUB2", "SUB3"}
    assert {e["person"]["id"] for e in profile["executives"]} == {"P0", "P1"}
    assert profile["dual_listed"] == []
    assert profile["succeeded_by"] == [] and profile["replaces"] == []


def test_relationship_payloads_survive(it_db):
    # The edge properties matter as much as the nodes — a split that returned the
    # right entities with the wrong stakes would still be broken.
    _rich_entity(it_db)
    profile = get_full_profile("ME")

    owner0 = next(o for o in profile["owners"] if o["owner"]["id"] == "OWN0")
    assert owner0["relationship"]["stake_percent"] == 10
    sub3 = next(s for s in profile["subsidiaries"] if s["entity"]["id"] == "SUB3")
    assert sub3["relationship"]["stake_percent"] == 53
    assert profile["executives"][0]["role"]["role"] == "CEO"


def test_ownership_summary_is_computed_from_the_owners(it_db):
    _rich_entity(it_db)
    profile = get_full_profile("ME")
    # 10 + 11 + 12
    assert profile["ownership"]["disclosed_pct"] == pytest.approx(33.0)


def test_self_loop_is_not_reported_as_an_owner(it_db):
    # A owns A — treasury shares or a data error. It would inflate the disclosed
    # percentage and the free float.
    it_db.run_command("CREATE (e:Entity {id:'LOOP', name:'Loop Co', type:'company'})")
    it_db.run_command("MATCH (a:Entity {id:'LOOP'}), (b:Entity {id:'LOOP'}) "
                      "CREATE (a)-[:OWNS {until:null, stake_percent:5, source_id:'s'}]->(b)")

    profile = get_full_profile("LOOP")
    assert profile["owners"] == []
    assert profile["subsidiaries"] == []


def test_cross_holding_is_surfaced(it_db):
    # B is both an owner and a subsidiary of A — a reciprocal holding, kept as a
    # data-quality signal rather than silently dropped.
    it_db.run_command("CREATE (a:Entity {id:'A', name:'A Co', type:'company'})")
    it_db.run_command("CREATE (b:Entity {id:'B', name:'B Co', type:'company'})")
    it_db.run_command("MATCH (a:Entity {id:'A'}), (b:Entity {id:'B'}) "
                      "CREATE (b)-[:OWNS {until:null, stake_percent:30, source_id:'s'}]->(a)")
    it_db.run_command("MATCH (a:Entity {id:'A'}), (b:Entity {id:'B'}) "
                      "CREATE (a)-[:OWNS {until:null, stake_percent:40, source_id:'s'}]->(b)")

    profile = get_full_profile("A")
    assert [c["id"] for c in profile["cross_holdings"]] == ["B"]


def test_closed_relationships_are_excluded(it_db):
    # until != null means historical; the profile shows the current picture.
    it_db.run_command("CREATE (e:Entity {id:'NOW', name:'Now Co', type:'company'})")
    it_db.run_command("CREATE (o:Entity {id:'OLD', name:'Old Owner', type:'company'})")
    it_db.run_command("MATCH (o:Entity {id:'OLD'}), (e:Entity {id:'NOW'}) "
                      "CREATE (o)-[:OWNS {until:'2020-01-01', stake_percent:80, source_id:'s'}]->(e)")

    assert get_full_profile("NOW")["owners"] == []


def test_missing_entity_is_a_404(it_db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        get_full_profile("does-not-exist")
    assert exc.value.status_code == 404


def test_entity_with_no_relationships_returns_empty_sections(it_db):
    it_db.run_command("CREATE (e:Entity {id:'BARE', name:'Bare Co', type:'company'})")
    profile = get_full_profile("BARE")

    assert profile["entity"]["id"] == "BARE"
    for section in ("owners", "subsidiaries", "executives",
                    "dual_listed", "cross_holdings", "succeeded_by", "replaces"):
        assert profile[section] == [], f"{section} should be empty"


# ── Per-section limits ────────────────────────────────────────────────────────
#
# Each section is now its own query with its own LIMIT, so the payload is the sum
# of the sections rather than one unbounded dump. Before the split a single
# profile inlined every subsidiary an entity had — 236 of them, ~197 KB, measured
# on the dev database.

def test_limit_applies_to_each_section_independently(it_db):
    _rich_entity(it_db)          # 3 owners, 4 subsidiaries, 2 executives
    profile = get_full_profile("ME", limit=2)

    assert len(profile["owners"]) == 2
    assert len(profile["subsidiaries"]) == 2
    assert len(profile["executives"]) == 2   # all of them; the section is small
    # Sections are capped separately, so one large section can't squeeze out
    # another the way a single shared budget would.


def test_limit_does_not_truncate_sections_under_it(it_db):
    _rich_entity(it_db)
    profile = get_full_profile("ME", limit=100)
    assert len(profile["owners"]) == 3
    assert len(profile["subsidiaries"]) == 4


def test_default_limit_returns_everything_for_a_normal_entity(it_db):
    _rich_entity(it_db)
    profile = get_full_profile("ME")
    assert len(profile["owners"]) == 3
    assert len(profile["subsidiaries"]) == 4
    assert len(profile["executives"]) == 2


def test_ownership_summary_reflects_the_truncated_owner_set(it_db):
    # Honest arithmetic: if owners were cut, the disclosed percentage is computed
    # from what came back, not from a total the caller can't see.
    _rich_entity(it_db)
    profile = get_full_profile("ME", limit=1)
    assert len(profile["owners"]) == 1
    assert profile["ownership"]["disclosed_pct"] == pytest.approx(
        profile["owners"][0]["relationship"]["stake_percent"])
