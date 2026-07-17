"""
Real-ArcadeDB integration test for /sources/person: a person's provenance comes
from the source_id stamped on their OWNS / HAS_ROLE edges (joined to the Source
node), same shape as the entity endpoint.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_person_sources_from_owns_and_role_edges(it_db):
    from app.routers.sources import get_sources_for_person

    it_db.run_command("CREATE (:Source {id:'src-sec', name:'SEC EDGAR', type:'scraper', "
                      "credibility_score:97, url:'https://www.sec.gov'})")
    it_db.run_command("CREATE (:Person {id:'fink', full_name:'Larry Fink'})")
    it_db.run_command("CREATE (:Entity {id:'blk', name:'BlackRock', type:'company'})")
    it_db.run_command("MATCH (p:Person {id:'fink'}), (e:Entity {id:'blk'}) "
                      "CREATE (p)-[:OWNS {source_id:'src-sec', source_url:'https://www.sec.gov/form4', source_date:'2024-01-01'}]->(e)")
    it_db.run_command("MATCH (p:Person {id:'fink'}), (e:Entity {id:'blk'}) "
                      "CREATE (p)-[:HAS_ROLE {role:'CEO', source_id:'src-sec', source_url:'https://www.sec.gov/role'}]->(e)")

    out = get_sources_for_person("fink")
    assert any(s["name"] == "SEC EDGAR" for s in out)
    urls = {s["url"] for s in out}
    assert "https://www.sec.gov/form4" in urls   # the specific ownership record URL
    assert "https://www.sec.gov/role" in urls    # the role record URL


def test_person_sources_empty_when_none(it_db):
    from app.routers.sources import get_sources_for_person
    it_db.run_command("CREATE (:Person {id:'solo', full_name:'Solo'})")
    assert get_sources_for_person("solo") == []
