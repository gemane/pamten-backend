"""Real-ArcadeDB tests of the shared registered address — the profile's
`registered_address` section and GET /search/address (app.registered_address).

Mirrors dev's largest group: companies registered at a corporate agent's door
in Wilmington, one of them suppressed, one at the same building under another
spelling (a separate group — the key is exact by design)."""
import pytest
from fastapi import HTTPException

from app.routers.search import companies_at_address, get_full_profile
from app.scraper.mapper import normalize_address

pytestmark = pytest.mark.integration

CSC = "C/O Corporation Service Company, 251 Little Falls Drive, Wilmington, 19808, US"
KEY = normalize_address(CSC)


def _company(it_db, cid, name, address=None):
    props = f"id:'{cid}', name:'{name}', type:'company'"
    if address:
        props += f", address:'{address}', address_normalized:'{normalize_address(address)}'"
    it_db.run_command(f"CREATE (e:Entity {{{props}}})")


def _wilmington(it_db):
    _company(it_db, "A", "Alpha Holdings LLC", CSC)
    _company(it_db, "B", "Beta Finance LLC", CSC)
    # the same key from a source that spells it in capitals
    _company(it_db, "C", "Charlie Capital LLC", CSC.upper())
    _company(it_db, "D", "Delta Ventures LLC", CSC)
    # the same building without the agent's name: another key, another group
    _company(it_db, "E", "Echo Trust LLC", "251 Little Falls Drive, Wilmington, 19808, US")
    _company(it_db, "F", "Foxtrot Ltd", "1 Churchill Place, London, E14 5HP, GB")
    _company(it_db, "G", "Golf Co")  # no registered office
    it_db.run_command("CREATE (s:Suppression {target_kind:'entity', node_id:'D'})")


def test_profile_lists_the_others_at_the_same_door(it_db):
    _wilmington(it_db)
    section = get_full_profile("A")["registered_address"]
    assert section["key"] == KEY
    assert section["address"] == CSC
    # B and C (same key, other spelling); not A itself, not the suppressed D,
    # not E at the same building under another key
    assert [e["id"] for e in section["entities"]] == ["B", "C"]
    assert section["count"] == 2


def test_profile_count_is_true_beyond_the_row_limit(it_db):
    _wilmington(it_db)
    section = get_full_profile("A", limit=1)["registered_address"]
    assert [e["id"] for e in section["entities"]] == ["B"]
    assert section["count"] == 2


def test_alone_at_its_address_and_without_one(it_db):
    _wilmington(it_db)
    alone = get_full_profile("F")["registered_address"]
    assert alone["count"] == 0 and alone["entities"] == []
    assert get_full_profile("G")["registered_address"] is None


def test_address_endpoint_lists_everyone_paged(it_db):
    _wilmington(it_db)
    page = companies_at_address(key=KEY, limit=10, offset=0)
    assert page["count"] == 3
    assert [e["id"] for e in page["entities"]] == ["A", "B", "C"]
    assert page["address"] == CSC  # the spelling most of them use
    second = companies_at_address(key=KEY, limit=2, offset=2)
    assert [e["id"] for e in second["entities"]] == ["C"]


def test_address_endpoint_takes_the_readable_address(it_db):
    _wilmington(it_db)
    page = companies_at_address(key=CSC, limit=10, offset=0)
    assert page["key"] == KEY and page["count"] == 3


def test_address_endpoint_404_for_an_empty_door(it_db):
    _wilmington(it_db)
    with pytest.raises(HTTPException) as exc:
        companies_at_address(key="nowhere street 1", limit=10, offset=0)
    assert exc.value.status_code == 404


def test_profile_carries_at_most_fifty_neighbours(it_db):
    for i in range(53):
        _company(it_db, f"N{i:02d}", f"Nominee {i:02d} LLC", CSC)
    section = get_full_profile("N00")["registered_address"]
    assert section["count"] == 52
    assert len(section["entities"]) == 50
    assert companies_at_address(key=KEY, limit=1000, offset=0)["count"] == 53
