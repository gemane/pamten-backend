"""Search against a real ArcadeDB: a group that matches the query takes its
own subsidiary's place (findings item 76, the user's option (a) of
2026-11-08). "America Movil" opened América Móvil B.V. above AMERICA MOVIL
SAB DE CV, which owns it in GLEIF. Only a subsidiary edge counts (GLEIF RR,
Exhibit 21 / 8.1, a majority): a shareholder's 13G stake never moves it."""
import types

import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    it_db.run_command("CREATE (:Entity {id:'lei:724500AVF7ZU7L61BL70', name:'América Móvil B.V.', "
                      "name_normalized:'america movil', type:'company', lei_id:'724500AVF7ZU7L61BL70', "
                      "legal_form_code:'54M6', wikidata_id:'Q482267'})")
    it_db.run_command("CREATE (:Entity {id:'lei:5493000FNR3UCEAONM59', name:'AMERICA MOVIL SAB DE CV/', "
                      "name_normalized:'america movil sab de cv', type:'company', lei_id:'5493000FNR3UCEAONM59', "
                      "legal_form_code:'L9JC'})")


def test_america_movil_finds_the_group_first(it_db):
    import manage
    from app.routers.search import resolve_best_entity, search
    _seed(it_db)
    it_db.run_command("MATCH (a:Entity {id:'lei:5493000FNR3UCEAONM59'}), (b:Entity {id:'lei:724500AVF7ZU7L61BL70'}) "
                      "CREATE (a)-[:OWNS {source_id:'gleif', filing_type:'RR', ownership_type:'unknown'}]->(b)")
    manage.cmd_backfill_search(types.SimpleNamespace(batch=1000))
    ids = [r["node"]["id"] for r in search(q="America Movil", country=None) if r["type"] == "Entity"]
    assert ids[:2] == ["lei:5493000FNR3UCEAONM59", "lei:724500AVF7ZU7L61BL70"]
    assert resolve_best_entity("America Movil")["id"] == "lei:5493000FNR3UCEAONM59"


def test_a_stake_or_an_ended_edge_moves_nothing(it_db):
    import manage
    from app.routers.search import resolve_best_entity
    _seed(it_db)
    it_db.run_command("MATCH (a:Entity {id:'lei:5493000FNR3UCEAONM59'}), (b:Entity {id:'lei:724500AVF7ZU7L61BL70'}) "
                      "CREATE (a)-[:OWNS {source_id:'sec-edgar', filing_type:'13G', ownership_type:'minority'}]->(b)")
    it_db.run_command("MATCH (a:Entity {id:'lei:5493000FNR3UCEAONM59'}), (b:Entity {id:'lei:724500AVF7ZU7L61BL70'}) "
                      "CREATE (a)-[:OWNS {source_id:'gleif', filing_type:'RR', until:'2020-01-01'}]->(b)")
    manage.cmd_backfill_search(types.SimpleNamespace(batch=1000))
    assert resolve_best_entity("America Movil")["id"] == "lei:724500AVF7ZU7L61BL70"
