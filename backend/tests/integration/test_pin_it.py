"""
Real-ArcadeDB test for Phase-B pin: pinning a corrected stake %/ownership type on
an OWNS edge overrides the scraped value at read time — and the correction stands
even after a re-scrape overwrites the edge (read-time overlay, edge not mutated).
Un-pin reverts to the scraped value.
"""
import pytest

from app.routers import flags, search
from app.models.flag import PinRequest

pytestmark = pytest.mark.integration


def _owner_stake(prof):
    return next((o["relationship"]["stake_percent"] for o in prof["owners"]
                 if o["owner"]["id"] == "O"), None)


def test_pin_overrides_stake_and_survives_rescrape(it_db):
    it_db.run_command("CREATE (e:Entity {id:'T', name:'Target', type:'company'})")
    it_db.run_command("CREATE (e:Entity {id:'O', name:'Owner', type:'company'})")
    it_db.run_command("MATCH (o:Entity {id:'O'}), (t:Entity {id:'T'}) "
                      "CREATE (o)-[:OWNS {until:null, stake_percent:10.0, ownership_type:'minority'}]->(t)")
    it_db.run_command(
        "CREATE (f:Flag {id:'flg', target_kind:'owns', from_id:'O', to_id:'T', role:'', "
        "category:'wrong-percent', status:'open', reporter_kind:'anon', reporter_id:'', "
        "reporter_fp:'x', note:'', node_id:'', created_at:'2026', updated_at:'2026'})")

    assert _owner_stake(search.get_full_profile("T")) == 10.0     # scraped value

    res = flags.pin_flag("flg", PinRequest(stake_percent=51.0, ownership_type="majority"), _=None)
    assert res["status"] == "pinned"

    # Read now shows the pinned correction, and the flag is resolved.
    assert _owner_stake(search.get_full_profile("T")) == 51.0
    st = it_db.run_command("MATCH (f:Flag {id:'flg'}) RETURN f.status AS s")
    assert st[0]["s"] == "resolved"

    # A re-scrape overwrites the edge's stake — the pin still wins at read time.
    it_db.run_command("MATCH (:Entity {id:'O'})-[r:OWNS]->(:Entity {id:'T'}) SET r.stake_percent = 20.0")
    assert _owner_stake(search.get_full_profile("T")) == 51.0

    # Un-pin → reads fall back to the (now re-scraped) value.
    pins = flags.list_pins(_=None)
    assert len(pins) == 1
    flags.remove_pin(pins[0]["id"], _=None)
    assert _owner_stake(search.get_full_profile("T")) == 20.0
