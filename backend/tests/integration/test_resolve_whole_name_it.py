"""The resolver against a real ArcadeDB: a query written with its legal form
is a whole legal name (findings item 104, rebuild 2026-11-10). "Citadel
Securities Llc" took Citadel Securities GP LLC while the LLC's own node was
not there, and the run-all's SEC part read the GP's CIK in its place."""
import types

import pytest

pytestmark = pytest.mark.integration


def _index():
    import manage
    manage.cmd_backfill_search(types.SimpleNamespace(batch=1000))


def test_a_whole_name_does_not_take_a_longer_one(it_db):
    from app.routers.search import resolve_best_entity
    it_db.run_command("CREATE (:Entity {id:'sec:0001721695', name:'Citadel Securities GP LLC', "
                      "name_normalized:'citadel securities gp', type:'company', sec_cik:'0001721695'})")
    _index()
    assert resolve_best_entity("Citadel Securities Llc") is None
    # without its form the query names no whole company, and takes the GP
    assert resolve_best_entity("Citadel Securities")["id"] == "sec:0001721695"
    it_db.run_command("CREATE (:Entity {id:'lei:12UUJYTN7D3SW8KCSG25', name:'CITADEL SECURITIES LLC', "
                      "name_normalized:'citadel securities', type:'company', lei_id:'12UUJYTN7D3SW8KCSG25'})")
    _index()
    assert resolve_best_entity("Citadel Securities Llc")["id"] == "lei:12UUJYTN7D3SW8KCSG25"
    assert resolve_best_entity("Citadel Securities GP LLC")["id"] == "sec:0001721695"


def test_a_whole_name_s_form_must_be_the_node_s(it_db):
    from app.routers.search import resolve_best_entity
    it_db.run_command("CREATE (:Entity {id:'lei:TESLALLC0000000000001', name:'Tesla LLC', "
                      "name_normalized:'tesla', type:'company', lei_id:'TESLALLC0000000000001'})")
    it_db.run_command("CREATE (:Entity {id:'lei:ROCHE000000000000001', name:'ROCHE HOLDING LTD', "
                      "name_normalized:'roche holding', type:'company', lei_id:'ROCHE000000000000001', "
                      "aliases:['Roche Holding AG']})")
    _index()
    assert resolve_best_entity("Tesla, Inc.") is None
    assert resolve_best_entity("Tesla LLC")["id"] == "lei:TESLALLC0000000000001"
    assert resolve_best_entity("Roche Holding AG")["id"] == "lei:ROCHE000000000000001"
