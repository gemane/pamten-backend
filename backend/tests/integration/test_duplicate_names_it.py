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


def test_confidence_tiers(it_db):
    from app.scraper import maintenance
    # definitive: two "Foo" nodes share a sec_cik
    it_db.run_command("CREATE (:Entity {id:'lei:F1', name:'Foo', name_normalized:'foo', lei_id:'F1', sec_cik:'0001'})")
    it_db.run_command("CREATE (:Entity {id:'lei:F2', name:'Foo Inc', name_normalized:'foo', lei_id:'F2', sec_cik:'0001'})")
    # high: two "Bar" nodes share a registered_address
    it_db.run_command("CREATE (:Entity {id:'lei:B1', name:'Bar', name_normalized:'bar', lei_id:'B1', registered_address:'1 king st'})")
    it_db.run_command("CREATE (:Entity {id:'lei:B2', name:'Bar Ltd', name_normalized:'bar', lei_id:'B2', registered_address:'1 king st'})")
    # low: two "Qux" nodes share only the name (different countries)
    it_db.run_command("CREATE (:Entity {id:'lei:Q1', name:'Qux', name_normalized:'qux', lei_id:'Q1', country:'US'})")
    it_db.run_command("CREATE (:Entity {id:'lei:Q2', name:'Qux', name_normalized:'qux', lei_id:'Q2', country:'DE'})")

    groups = {g["name_normalized"]: g["confidence"]
              for g in maintenance.find_duplicate_entity_names(limit=50)}
    assert groups["foo"] == "definitive"
    assert groups["bar"] == "high"
    assert groups["qux"] == "low"

    # min_confidence filters out the weak ones
    high = maintenance.find_duplicate_entity_names(limit=50, min_confidence="high")
    hnames = {g["name_normalized"] for g in high}
    assert "foo" in hnames and "bar" in hnames and "qux" not in hnames
