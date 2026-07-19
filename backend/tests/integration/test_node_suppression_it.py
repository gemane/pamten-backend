"""
Real-ArcadeDB test for node suppression: suppressing an Entity flag hides the node
everywhere at read time — its own profile 404s, it drops out of search, and it's
filtered from another entity's owners — without deleting it, so un-suppress
restores it.
"""
import pytest

from fastapi import HTTPException

from app.routers import flags, search

pytestmark = pytest.mark.integration


def test_node_suppression_hides_everywhere_and_reverts(it_db):
    # BadCo owns RealCo; a flag says BadCo isn't a real entity.
    it_db.run_command("CREATE (e:Entity {id:'REAL', name:'RealCo', type:'company'})")
    it_db.run_command("CREATE (e:Entity {id:'BAD', name:'BadCo Ltd', type:'company'})")
    it_db.run_command("MATCH (b:Entity {id:'BAD'}), (r:Entity {id:'REAL'}) "
                      "CREATE (b)-[:OWNS {until:null}]->(r)")
    it_db.run_command(
        "CREATE (f:Flag {id:'flg', target_kind:'entity', node_id:'BAD', from_id:'', to_id:'', "
        "role:'', category:'not-real', status:'open', reporter_kind:'anon', reporter_id:'', "
        "reporter_fp:'x', note:'', created_at:'2026', updated_at:'2026'})")

    # Before: BadCo is a visible owner of RealCo and has its own profile.
    assert any(o["owner"]["id"] == "BAD" for o in search.get_full_profile("REAL")["owners"])
    assert search.get_full_profile("BAD")["entity"]["id"] == "BAD"

    res = flags.suppress_flag("flg", _=None)
    assert res["status"] == "suppressed"
    sup_id = res["id"]

    # Node still exists in the graph (not deleted)...
    n = it_db.run_command("MATCH (e:Entity {id:'BAD'}) RETURN count(e) AS n")
    assert n[0]["n"] == 1
    # ...but it's hidden: gone from RealCo's owners, from search, and its own
    # profile 404s.
    assert search.get_full_profile("REAL")["owners"] == []
    assert not any(r["node"]["id"] == "BAD" for r in search.search("BadCo", country=None))
    with pytest.raises(HTTPException) as exc:
        search.get_full_profile("BAD")
    assert exc.value.status_code == 404

    # Un-suppress → BadCo reappears everywhere.
    flags.remove_suppression(sup_id, _=None)
    assert any(o["owner"]["id"] == "BAD" for o in search.get_full_profile("REAL")["owners"])
    assert search.get_full_profile("BAD")["entity"]["id"] == "BAD"
