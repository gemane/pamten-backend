"""Person resolution by SEC CIK against a real database: a filer's many name
spellings collapse to one node, the CIK is stamped on a name-matched node, and
a distinct filer (distinct CIK) never merges — the father/son-safe key."""
import pytest

from app.scraper.graph_writer import _upsert_person_by_name

pytestmark = pytest.mark.integration


def test_same_cik_different_spellings_resolve_to_one_node(it_db):
    a = _upsert_person_by_name("Timothy D Cook", sec_cik="0001214156")
    b = _upsert_person_by_name("Cook Timothy D", sec_cik="0001214156")  # SEC last-first
    assert a == b, "the CIK collapses the spelling variants to one node"
    n = it_db.run_sql("SELECT count(*) AS n FROM Person WHERE sec_cik = '0001214156'")[0]["n"]
    assert n == 1


def test_cik_is_stamped_on_a_name_matched_node(it_db):
    first = _upsert_person_by_name("Warren Buffett")                    # no CIK yet
    again = _upsert_person_by_name("Warren Buffett", sec_cik="0000315090")
    assert first == again, "matched by name"
    cik = it_db.run_sql("SELECT sec_cik FROM Person WHERE id = :id", {"id": first})[0]["sec_cik"]
    assert cik == "0000315090", "the CIK was filled in on the existing node"


def test_a_different_cik_is_a_different_person(it_db):
    # Same name, different CIK = father and son. They must NOT collapse.
    dad = _upsert_person_by_name("John Q Public", sec_cik="0000111111")
    son = _upsert_person_by_name("John Q Public", sec_cik="0000222222")
    assert dad != son, "distinct CIKs are distinct people, however identical the name"
    assert it_db.run_sql("SELECT count(*) AS n FROM Person "
                         "WHERE full_name = 'John Q Public'")[0]["n"] == 2
