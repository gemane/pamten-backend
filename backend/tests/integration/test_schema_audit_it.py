"""Real-ArcadeDB: the ledger of declared properties against what the rows carry
(`schema.undeclared_properties`, `manage.py schema-audit`). ArcadeDB accepts a
property nobody declared, so the audit is how a typo'd writer is caught.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_a_fresh_database_written_by_the_writers_has_no_undeclared_property(it_db):
    from app.db.schema import undeclared_properties
    from app.scraper import runner
    person = runner._upsert_person_by_name("Tim Cook", source_id="sec-edgar")
    entity = runner._upsert_entity_by_name(name="Apple Inc.", entity_type="company", source_id="sec-edgar")
    runner._upsert_role(person, entity, "CEO", "sec-edgar", since="2011-08-24")
    assert undeclared_properties() == {}


def test_a_typo_d_property_is_named_by_type(it_db):
    from app.db.schema import undeclared_properties
    it_db.run_command("CREATE (:Entity {id:'a', name:'A', name_normalized:'a', countrry:'US'})")
    it_db.run_command("MATCH (a:Entity {id:'a'}) CREATE (a)-[:OWNS {stake_percent: 10, stakke: 10, until:null}]->(a)")
    drift = {"Entity": {"countrry": {"rows": 1, "null_only": False}},
             "OWNS": {"stakke": {"rows": 1, "null_only": False}}}
    assert undeclared_properties() == drift
    # the sample is per type: a tiny one still reads the one row
    assert undeclared_properties(sample=1) == drift
    # a key every row holds without a value — the leftover of a renamed field.
    # SQL `SET x = null` keeps the key with a null value; Cypher's drops the key.
    it_db.run_sql("UPDATE Entity SET founded_date = null WHERE id = 'a'")
    assert undeclared_properties()["Entity"].get("founded_date") == {"rows": 1, "null_only": True}
