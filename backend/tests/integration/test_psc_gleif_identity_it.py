"""A corporate PSC and its GLEIF record become one node, against a real
ArcadeDB (findings item 25): Berkshire Hathaway as Companies House records
it — the owner of Berkshire Hathaway International Insurance — and GLEIF's
BERKSHIRE HATHAWAY INC share no hard id, and the registers' words join them
(name with its form, Delaware, 3555 Farnam Street)."""
import pytest

pytestmark = pytest.mark.integration

PSC_ID, LEI = "chpsc:03230337:VkVz5Am9-v9NnalMlijnYSGP0jE", "5493000C01ZX7D35SD85"


def _seed(it_db, psc_street="3555 Farnam Street"):
    it_db.run_command(
        "CREATE (e:Entity {id: $id, name: 'BERKSHIRE HATHAWAY INC', name_normalized: 'berkshire hathaway', "
        "lei_id: $lei, country: 'US', jurisdiction_code: 'US-DE', source_id: 'gleif', name_credibility: 92, "
        "reg_street: 'C/O THE CORPORATION TRUST COMPANY, 1209 ORANGE ST', reg_city: 'WILMINGTON', reg_postcode: '19801', "
        "hq_street: '3555 Farnam Street, Suite 1440', hq_city: 'Omaha', hq_postcode: '68131', type: 'company'})",
        {"id": f"lei:{LEI}", "lei": LEI})
    it_db.run_command(
        "CREATE (e:Entity {id: $id, name: 'Berkshire Hathaway Inc.', name_normalized: 'berkshire hathaway', "
        "country: 'US', jurisdiction_code: 'US-DE', source_id: 'uk-psc', name_credibility: 80, "
        "registration_number: '47-0813844', reg_street: $street, reg_city: 'Omaha', reg_postcode: '68131', "
        "type: 'company'})", {"id": PSC_ID, "street": psc_street})
    it_db.run_command("CREATE (e:Entity {id: 'gb-coh:03230337', name: 'BERKSHIRE HATHAWAY INTERNATIONAL INSURANCE LIMITED', "
                      "name_normalized: 'berkshire hathaway international insurance', companies_house_id: '03230337'})")
    it_db.run_command("MATCH (a:Entity {id: $a}), (b:Entity {id: 'gb-coh:03230337'}) "
                      "CREATE (a)-[:OWNS {source_id: 'uk-psc', stake_percent: 75.0}]->(b)", {"a": PSC_ID})


def _berkshires(it_db):
    return [(r["id"], r["lei"], r["why"]) for r in it_db.run_command(
        "MATCH (e:Entity) WHERE e.name_normalized = 'berkshire hathaway' "
        "RETURN e.id AS id, e.lei_id AS lei, e.lei_source AS why ORDER BY e.id")]


def _owner_of_bhiil(it_db):
    return [r["id"] for r in it_db.run_command(
        "MATCH (a:Entity)-[:OWNS]->(:Entity {id: 'gb-coh:03230337'}) RETURN a.id AS id")]


def test_the_full_dedup_makes_one_node(it_db):
    from app.scraper import maintenance
    _seed(it_db)
    maintenance.deduplicate_entities(limit=None)
    nodes = _berkshires(it_db)
    assert nodes == [(f"lei:{LEI}", LEI, None)] or (len(nodes) == 1 and nodes[0][:2] == (f"lei:{LEI}", LEI))
    assert _owner_of_bhiil(it_db) == [f"lei:{LEI}"], "the PSC's holding moved onto the one node"


def test_the_scoped_dedup_makes_one_node(it_db):
    from app.scraper import maintenance
    _seed(it_db)
    maintenance.deduplicate_entities_for([PSC_ID])
    nodes = _berkshires(it_db)
    assert len(nodes) == 1 and nodes[0][:2] == (f"lei:{LEI}", LEI)
    assert _owner_of_bhiil(it_db) == [f"lei:{LEI}"]


def test_another_building_stays_apart(it_db):
    from app.scraper import maintenance
    _seed(it_db, psc_street="1 Main Street")
    maintenance.deduplicate_entities(limit=None)
    maintenance.deduplicate_entities_for([PSC_ID])
    assert [n[0] for n in _berkshires(it_db)] == [PSC_ID, f"lei:{LEI}"]
    assert _berkshires(it_db)[0][1] is None, "no LEI stamped on a refusal"
