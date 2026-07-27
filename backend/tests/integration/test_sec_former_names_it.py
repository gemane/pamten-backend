"""
Real-ArcadeDB test: SEC EDGAR formerNames folded into an entity's aliases +
search_text make the former name searchable via the FULL_TEXT index — the
CONTAINSTEXT path and list handling the mocked unit tests can't validate.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def test_former_names_make_entity_searchable(it_db):
    from app.scraper.runner import _upsert_entity_by_name

    it_db.run_command("CREATE (:Source {id: 'sec', name: 'SEC EDGAR', type: 'register'})")

    eid = _upsert_entity_by_name(
        "Meta Platforms, Inc.", cik="1326801", source_id="sec",
        former_names=["Facebook Inc"],
    )

    row = it_db.run_sql(f"SELECT name, aliases, search_text FROM Entity WHERE id = '{eid}'")[0]
    assert row["aliases"] == ["Facebook Inc"]
    assert "Facebook Inc" in row["search_text"]

    # Findable by the former name through the FULL_TEXT index on search_text.
    hits = it_db.run_sql("SELECT name FROM Entity WHERE search_text CONTAINSTEXT 'Facebook'")
    assert any(h["name"] == "Meta Platforms, Inc." for h in hits)


def test_existing_entity_gains_former_names_without_losing_existing_aliases(it_db):
    from app.scraper.runner import _upsert_entity_by_name

    it_db.run_command("CREATE (:Source {id: 'sec', name: 'SEC EDGAR', type: 'register'})")
    # A pre-existing (e.g. Wikidata) node with its own alias, matched by CIK.
    it_db.run_command(
        "CREATE (:Entity {id: 'wd', name: 'Meta Platforms, Inc.', sec_cik: '1326801', "
        "aliases: ['Meta'], type: 'company', verified: true})"
    )

    _upsert_entity_by_name(
        "Meta Platforms, Inc.", cik="1326801", source_id="sec",
        former_names=["Facebook Inc"],
    )

    row = it_db.run_sql("SELECT aliases, verified FROM Entity WHERE id = 'wd'")[0]
    assert set(row["aliases"]) == {"Meta", "Facebook Inc"}   # union — nothing lost
    assert row["verified"] is True                            # unrelated fields preserved
