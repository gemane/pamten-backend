"""
Real-ArcadeDB test for cross-source duplicate DETECTION: the same company under
different identifiers (e.g. two GLEIF LEIs) shares a name_normalized, which the
id-based dedup can't see. Counting/listing is sharded server-side so it doesn't
trip the query heap on a full import.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    # BlackRock under two LEIs (a true duplicate)
    it_db.run_command("CREATE (:Entity {id:'lei:A', name:'BlackRock, Inc.', "
                      "name_normalized:'blackrock', lei_id:'A', country:'US'})")
    it_db.run_command("CREATE (:Entity {id:'lei:B', name:'BLACKROCK, INC.', "
                      "name_normalized:'blackrock', lei_id:'B', country:'US'})")
    # Acme under two LEIs (another group)
    it_db.run_command("CREATE (:Entity {id:'lei:C', name:'Acme Ltd', name_normalized:'acme', lei_id:'C'})")
    it_db.run_command("CREATE (:Entity {id:'lei:D', name:'Acme', name_normalized:'acme', lei_id:'D'})")
    # a unique company (not a duplicate)
    it_db.run_command("CREATE (:Entity {id:'lei:E', name:'Unique Co', name_normalized:'unique co', lei_id:'E'})")


def test_count_duplicate_entity_names(it_db):
    from app.scraper import maintenance
    _seed(it_db)
    c = maintenance.count_duplicate_entity_names()
    assert c["duplicate_name_groups"] == 2      # blackrock, acme
    assert c["redundant_nodes"] == 2            # one extra per group


def test_find_duplicate_entity_names_lists_members(it_db):
    from app.scraper import maintenance
    _seed(it_db)
    groups = maintenance.find_duplicate_entity_names(limit=10)
    names = {g["name_normalized"] for g in groups}
    assert names == {"blackrock", "acme"}
    br = next(g for g in groups if g["name_normalized"] == "blackrock")
    assert br["count"] == 2
    assert {m["lei_id"] for m in br["members"]} == {"A", "B"}   # both LEIs listed for review
