"""A 13D/G holding ends when the shares it is about ceased to exist (findings
item 99), against a real ArcadeDB. Sprint: SoftBank's 13D/A of 2020-02-21
(83.8 %) stayed live after the NYSE removed Sprint's common stock on
2020-04-01 (25-NSE, Rule 12d2-2(a)(3)) in the merger into T-Mobile."""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

SID = "sec-edgar"
SPRINT, SOFTBANK, ROCHE, VANGUARD, TMUS = (
    "lei:549300ZTTY7CXOLJ6539", "lei:SOFTBANK", "lei:ROCHE", "sec:0000102909", "lei:TMUS")
END = {"date": "2020-04-01", "form": "25-NSE", "class": "Common Stock",
       "url": "https://www.sec.gov/Archives/edgar/data/101830/000087666120000282/0000876661-20-000282-index.htm"}


def _companies(it_db):
    for eid, name in ((SPRINT, "SPRINT Corp"), (SOFTBANK, "SoftBank Group Corp"), (ROCHE, "Roche Holding Ltd"),
                      (VANGUARD, "VANGUARD GROUP INC"), (TMUS, "T-Mobile US, Inc.")):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company'}})")
    it_db.run_command(f"MATCH (e:Entity {{id: '{SPRINT}'}}) SET e.sec_cik = '0000101830'")
    it_db.run_command(f"CREATE (:Source {{id: '{SID}', name: 'SEC EDGAR'}})")


def _statement(owner, filed, pct, form="13D/A", owned=SPRINT):
    from app.scraper.sec_writer import _upsert_owns_sec
    _upsert_owns_sec(owner_id=owner, owned_id=owned, source_id=SID, ownership_type="minority",
                     file_date=filed, stake_percent=pct, filing_type=form,
                     since_date=filed, since_basis="first_reported",
                     source_url=f"https://www.sec.gov/{owner}/{filed}")


def _sprint(it_db):
    _companies(it_db)
    _statement(SOFTBANK, "2020-02-21", 83.8)
    it_db.run_command("MATCH (:Entity {id: $o})-[r:OWNS]->(:Entity {id: $s}) SET r.stale = true, "
                      "r.stale_reason = 'not_restated', r.stale_since = '2025-02-21'", {"o": SOFTBANK, "s": SPRINT})
    _statement(ROCHE, "2019-06-01", 100.0)                    # a whole-class holder: the acquirer's word
    _statement(VANGUARD, "2020-05-10", 0.0, form="13G/A")     # filed after the end: not this rule's
    _statement(TMUS, "2019-01-01", 4.0, form="13F")           # another rulebook


def _owners(it_db):
    return {r["a"]: r for r in it_db.run_command(
        "MATCH (a:Entity)-[r:OWNS]->(b:Entity {id: $b}) RETURN a.id AS a, r.until AS until, "
        "r.until_reason AS why, r.until_source_url AS url, r.stale AS stale, r.stale_reason AS sreason",
        {"b": SPRINT})}


def test_a_statement_filed_before_the_class_ended_closes_on_that_day(it_db):
    from app.scraper.sec_writer import close_at_class_end
    _sprint(it_db)
    assert close_at_class_end(SPRINT, [END]) == 1
    owners = _owners(it_db)
    sb = owners[SOFTBANK]
    assert (sb["until"], sb["why"], sb["url"]) == ("2020-04-01", "class_ended", END["url"])
    assert not sb["stale"] and sb["sreason"] is None
    assert owners[ROCHE]["until"] is None                     # ≥ 99.5 %: the acquirer's
    assert owners[VANGUARD]["until"] is None                  # filed after the end
    assert owners[TMUS]["until"] is None                      # a 13F is not a 13D/G
    assert close_at_class_end(SPRINT, [END]) == 0             # idempotent
    assert close_at_class_end(SPRINT, []) == 0


def test_the_first_end_after_the_statement_is_the_one(it_db):
    from app.scraper.sec_writer import close_at_class_end
    _sprint(it_db)
    older = {**END, "date": "2013-07-10", "url": "https://www.sec.gov/old"}
    later = {**END, "date": "2020-04-13", "form": "15-12B", "url": "https://www.sec.gov/later"}
    assert close_at_class_end(SPRINT, [later, END, older]) == 1
    assert _owners(it_db)[SOFTBANK]["until"] == "2020-04-01"


def test_re_reading_the_filing_updates_the_closed_spell(it_db):
    from app.scraper.sec_writer import close_at_class_end
    _sprint(it_db)
    close_at_class_end(SPRINT, [END])
    _statement(SOFTBANK, "2020-02-21", 83.8)                  # the next scrape reads it again
    close_at_class_end(SPRINT, [END])
    spells = [(r["until"], r["why"]) for r in it_db.run_command(
        "MATCH (a:Entity {id: $o})-[r:OWNS]->(b:Entity {id: $s}) RETURN r.until AS until, r.until_reason AS why",
        {"o": SOFTBANK, "s": SPRINT})]
    assert spells == [("2020-04-01", "class_ended")]          # the one spell, no twin drawn beside it


def test_the_claim_is_not_touched(it_db):
    from app.scraper.sec_writer import close_at_class_end
    _sprint(it_db)
    close_at_class_end(SPRINT, [END])
    claims = it_db.run_command("MATCH (c:Claim) WHERE c.kind = 'owns' AND c.from_id = $o AND c.to_id = $s "
                               "RETURN c.until AS until", {"o": SOFTBANK, "s": SPRINT})
    assert claims and all(c.get("until") is None for c in claims)


def test_the_whole_graph_pass_reads_each_issuer_once(it_db):
    from app.scraper.sec_writer import close_ended_classes
    _sprint(it_db)
    with patch("app.scraper.sec_class_end.class_ends", return_value=[END]) as read:
        res = close_ended_classes()
    read.assert_called_once_with("0000101830")                # issuers with a CIK and an open 13D/G
    assert (res["companies"], res["read"], res["failed"], res["closed"]) == (1, 1, 0, 1)
    assert res["lines"] == ["SPRINT Corp: 1 closed on 2020-04-01 (25-NSE, Common Stock)"]


def test_a_failed_read_is_counted_and_closes_nothing(it_db):
    from app.scraper.sec_writer import close_ended_classes
    _sprint(it_db)
    with patch("app.scraper.sec_class_end.class_ends", side_effect=RuntimeError("timeout")):
        res = close_ended_classes()
    assert (res["read"], res["failed"], res["closed"]) == (0, 1, 0)
    assert _owners(it_db)[SOFTBANK]["until"] is None
