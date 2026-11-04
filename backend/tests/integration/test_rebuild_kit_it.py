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
