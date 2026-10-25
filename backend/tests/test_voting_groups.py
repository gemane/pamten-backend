"""The heal that moves a 13D filing group from an Entity of type
`voting_group` to its own vertex type (app.voting_groups, 2026-10-26)."""
from unittest.mock import patch

from app import voting_groups

GROUP = {"id": "g1", "name": "Voting group · 2 parties", "name_normalized": "voting group abi",
         "search_text": "Voting group · 2 parties ABI", "description": "Parties acting together…",
         "type": "voting_group", "source_id": "sec", "created_at": "2026-01-01T00:00:00+00:00",
         "last_scraped_at": "2026-02-01T00:00:00+00:00", "member_keys": ["1|brc", "|lemann"]}
MEMBERS = [{"mid": "brc", "mlabel": "Entity", "source_id": "sec", "last_scraped_at": "2026-02-01T00:00:00+00:00"},
           {"mid": "lemann", "mlabel": "Person", "source_id": "sec", "last_scraped_at": None}]
HOLDING = [{"tid": "abi", "voting_power_pct": 52.3, "source_id": "sec", "stake_percent": None}]


def _patch(monkeypatch, groups):
    cmds, carried = [], []
    monkeypatch.setattr(voting_groups, "run_sql", lambda sql, params=None, **k: groups if "FROM Entity" in sql else [])
    monkeypatch.setattr(voting_groups, "run_query",
                        lambda cy, params=None: MEMBERS if "RELATED_TO" in cy else HOLDING if "OWNS" in cy else [])
    monkeypatch.setattr(voting_groups, "run_command", lambda cy, params=None: cmds.append((cy, params)))
    return cmds, carried


def test_a_dry_run_counts_and_names_without_writing(monkeypatch):
    cmds, _ = _patch(monkeypatch, [GROUP])
    with patch("app.scraper.maintenance._carry_edge") as carry:
        res = voting_groups.heal_voting_group_type(apply=False)
    assert (res["groups"], res["members"], res["holdings"], res["applied"]) == (1, 2, 1, False)
    assert res["names"] == ["Voting group · 2 parties (2 parties, 1 holdings)"]
    assert cmds == [] and not carry.called


def test_apply_moves_the_node_its_memberships_and_its_holding(monkeypatch):
    cmds, _ = _patch(monkeypatch, [GROUP])
    with patch("app.scraper.maintenance._carry_edge", return_value=1) as carry:
        res = voting_groups.heal_voting_group_type(apply=True)
    assert res["groups"] == 1 and res["applied"] is True
    create, *members, delete = cmds
    assert create[0].startswith("CREATE (g:VotingGroup {") and create[1] == GROUP
    assert "type: $type" in create[0] and "member_keys: $member_keys" in create[0]
    # each party keeps its label and its provenance; the edge is MEMBER_OF
    assert [m[0].split("MATCH ")[1].split(" {")[0] for m in members] == ["(m:Entity", "(m:Person"]
    assert all("CREATE (m)-[:MEMBER_OF {source_id: $sid, last_scraped_at: $t}]->(g)" in m[0] for m in members)
    assert members[0][1] == {"mid": "brc", "gid": "g1", "sid": "sec", "t": "2026-02-01T00:00:00+00:00"}
    # the holding is recreated from the new vertex with the whole OWNS schema
    carry.assert_called_once()
    rel, props, src, tgt, params, edge = carry.call_args.args
    assert (rel, src, tgt, params) == ("OWNS", "(a:VotingGroup {id: $k})", "(t:Entity {id: $tid})", {"k": "g1", "tid": "abi"})
    assert edge["voting_power_pct"] == 52.3
    # the old node goes last, with its edges
    assert delete == ("MATCH (g:Entity {id: $gid}) DETACH DELETE g", {"gid": "g1"})


def test_nothing_to_move_is_a_quiet_run(monkeypatch):
    cmds, _ = _patch(monkeypatch, [])
    res = voting_groups.heal_voting_group_type(apply=True)
    assert res["groups"] == 0 and cmds == []
