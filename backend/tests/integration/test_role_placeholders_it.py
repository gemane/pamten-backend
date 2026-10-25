"""Real-ArcadeDB: `manage.py heal-role-placeholders` removes the seats whose
role is Form 4's "See Remarks" placeholder — edge and claim, either spelling —
and nothing else.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _seed(it_db):
    from app.scraper import runner
    chang = runner._upsert_person_by_name("Charles C Chang", source_id="sec-edgar")
    valim = runner._upsert_person_by_name("Francisco Valim", source_id="sec-edgar")
    cook = runner._upsert_person_by_name("Tim Cook", source_id="sec-edgar")
    brk = runner._upsert_entity_by_name(name="Berkshire Hathaway Inc.", entity_type="company")
    tv = runner._upsert_entity_by_name(name="Grupo Televisa", entity_type="company")
    runner._upsert_role(chang, brk, "See Remarks", "sec-edgar", since="2026-06-01")
    runner._upsert_role(valim, tv, "See remarks", "sec-edgar")
    runner._upsert_role(cook, brk, "CEO", "sec-edgar")
    return brk, tv


def _seats(it_db):
    return sorted((r["p"], r["role"]) for r in it_db.run_sql(
        "SELECT @out.full_name AS p, role FROM HAS_ROLE"))


def test_the_dry_run_counts_and_the_apply_deletes_only_the_placeholders(it_db):
    from app.roles import heal_placeholder_roles
    _seed(it_db)
    assert len(_seats(it_db)) == 3
    before_claims = it_db.run_sql("SELECT count(*) AS n FROM Claim WHERE kind = 'role'")[0]["n"]
    assert before_claims == 3

    res = heal_placeholder_roles()
    assert (res["edges"], res["claims"], res["applied"]) == (2, 2, False)
    assert res["companies"] == ["Berkshire Hathaway Inc.", "Grupo Televisa"]
    assert len(_seats(it_db)) == 3                                   # a dry run changes nothing

    res = heal_placeholder_roles(apply=True)
    assert (res["edges"], res["claims"], res["applied"]) == (2, 2, True)
    assert _seats(it_db) == [("Tim Cook", "CEO")]
    roles_left = it_db.run_sql("SELECT role FROM Claim WHERE kind = 'role'")
    assert [r["role"] for r in roles_left] == ["CEO"]
    # the persons stay: they are real, only the roleless seat was noise
    assert it_db.run_sql("SELECT count(*) AS n FROM Person")[0]["n"] == 3
    # a second run finds nothing
    assert heal_placeholder_roles()["edges"] == 0
