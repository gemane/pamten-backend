"""Real-ArcadeDB test of `manage.py dedupe-persons` (findings item 55): the pair
the 2026-11-04 rebuild left — EDGAR's "Lachlan K. Murdoch" written in one scrape,
Wikidata's "Lachlan Murdoch" two hours later in another — merges when the command
runs after the scrapes; a dry run reports it and changes nothing; `--scraped-only`
seeds from the persons with a hard id, so a register-only pair waits for the
full scan.

Skipped unless ARCADEDB_IT_URL is set — see conftest.py.
"""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration


def _news_corp_pair(it_db):
    it_db.run_command("CREATE (:Entity {id:'nws', name:'News Corp', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'sec:0001242818', full_name:'Lachlan K. Murdoch', sec_cik:'0001242818', aliases: []})")
    it_db.run_command("CREATE (:Person {id:'wd:Q3215817', full_name:'Lachlan Murdoch', wikidata_id:'Q3215817', aliases: []})")
    for pid in ("sec:0001242818", "wd:Q3215817"):
        it_db.run_command("MATCH (p:Person {id:$p}),(e:Entity {id:'nws'}) CREATE (p)-[:HAS_ROLE {role:'Director'}]->(e)",
                          {"p": pid})


def _register_pair(it_db):
    # two spellings of one person from a register, no hard id on either
    it_db.run_command("CREATE (:Entity {id:'brk', name:'Berkshire Hathaway', type:'company'})")
    it_db.run_command("CREATE (:Person {id:'u1', full_name:'Warren E Buffett', aliases: []})")
    it_db.run_command("CREATE (:Person {id:'u2', full_name:'Buffett Warren E', aliases: []})")
    for pid in ("u1", "u2"):
        it_db.run_command("MATCH (p:Person {id:$p}),(e:Entity {id:'brk'}) CREATE (p)-[:OWNS {}]->(e)", {"p": pid})


def _murdochs(it_db):
    return it_db.run_command("MATCH (p:Person) WHERE p.full_name CONTAINS 'Murdoch' RETURN p.id AS id ORDER BY id")


def test_the_rebuilds_twin_pair_merges(it_db, capsys):
    import manage
    _news_corp_pair(it_db)

    manage.cmd_dedupe_persons(SimpleNamespace(scraped_only=False, dry_run=False, db_url=None))

    assert len(_murdochs(it_db)) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "Merged 1 persons across 1 groups; 0 groups need review"
    assert out[0].startswith("  Lachlan Murdoch (") and out[0].endswith("takes Lachlan K. Murdoch")


def test_a_dry_run_reports_and_keeps_both(it_db, capsys):
    import manage
    _news_corp_pair(it_db)

    manage.cmd_dedupe_persons(SimpleNamespace(scraped_only=False, dry_run=True, db_url=None))

    assert len(_murdochs(it_db)) == 2
    assert capsys.readouterr().out.splitlines()[-1] == "Would merge 1 persons across 1 groups; 0 groups need review"


def test_scraped_only_seeds_from_the_hard_ids(it_db, capsys):
    import manage
    _news_corp_pair(it_db)
    _register_pair(it_db)
    # a Wikidata person with no CIK beside a register spelling of the same name:
    # the item is the hard id that makes them a seed
    it_db.run_command("CREATE (:Person {id:'wd:Q471851', full_name:'James Murdoch', wikidata_id:'Q471851', aliases: []})")
    it_db.run_command("CREATE (:Person {id:'reg-james', full_name:'Murdoch James', aliases: []})")
    for pid in ("wd:Q471851", "reg-james"):
        it_db.run_command("MATCH (p:Person {id:$p}),(e:Entity {id:'nws'}) CREATE (p)-[:HAS_ROLE {role:'Director'}]->(e)",
                          {"p": pid})

    manage.cmd_dedupe_persons(SimpleNamespace(scraped_only=True, dry_run=False, db_url=None))
    # both scraped pairs merged — Lachlan's survivor took the twin's CIK and lives
    # under it now (keyed ids: `sec:` outranks `wd:`), James's stays `wd:`
    assert [r["id"] for r in _murdochs(it_db)] == ["sec:0001242818", "wd:Q471851"]
    assert len(it_db.run_command("MATCH (p:Person) WHERE p.full_name CONTAINS 'Buffett' RETURN p.id AS id")) == 2  # …the register pair not seen
    assert capsys.readouterr().out.splitlines()[-1] == "Merged 2 persons across 2 groups; 0 groups need review"

    manage.cmd_dedupe_persons(SimpleNamespace(scraped_only=False, dry_run=False, db_url=None))
    assert len(it_db.run_command("MATCH (p:Person) WHERE p.full_name CONTAINS 'Buffett' RETURN p.id AS id")) == 1  # the full scan sees it
