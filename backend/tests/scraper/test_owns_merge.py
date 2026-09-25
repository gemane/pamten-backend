"""The rule two sources follow when they share one OWNS edge (app.scraper.owns_merge).

Pure: no database. The writers that apply it are pinned against a real ArcadeDB
in tests/integration/test_one_owns_edge_per_pair_it.py.
"""
from app.scraper.owns_merge import (
    ANSWER_FIELDS, answer_rank, combine_since, fold, outranks,
)

SEC_EX21 = {"source_id": "sec", "credibility_score": 98, "stake_percent": None,
            "filing_type": "EX-21", "source_url": "https://www.sec.gov/ex21.htm"}
PSC = {"source_id": "psc", "credibility_score": 97, "stake_percent": 75.0,
       "filing_type": "PSC", "source_url": "https://find-and-update.example.test/psc"}
GLEIF = {"source_id": "gleif", "credibility_score": 92, "stake_percent": None,
         "filing_type": "RR", "direct_or_indirect": "direct"}
OC = {"source_id": "oc", "credibility_score": 85, "stake_percent": 42.0}


class TestRank:
    def test_among_registers_a_stated_stake_beats_a_silent_list(self):
        # Exhibit 21 names a subsidiary without a number; the register says 75%.
        assert outranks(PSC, SEC_EX21)
        assert not outranks(SEC_EX21, PSC)

    def test_without_stakes_credibility_decides(self):
        assert outranks(SEC_EX21, GLEIF)
        assert not outranks(GLEIF, SEC_EX21)

    def test_a_community_number_never_displaces_a_register(self):
        assert not outranks(OC, GLEIF)
        assert outranks(GLEIF, OC)

    def test_a_tie_keeps_the_incumbent(self):
        # Strict: two equal sources re-asserting on alternate nights must not
        # flip the edge back and forth.
        assert not outranks(dict(SEC_EX21, source_id="other"), SEC_EX21)

    def test_missing_credibility_ranks_lowest(self):
        assert answer_rank({}) < answer_rank(OC)


class TestCombineSince:
    def test_the_earliest_date_wins_and_keeps_its_basis(self):
        lower = {"since": "2013-06-30", "since_basis": "first_listed",
                 "since_source_url": "https://www.sec.gov/2013.htm"}
        stated = {"since": "2024-06-30"}
        assert combine_since(stated, lower) == lower
        assert combine_since(lower, stated) == lower

    def test_a_stated_start_before_the_bound_replaces_it(self):
        lower = {"since": "2013-06-30", "since_basis": "first_listed", "since_source_url": "u"}
        assert combine_since(lower, {"since": "1991-01-20"}) == {
            "since": "1991-01-20", "since_basis": None, "since_source_url": None}

    def test_on_the_same_day_a_stated_start_beats_the_bound(self):
        lower = {"since": "2013-06-30", "since_basis": "first_listed", "since_source_url": "u"}
        assert combine_since(lower, {"since": "2013-06-30"})["since_basis"] is None

    def test_undated_candidates_do_not_count(self):
        assert combine_since(None, {"since": None}, {"since": "2020-01-01"})["since"] == "2020-01-01"
        assert combine_since(None, {}) == {"since": None, "since_basis": None,
                                           "since_source_url": None}


class TestFold:
    def test_the_higher_source_s_answer_moves_onto_the_survivor_as_a_unit(self):
        gleif = dict(GLEIF, since="2024-06-30", last_scraped_at="2026-09-01")
        sec = dict(SEC_EX21, since="2013-06-30", since_basis="first_listed",
                   since_source_url="https://www.sec.gov/2013.htm",
                   last_scraped_at="2026-09-20")
        out = fold([gleif, sec], gleif)
        assert {f: out[f] for f in ANSWER_FIELDS} == {f: sec.get(f) for f in ANSWER_FIELDS}
        assert (out["since"], out["since_basis"]) == ("2013-06-30", "first_listed")
        assert out["last_scraped_at"] == "2026-09-20"
        assert "direct_or_indirect" not in out          # the survivor's marker stays

    def test_a_survivor_that_already_holds_the_answer_only_gains_what_it_lacks(self):
        psc = dict(PSC, since="2016-04-06")
        sec = dict(SEC_EX21, direct_or_indirect="direct", since=None)
        out = fold([psc, sec], psc)
        assert out == {"direct_or_indirect": "direct"}

    def test_nothing_to_do_for_edges_that_agree(self):
        a = dict(GLEIF, since="2020-01-01")
        assert fold([a, dict(a)], a) == {}

    def test_the_shortcut_mark_is_never_copied(self):
        survivor = dict(GLEIF)
        other = dict(GLEIF, source_id="gleif2", shortcut=True)
        assert "shortcut" not in fold([survivor, other], survivor)

    def test_staleness_clears_when_any_edge_is_fresh(self):
        stale = dict(GLEIF, stale=True)
        assert fold([stale, dict(SEC_EX21, stale=False)], stale)["stale"] is False
