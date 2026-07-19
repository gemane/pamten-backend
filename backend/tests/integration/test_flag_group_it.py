"""
Real-ArcadeDB test for the aggregated moderation queue: many reports of the same
target+category collapse to one group row, and resolving via one action (suppress)
cascades to every open flag on that target.
"""
import pytest

from app.routers import flags

pytestmark = pytest.mark.integration


def _flag(fid, cat, fp):
    return (f"CREATE (f:Flag {{id:'{fid}', target_kind:'owns', from_id:'A', to_id:'B', "
            f"role:'', node_id:'', category:'{cat}', status:'open', reporter_kind:'anon', "
            f"reporter_id:'', reporter_fp:'{fp}', note:'', created_at:'2026-01', updated_at:'2026-01'}})")


def _open_groups():
    return flags.list_flags(status="open", target_kind=None, category=None, group=True, limit=1000, _=None)


def test_group_collapses_and_suppress_cascades(it_db):
    it_db.run_command("CREATE (e:Entity {id:'A', name:'A'})")
    it_db.run_command("CREATE (e:Entity {id:'B', name:'B'})")
    it_db.run_command("MATCH (a:Entity {id:'A'}), (b:Entity {id:'B'}) CREATE (a)-[:OWNS {until:null}]->(b)")
    # 3 reports on the same edge: 2 wrong-percent + 1 wrong-owner (distinct reporters).
    for fid, cat, fp in [("g0", "wrong-percent", "p1"), ("g1", "wrong-percent", "p2"),
                         ("g2", "wrong-owner", "p3")]:
        it_db.run_command(_flag(fid, cat, fp))

    groups = _open_groups()
    edge = [g for g in groups if g["from_id"] == "A" and g["to_id"] == "B"]
    assert len(edge) == 2                                   # two categories → two groups
    wp = next(g for g in edge if g["category"] == "wrong-percent")
    assert wp["count"] == 2 and set(wp["flag_ids"]) == {"g0", "g1"}

    # Suppress via ONE flag → every open flag on the edge resolves (both categories).
    flags.suppress_flag("g0", _=None)
    assert not any(g["from_id"] == "A" and g["to_id"] == "B" for g in _open_groups())
    n = it_db.run_command("MATCH (f:Flag) WHERE f.from_id='A' AND f.to_id='B' AND f.status='resolved' RETURN count(f) AS n")
    assert n[0]["n"] == 3
