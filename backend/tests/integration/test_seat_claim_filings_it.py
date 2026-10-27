"""Real-ArcadeDB: an SEC seat claim carries the record that stated it, the
closing write keeps it on the same claim, and the heal labels what an old
claim can still tell (app.roles.heal_seat_claim_filings).

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
import pytest

pytestmark = pytest.mark.integration


def _claims(it_db, pid):
    return it_db.run_sql("SELECT role, until, filing_type, read_from, source_url, until_source_url, until_read_from "
                         "FROM Claim WHERE kind = 'role' AND from_id = :p", {"p": pid})


def test_the_writers_stamp_the_filing_and_the_close_keeps_one_claim(it_db):
    from app.scraper.sec_writer import _close_role_sec, _upsert_role_sec

    it_db.run_command("CREATE (:Person {id:'p1', full_name:'Warren Buffett'})")
    it_db.run_command("CREATE (:Entity {id:'e1', name:'Berkshire', name_normalized:'berkshire'})")
    _upsert_role_sec("p1", "e1", "Director", "sec-edgar", source_url="u4", source_date="2026-07-15",
                     filing_type="Form 4", read_from="field")
    (claim,) = _claims(it_db, "p1")
    assert claim["filing_type"] == "Form 4" and claim["until"] is None

    assert _close_role_sec("p1", "e1", "2026-12-31", role="Director", source_id="sec-edgar",
                           source_url="u8k", filing_type="8-K", read_from="narrative") == 1
    (claim,) = _claims(it_db, "p1")                                   # the same row, now ended
    assert claim["until"] == "2026-12-31"
    # the appointment keeps its citation, the departure has its own
    assert (claim["filing_type"], claim["source_url"], claim["read_from"]) == ("Form 4", "u4", "field")
    assert (claim["until_source_url"], claim["until_read_from"]) == ("u8k", "narrative")
    edge = it_db.run_sql("SELECT until, source_url, until_source_url FROM HAS_ROLE WHERE @out.id = 'p1'")[0]
    assert (edge["until"], edge["source_url"], edge["until_source_url"]) == ("2026-12-31", "u4", "u8k")
