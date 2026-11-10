"""The /rebuild kit's snapshot against a real ArcadeDB (findings item 51): the
OWNS rows it writes carry each edge's filing type, read by the Cypher it runs
on the dev database, so the verify's diff can tell a 13F sample holder that
came and went from a lost edge."""
import importlib.util
import pathlib

import pytest

from app.db.arcadedb import run_query, run_sql

pytestmark = pytest.mark.integration

KIT = pathlib.Path(__file__).resolve().parents[3] / ".claude" / "skills" / "rebuild" / "kit"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BRK, DIXON, BLK = "lei:5493000C01ZX7D35SD85", "sec:0001909993", "sec:0001364742"


def test_the_snapshot_reads_every_owns_edge_with_its_filing_type(it_db):
    it_db.run_command(f"CREATE (e:Entity {{id:'{BRK}', name:'BERKSHIRE HATHAWAY INC', type:'company', "
                      "name_normalized:'berkshire hathaway', lei_id:'5493000C01ZX7D35SD85', sec_cik:'0001067983'})")
    it_db.run_command(f"CREATE (e:Entity {{id:'{DIXON}', name:'DIXON FNANCIAL SERVICES, INC.', type:'company', "
                      "name_normalized:'dixon fnancial services', sec_cik:'0001909993', source_id:'sec-edgar'})")
    it_db.run_command(f"CREATE (e:Entity {{id:'{BLK}', name:'BlackRock Finance, Inc.', type:'company', "
                      "name_normalized:'blackrock finance', sec_cik:'0001364742', source_id:'sec-edgar'})")
    it_db.run_command(f"MATCH (a:Entity {{id:'{DIXON}'}}), (b:Entity {{id:'{BRK}'}}) "
                      "CREATE (a)-[:OWNS {source_id:'sec-edgar', filing_type:'13F', until:null}]->(b)")
    it_db.run_command(f"MATCH (a:Entity {{id:'{BLK}'}}), (b:Entity {{id:'{BRK}'}}) "
                      "CREATE (a)-[:OWNS {source_id:'sec-edgar', filing_type:'13G/A', until:'2026-03-13'}]->(b)")
    snapshot, diff = _load("snapshot"), _load("diff_snapshots")
    snap = snapshot.take(run_sql, run_query)
    assert snap["owns_edges"] == [[BLK, BRK, "sec-edgar", "2026-03-13", "13G/A"],
                                  [DIXON, BRK, "sec-edgar", None, "13F"]]
    assert snap["owns_by_filing"] == {"13F": 1, "13G/A": 1}
    assert [e[0] for e in snap["entities"]] == sorted([BRK, BLK, DIXON])
    # the diff reads what the snapshot wrote: Dixon gone with its node is sample churn
    later = {**snap, "entities": [e for e in snap["entities"] if e[0] != DIXON],
             "owns_edges": [e for e in snap["owns_edges"] if e[0] != DIXON]}
    c = diff.classify(snap, later)
    assert sum(c["churn_gone"].values()) == 1 and not c["gone"]


def test_the_snapshot_lists_every_succession_and_the_lei_list_names_every_lei_node(it_db, capsys):
    """Findings item 89: the SUCCEEDED_BY edges go into the snapshot as a list
    (the 11-08b verify had a count alone), the diff tells one gone and one new
    by name, and `lei_ids` writes the LEIs of every lei: node for the kit's
    step-7 succession pass."""
    old, new = "lei:529900NBET57WDRXAR22", "lei:529900G1S6D7T5ZIX198"
    it_db.run_command(f"CREATE (e:Entity {{id:'{old}', name:'Random House LLC', type:'company', "
                      "name_normalized:'random house', lei_id:'529900NBET57WDRXAR22', active:false})")
    it_db.run_command(f"CREATE (e:Entity {{id:'{new}', name:'Penguin Random House LLC', type:'company', "
                      "name_normalized:'penguin random house', lei_id:'529900G1S6D7T5ZIX198'})")
    it_db.run_command(f"CREATE (e:Entity {{id:'{DIXON}', name:'DIXON FNANCIAL SERVICES, INC.', type:'company', "
                      "name_normalized:'dixon fnancial services', sec_cik:'0001909993'})")
    it_db.run_command(f"MATCH (a:Entity {{id:'{old}'}}), (b:Entity {{id:'{new}'}}) "
                      "CREATE (a)-[:SUCCEEDED_BY {source_id:'gleif'}]->(b)")
    snapshot, diff, lei_ids = _load("snapshot"), _load("diff_snapshots"), _load("lei_ids")
    snap = snapshot.take(run_sql, run_query)
    assert snap["succeeded_by"] == [[old, new, "gleif"]]
    assert lei_ids.ids(run_sql) == ["529900G1S6D7T5ZIX198", "529900NBET57WDRXAR22"]   # the sec: node is not an LEI
    diff._print_succession(snap, {**snap, "succeeded_by": []})
    out = capsys.readouterr().out
    assert "succession (SUCCEEDED_BY): 1 -> 0; gone 1, new 0" in out
    assert f"- ('{old}', '{new}') (Random House LLC -> Penguin Random House LLC) 1" in out


def test_the_two_checks_read_live_drawn_edges(it_db):
    """Findings item 97: over 100 % and the cycles from the Cypher the snapshot
    runs — an ended edge and a shortcut edge count in neither."""
    for i, name in (("lei:S", "SIRIUS XM"), ("sec:A", "LIBERTY A"), ("sec:B", "LIBERTY B"),
                    ("sec:T", "Texaco Inc"), ("sec:C", "CHEVRON CORP"), ("sec:X", "OLD HOLDER")):
        it_db.run_command(f"CREATE (e:Entity {{id:'{i}', name:'{name}', type:'company', name_normalized:'{name.lower()}'}})")
    def edge(a, b, extra):
        it_db.run_command(f"MATCH (a:Entity {{id:'{a}'}}), (b:Entity {{id:'{b}'}}) "
                          f"CREATE (a)-[:OWNS {{source_id:'sec-edgar', filing_type:'13D/A', {extra}}}]->(b)")
    edge("sec:A", "lei:S", "stake_percent:49.5, stale:true")
    edge("sec:B", "lei:S", "stake_percent:40.15")
    edge("sec:X", "lei:S", "stake_percent:30.0, until:'2020-01-01'")      # ended: not summed
    edge("lei:S", "lei:S", "stake_percent:100.0")                         # self-loop: a cycle, not summed
    edge("sec:T", "sec:C", "stake_percent:16.5")
    edge("sec:C", "sec:T", "stake_percent:100.0")
    edge("sec:A", "sec:B", "stake_percent:20.0, shortcut:true")           # a shortcut: neither
    edge("sec:B", "sec:A", "stake_percent:20.0")
    snapshot = _load("snapshot")
    snap = snapshot.take(run_sql, run_query)
    assert snap["over_100"] == []                                         # 49.5 + 40.15: the ended 30 % is left out
    assert snap["cycles"] == [["lei:S"], ["sec:C", "sec:T"]]              # A<->B only through the shortcut
    edge("sec:X", "lei:S", "stake_percent:30.0")
    snap = snapshot.take(run_sql, run_query)
    assert snap["over_100"] == [["lei:S", "SIRIUS XM", 119.65, [["sec:A", "LIBERTY A", 49.5, "13D/A", True],
                                                               ["sec:B", "LIBERTY B", 40.15, "13D/A", False],
                                                               ["sec:X", "OLD HOLDER", 30.0, "13D/A", False]]]]
