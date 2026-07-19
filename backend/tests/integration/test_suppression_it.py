"""
Real-ArcadeDB test for Phase-B suppression: suppressing an edge flag deletes the
edge, resolves the flag, and hides the edge from reads — and it stays hidden even
after a re-scrape recreates it (read-time enforcement). Un-suppress restores it.
"""
import pytest

from app.routers import flags, search

pytestmark = pytest.mark.integration


def _owns(it_db):
    it_db.run_command("MATCH (o:Entity {id:'O'}), (t:Entity {id:'T'}) "
                      "CREATE (o)-[:OWNS {until:null, source_id:'s'}]->(t)")


def test_suppress_hides_edge_and_survives_rescrape(it_db):
    it_db.run_command("CREATE (e:Entity {id:'T', name:'Target', type:'company'})")
    it_db.run_command("CREATE (e:Entity {id:'O', name:'Owner Co', type:'company'})")
    _owns(it_db)
    it_db.run_command(
        "CREATE (f:Flag {id:'flg', target_kind:'owns', from_id:'O', to_id:'T', role:'', "
        "category:'wrong-owner', status:'open', reporter_kind:'anon', reporter_id:'', "
        "reporter_fp:'x', note:'', node_id:'', created_at:'2026', updated_at:'2026'})")

    # Owner shows before suppression.
    assert any(o["owner"]["id"] == "O" for o in search.get_full_profile("T")["owners"])

    res = flags.suppress_flag("flg", _=None)
    assert res["status"] == "suppressed"
    sup_id = res["id"]

    # The edge is deleted and the flag is resolved.
    n = it_db.run_command("MATCH (:Entity {id:'O'})-[r:OWNS]->(:Entity {id:'T'}) RETURN count(r) AS n")
    assert n[0]["n"] == 0
    st = it_db.run_command("MATCH (f:Flag {id:'flg'}) RETURN f.status AS s")
    assert st[0]["s"] == "resolved"

    # Hidden from the profile.
    assert search.get_full_profile("T")["owners"] == []

    # A re-scrape recreates the edge — still hidden (read-time filter).
    _owns(it_db)
    assert search.get_full_profile("T")["owners"] == []
    # ...and from /relationships/owners too.
    from app.routers.relationships import get_owners
    assert get_owners("T") == []

    # Un-suppress → the owner reappears.
    flags.remove_suppression(sup_id, _=None)
    assert any(o["owner"]["id"] == "O" for o in search.get_full_profile("T")["owners"])


def test_suppress_is_idempotent_by_natural_key(it_db):
    it_db.run_command("CREATE (e:Entity {id:'E2', name:'E2', type:'company'})")
    it_db.run_command("CREATE (e:Entity {id:'O2', name:'O2', type:'company'})")
    it_db.run_command("MATCH (o:Entity {id:'O2'}), (t:Entity {id:'E2'}) "
                      "CREATE (o)-[:OWNS {until:null}]->(t)")
    for fid in ("g1", "g2"):
        it_db.run_command(
            f"CREATE (f:Flag {{id:'{fid}', target_kind:'owns', from_id:'O2', to_id:'E2', role:'', "
            f"category:'wrong-owner', status:'open', reporter_kind:'anon', reporter_id:'', "
            f"reporter_fp:'y', note:'', node_id:'', created_at:'2026', updated_at:'2026'}})")

    flags.suppress_flag("g1", _=None)
    flags.suppress_flag("g2", _=None)   # same edge → no second Suppression row

    rows = it_db.run_command("MATCH (s:Suppression) WHERE s.from_id='O2' AND s.to_id='E2' RETURN count(s) AS n")
    assert rows[0]["n"] == 1
