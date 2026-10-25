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


def test_the_heal_labels_what_the_claim_itself_tells(it_db):
    from app.roles import heal_seat_claim_filings

    rows = (("c1", "narrative", "2026-01-01", "https://www.sec.gov/Archives/edgar/data/1/0001-index.htm"),
            ("c2", "field", None, "https://www.sec.gov/Archives/edgar/data/1/000123/primary_doc.xml"),
            ("c3", "field", None, "https://www.sec.gov/Archives/edgar/data/1/0002-index.htm"),
            ("c4", "field", None, "https://www.sec.gov/Archives/edgar/data/1/0003-index.htm"))
    for key, grade, until, url in rows:
        it_db.run_sql("UPDATE Claim SET claim_key = :k, kind = 'role', from_id = 'p9', to_id = 'e9', "
                      "source_id = 'sec-edgar', role = 'CEO', read_from = :g, until = :u, source_url = :url "
                      "UPSERT WHERE claim_key = :k", {"k": key, "g": grade, "u": until, "url": url})
    it_db.run_sql("UPDATE Claim SET filing_type = 'Form 4' WHERE claim_key = 'c4'")   # already labelled: untouched

    dry = heal_seat_claim_filings(apply=False)
    assert dry == {"applied": False, "form_8k": 1, "form_d": 1, "unfilled": 1}
    assert it_db.run_sql("SELECT filing_type FROM Claim WHERE claim_key = 'c1'")[0]["filing_type"] is None

    res = heal_seat_claim_filings(apply=True)
    assert (res["form_8k"], res["form_d"], res["unfilled"]) == (1, 1, 1)
    got = {r["claim_key"]: r["filing_type"] for r in it_db.run_sql("SELECT claim_key, filing_type FROM Claim")}
    assert got == {"c1": "8-K", "c2": "Form D", "c3": None, "c4": "Form 4"}
    assert heal_seat_claim_filings(apply=True) == {"applied": True, "form_8k": 0, "form_d": 0, "unfilled": 1}
