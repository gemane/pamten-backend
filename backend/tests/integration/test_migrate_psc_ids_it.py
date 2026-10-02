"""The one-shot chpsc id migration, against a real database.

Node ids minted before psc_slug_id existed carry the raw CH self-link with
slashes; migrate-psc-ids rewrites the node, follows every property store that
holds node ids, and leaves a MergedId forwarding row so old links resolve.
"""
import pytest

pytestmark = pytest.mark.integration

def test_migrate_psc_ids_rewrites_the_node_its_claims_and_leaves_forwarding(it_db):
    """The one-shot migration: node id, Claim rows, and a MergedId redirect."""
    from argparse import Namespace
    import manage
    from app.config import settings
    from app.merged_ids import resolve_current_id
    from app.database import db

    old = "chpsc:/company/07882791/persons-with-significant-control/corporate-entity/tMR42k"
    it_db.run_command(
        "CREATE (:Entity {id: $id, name: 'Old Style Ltd', name_normalized: 'old style', "
        "search_text: 'Old Style Ltd', type: 'company'})", {"id": old})
    from app.claims import claim_key
    it_db.run_sql("INSERT INTO Claim SET claim_key = :k, kind = 'owns', from_id = :f, "
                  "to_id = 'gb-coh:07882791', source_id = 'ch-psc', stake_percent = 75.0",
                  {"f": old, "k": claim_key("owns", old, "gb-coh:07882791", "ch-psc")})

    manage.cmd_migrate_psc_ids(Namespace(confirm_database=settings.ARCADEDB_DATABASE))

    new = "chpsc:07882791:tMR42k"
    assert it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.name AS n",
                             {"id": new})[0]["n"] == "Old Style Ltd"
    assert not it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e",
                                 {"id": old})
    # the claim follows the node AND is re-keyed for its new id (a claim left
    # under its old key is not found by the next import, which writes a second)
    [claim] = it_db.run_sql("SELECT from_id, claim_key, stake_percent FROM Claim")
    assert claim["from_id"] == new and claim["stake_percent"] == 75.0
    assert claim["claim_key"] == claim_key("owns", new, "gb-coh:07882791", "ch-psc")
    with db.get_session() as session:
        assert resolve_current_id(session, old) == new

    # Idempotent: a second run finds nothing in the old shape.
    manage.cmd_migrate_psc_ids(Namespace(confirm_database=settings.ARCADEDB_DATABASE))
    assert it_db.run_command("MATCH (e:Entity {id: $id}) RETURN e.name AS n",
                             {"id": new})[0]["n"] == "Old Style Ltd"
