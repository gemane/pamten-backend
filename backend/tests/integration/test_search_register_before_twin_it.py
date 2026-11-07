"""Search against a real ArcadeDB: a register's node of the same name takes a
Wikidata-only node's place (findings item 75, the user's option (b) of
2026-11-07) — "Grupo Bimbo" opened wd:Q775031, an item with no LEI and no
holdings, above GLEIF's Grupo Bimbo S.A.B. with 45. Nothing is merged; the
twin is still found, right below."""
import types

import pytest

pytestmark = pytest.mark.integration


def test_grupo_bimbo_finds_the_register_s_node_first(it_db):
    import manage
    from app.routers.search import resolve_best_entity, search
    it_db.run_command("CREATE (:Entity {id:'wd:Q775031', name:'Grupo Bimbo', name_normalized:'grupo bimbo', "
                      "type:'company', wikidata_id:'Q775031'})")
    it_db.run_command("CREATE (:Entity {id:'lei:984500FEES8936E86D53', name:'GRUPO BIMBO UK LIMITED', "
                      "name_normalized:'grupo bimbo uk', type:'company', lei_id:'984500FEES8936E86D53'})")
    it_db.run_command("CREATE (:Entity {id:'lei:5493000RIXURZEBFEV60', name:'GRUPO BIMBO S A B DE C V', "
                      "name_normalized:'grupo bimbo sab de cv', type:'company', lei_id:'5493000RIXURZEBFEV60', "
                      "legal_form_code:'L9JC'})")
    manage.cmd_backfill_search(types.SimpleNamespace(batch=1000))
    ids = [r["node"]["id"] for r in search(q="Grupo Bimbo", country=None) if r["type"] == "Entity"]
    assert ids[:2] == ["lei:5493000RIXURZEBFEV60", "wd:Q775031"]
    assert resolve_best_entity("Grupo Bimbo")["id"] == "lei:5493000RIXURZEBFEV60"
