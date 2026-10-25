"""Real-ArcadeDB: `manage.py heal-name-keys` recomputes the stored name key
where the normaliser changed and leaves the rest alone.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_old_keys_are_recomputed_and_the_hidden_pair_named(it_db):
    from app.name_keys import heal_name_keys
    # written before 2026-10-25: the keys the old normaliser stored
    it_db.run_command("CREATE (:Entity {id:'lei:tv', name:'GRUPO TELEVISA S A B', name_normalized:'grupo televisa s a b', country:'MX', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'sec:tv', name:'GRUPO TELEVISA, S.A.B.', name_normalized:'grupo televisa sab', country:'MX', type:'company'})")
    it_db.run_command("CREATE (:Entity {id:'apple', name:'Apple Inc.', name_normalized:'apple', country:'US', type:'company'})")
    # a 13D voting group: keyed by the writer from the issuer, never from its display name
    it_db.run_command("CREATE (:Entity {id:'vg', name:'Voting group · 5 parties', name_normalized:'voting group mercadolibre', type:'voting_group'})")

    res = heal_name_keys()
    assert (res["scanned"], res["changed"], res["applied"]) == (4, 1, False)
    assert res["newly_shared"] == {"grupo televisa sab | MX": ["GRUPO TELEVISA S A B", "GRUPO TELEVISA, S.A.B."]}
    assert it_db.run_sql("SELECT name_normalized AS k FROM Entity WHERE id = 'lei:tv'")[0]["k"] == "grupo televisa s a b"

    res = heal_name_keys(apply=True)
    assert res["changed"] == 1
    keys = {r["id"]: r["k"] for r in it_db.run_sql("SELECT id, name_normalized AS k FROM Entity")}
    assert keys == {"lei:tv": "grupo televisa sab", "sec:tv": "grupo televisa sab", "apple": "apple",
                    "vg": "voting group mercadolibre"}
    # a second run finds nothing left, and the pair is no longer news
    again = heal_name_keys()
    assert again["changed"] == 0 and again["newly_shared"] == {}
