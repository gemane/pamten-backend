"""Real-ArcadeDB: a merge or a rename takes the stored references with it.

Flags, suppressions, pins and scrape runs carry node ids as properties (graph
edges do not). Until 2026-10-24 the two merge paths left them on the deleted
loser — a moderator's pin on a company dangled the moment the dedup folded
that company into its GLEIF twin. Each path is driven end to end here.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

from app.database import db

pytestmark = pytest.mark.integration


def _references_to(it_db, node_id):
    """{table.column: count} of every stored reference to `node_id`."""
    from app.merged_ids import REFERENCE_COLUMNS
    out = {}
    for table, cols in REFERENCE_COLUMNS:
        for col in cols:
            n = it_db.run_sql(f"SELECT count(*) AS n FROM {table} WHERE {col} = '{node_id}'")[0]["n"]
            if n:
                out[f"{table}.{col}"] = n
    return out


def _moderation_on(it_db, node_id):
    it_db.run_command(
        f"CREATE (:Flag {{id:'f-{node_id}', target_kind:'node', node_id:'{node_id}', from_id:'', to_id:'', "
        "role:'', status:'open', category:'wrong_data', note:'', created_at:'2026-01-01', updated_at:'2026-01-01'})")
    it_db.run_command(
        f"CREATE (:Flag {{id:'fe-{node_id}', target_kind:'owns', from_id:'{node_id}', to_id:'other', "
        "role:'', node_id:'', status:'open', category:'wrong_data', note:'', created_at:'2026-01-01', updated_at:'2026-01-01'})")
    it_db.run_command(
        f"CREATE (:Pin {{id:'p-{node_id}', from_id:'owner', to_id:'{node_id}', stake_percent:51.0, "
        "ownership_type:'majority', flag_id:'', created_at:'2026-01-01'})")
    it_db.run_command(
        f"CREATE (:Suppression {{id:'s-{node_id}', target_kind:'node', node_id:'{node_id}', from_id:'', to_id:'', "
        "role:'', flag_id:'', created_at:'2026-01-01'})")
    it_db.run_command(
        f"CREATE (:ScrapeRun {{id:'r-{node_id}', source:'wikidata', target:'x', status:'ok', "
        f"started_at:'2026-01-01', finished_at:'2026-01-01', total:1, error:'', entity_id:'{node_id}'}})")


def test_an_entity_merge_moves_the_moderation_records_and_runs(it_db):
    from app.scraper.maintenance import deduplicate_entities_for

    it_db.run_command(
        "CREATE (:Entity {id:'ent-a', name:'Acme', name_normalized:'acme', type:'company', "
        "lei_id:'AAAA1111BBBB2222CCCC', name_credibility:92})")
    it_db.run_command(
        "CREATE (:Entity {id:'ent-b', name:'Acme', name_normalized:'acme', type:'company', "
        "lei_id:'AAAA1111BBBB2222CCCC', name_credibility:80})")
    _moderation_on(it_db, "ent-b")
    assert _references_to(it_db, "ent-b") == {"Flag.node_id": 1, "Flag.from_id": 1, "Pin.to_id": 1,
                                              "Suppression.node_id": 1, "ScrapeRun.entity_id": 1}

    assert deduplicate_entities_for(["ent-a", "ent-b"], apply=True)["entities_merged"] == 1

    assert _references_to(it_db, "ent-b") == {}
    assert _references_to(it_db, "ent-a") == {"Flag.node_id": 1, "Flag.from_id": 1, "Pin.to_id": 1,
                                              "Suppression.node_id": 1, "ScrapeRun.entity_id": 1}


def test_a_person_merge_moves_them_too(it_db):
    from app.routers.persons import merge_person_records

    for pid in ("keep-1", "dup-1"):
        it_db.run_command(f"CREATE (:Person {{id:'{pid}', first_name:'Ada', last_name:'L', full_name:'Ada L'}})")
    _moderation_on(it_db, "dup-1")

    merge_person_records(keep="keep-1", dup="dup-1")

    assert _references_to(it_db, "dup-1") == {}
    assert _references_to(it_db, "keep-1")["Flag.node_id"] == 1
    assert _references_to(it_db, "keep-1")["ScrapeRun.entity_id"] == 1


def test_a_rename_moves_them_through_the_same_path(it_db):
    from app.merged_ids import rename_node_id

    it_db.run_command("CREATE (:Entity {id:'old-id', name:'Acme', name_normalized:'acme'})")
    _moderation_on(it_db, "old-id")
    with db.get_session() as session:
        assert rename_node_id(session, "Entity", "old-id", "new-id") is True
    assert _references_to(it_db, "old-id") == {}
    assert _references_to(it_db, "new-id")["Suppression.node_id"] == 1


def test_the_heal_moves_what_an_old_merge_left_behind(it_db):
    from app.merged_ids import heal_merged_references, record_merge_sql
    from app.merged_ids import repoint_references

    # a merge recorded the old way: forwarding row, references untouched
    it_db.run_command("CREATE (:Entity {id:'survivor', name:'S', name_normalized:'s'})")
    _moderation_on(it_db, "gone")
    with db.get_session() as session:
        session.run("MERGE (m:MergedId {old_id:'gone'}) SET m.new_id='survivor', m.kind='Entity', m.at='2026-01-01', m.id='x'")
    assert _references_to(it_db, "gone")

    dry = heal_merged_references(apply=False)
    assert dry["redirects"] == 1 and dry["stale"] == 5 and dry["moved"] == 0
    assert _references_to(it_db, "gone")                      # a dry run moved nothing

    res = heal_merged_references(apply=True)
    assert res["moved"] == 5
    assert _references_to(it_db, "gone") == {}
    assert sum(_references_to(it_db, "survivor").values()) == 5
    assert heal_merged_references(apply=True)["stale"] == 0   # idempotent
    assert repoint_references("gone", "survivor") == 0        # and so is the primitive
    record_merge_sql("gone", "survivor")                       # recording it again is harmless
