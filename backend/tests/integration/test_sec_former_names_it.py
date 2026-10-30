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
        "aliases: ['Meta'], type: 'company', description: 'kept'})"
    )

    _upsert_entity_by_name(
        "Meta Platforms, Inc.", cik="1326801", source_id="sec",
        former_names=["Facebook Inc"],
    )

    row = it_db.run_sql("SELECT aliases, description FROM Entity WHERE id = 'wd'")[0]
    assert set(row["aliases"]) == {"Meta", "Facebook Inc"}   # union — nothing lost
    assert row["description"] == "kept"                       # unrelated fields preserved


def test_dated_former_names_are_stored_merged_and_become_rename_events(it_db):
    """The dated list round-trips through ArcadeDB, a second write unions (and
    dates) it, and the history endpoint reads it back as `renamed` events."""
    from app.scraper.runner import _upsert_entity_by_name
    from app.routers.relationships import ownership_history_of

    it_db.run_command("CREATE (:Source {id: 'sec', name: 'SEC EDGAR', type: 'register'})")
    it_db.run_command(
        "CREATE (:Entity {id: 'wd', name: 'Meta Platforms, Inc.', sec_cik: '1326801', "
        "aliases: ['Meta'], former_names: ['TheFacebook, Inc.|'], type: 'company'})"
    )

    _upsert_entity_by_name(
        "Meta Platforms, Inc.", cik="1326801", source_id="sec",
        former_names=["Facebook Inc|2021-10-27", "TheFacebook, Inc.|2005-05-05"],
    )

    row = it_db.run_sql("SELECT aliases, former_names FROM Entity WHERE id = 'wd'")[0]
    assert set(row["aliases"]) == {"Meta", "Facebook Inc", "TheFacebook, Inc."}
    # the undated entry gained its date; the list is a plain list of strings
    assert row["former_names"] == ["TheFacebook, Inc.|2005-05-05", "Facebook Inc|2021-10-27"]

    events, truncated = ownership_history_of("wd")
    assert truncated is False
    renames = [e for e in events if e["kind"] == "renamed"]
    # newest first, like every other event; each says what the name became
    assert [(e["since"], e["former_name"], e["new_name"]) for e in renames] == [
        ("2021-10-27", "Facebook Inc", "Meta Platforms, Inc."),
        ("2005-05-05", "TheFacebook, Inc.", "Facebook Inc"),
    ]
    assert all(e["party"] is None and e["until"] is None and e["active"] is False for e in renames)


def test_a_company_without_former_names_has_no_rename_events(it_db):
    from app.routers.relationships import ownership_history_of

    it_db.run_command("CREATE (:Entity {id: 'plain', name: 'Plain Ltd', type: 'company'})")
    events, _ = ownership_history_of("plain")
    assert events == []


def test_gleifs_dated_previous_name_is_a_rename_event_too(it_db):
    """Findings item 16a: the timeline reads both former-name properties."""
    from app.routers.relationships import ownership_history_of

    it_db.run_command(
        "CREATE (:Entity {id: 'lei:529900VBK42Y5HHRMD23', name: 'BlackRock, Inc.', type: 'company', "
        "gleif_former_names: ['BLACKROCK FUNDING, INC.|2024-09-30', 'Undated Old Name|']})")
    events, _ = ownership_history_of("lei:529900VBK42Y5HHRMD23")
    assert [(e["since"], e["former_name"], e["new_name"]) for e in events if e["kind"] == "renamed"] == [
        ("2024-09-30", "BLACKROCK FUNDING, INC.", "BlackRock, Inc.")]
