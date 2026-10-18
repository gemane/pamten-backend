"""The rule two sources follow when they share one OWNS edge (app.scraper.owns_merge).

Pure: no database. The writers that apply it are pinned against a real ArcadeDB
in tests/integration/test_one_owns_edge_per_pair_it.py.
"""
from app.scraper.owns_merge import (
    ANSWER_FIELDS, answer_rank, combine_since, fold, outranks, said_at, supersedes,
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


class TestSupersedes:
    """One source's newer statement replaces its older one — the tie rule
    between sources does not apply within a source."""
    OLD = dict(PSC, source_date="2016-04-06", last_scraped_at="2025-01-01T00:00:00")
    NEW = dict(PSC, source_date="2026-10-16", last_scraped_at="2026-10-18T00:00:00")

    def test_the_same_source_s_later_snapshot_supersedes_its_earlier_one(self):
        assert supersedes(self.NEW, self.OLD)
        assert not supersedes(self.OLD, self.NEW)
        assert not outranks(self.NEW, self.OLD)          # equal rank: the fold needs the date

    def test_another_source_never_supersedes_on_the_date(self):
        other = dict(self.NEW, source_id="psc2")
        assert not supersedes(other, self.OLD)

    def test_a_better_or_worse_answer_is_outranking_not_superseding(self):
        # a stake that appeared or vanished changes the rank; `outranks` decides it
        silent_newer = dict(self.NEW, stake_percent=None)
        assert not supersedes(silent_newer, self.OLD) and outranks(self.OLD, silent_newer)

    def test_undated_statements_fall_back_to_the_scrape_time(self):
        a = dict(GLEIF, last_scraped_at="2026-01-01T00:00:00")
        b = dict(GLEIF, last_scraped_at="2026-02-01T00:00:00")
        assert supersedes(b, a) and not supersedes(a, b)
        assert said_at({}) == ("", "") and said_at(b) == ("", "2026-02-01T00:00:00")

    def test_the_same_statement_twice_supersedes_nothing(self):
        assert not supersedes(dict(self.NEW), self.NEW)

    def test_without_a_source_id_nothing_matches(self):
        assert not supersedes(dict(self.NEW, source_id=None), dict(self.OLD, source_id=None))


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
        assert out == {"direct_or_indirect": "direct", "structure_basis": None}

    def test_an_inferred_marker_brings_its_basis_and_a_stated_one_none(self):
        # SEC's Exhibit 21 writer infers a marker from the filer's layout and
        # says so; GLEIF states one. The pair must stay paired when copied.
        psc = dict(PSC)
        sec = dict(SEC_EX21, direct_or_indirect="direct", structure_basis="ex21_indent")
        assert fold([psc, sec], psc) == {"direct_or_indirect": "direct",
                                         "structure_basis": "ex21_indent"}
        gleif = dict(GLEIF)
        assert "structure_basis" not in fold([gleif, sec], gleif)   # GLEIF's stated marker stays

    def test_nothing_to_do_for_edges_that_agree(self):
        a = dict(GLEIF, since="2020-01-01")
        assert fold([a, dict(a)], a) == {}

    def test_the_same_source_s_newer_statement_takes_the_answer_from_an_old_survivor(self):
        # Dev, 2026-10-18: the register's 2016 edge survived the dedup for GLEIF's
        # `direct` marker and the 2026 re-import's twin was deleted — the edge kept
        # saying "source_date 2016-04-06" (and would have kept an old stake).
        old = dict(PSC, source_date="2016-04-06", since="2016-04-06", since_basis="register_start",
                   direct_or_indirect="direct", last_scraped_at="2025-01-01T00:00:00")
        new = dict(PSC, source_date="2026-10-16", stake_percent=50.0, since="2016-04-06",
                   since_basis="register_start", last_scraped_at="2026-10-18T00:00:00",
                   source_url="https://find-and-update.example.test/psc/2026")
        out = fold([old, new], old)
        assert {f: out[f] for f in ANSWER_FIELDS} == {f: new.get(f) for f in ANSWER_FIELDS}
        assert out["last_scraped_at"] == "2026-10-18T00:00:00"
        assert "direct_or_indirect" not in out          # the marker stays with the survivor
        assert "since" not in out                       # the start is unchanged

    def test_a_newer_survivor_keeps_its_own_answer(self):
        old = dict(PSC, source_date="2016-04-06", last_scraped_at="2025-01-01T00:00:00")
        new = dict(PSC, source_date="2026-10-16", last_scraped_at="2026-10-18T00:00:00")
        assert fold([new, old], new) == {}

    def test_another_source_s_newer_statement_still_leaves_the_incumbent(self):
        held = dict(SEC_EX21, source_date="2016-01-01", last_scraped_at="2025-01-01T00:00:00")
        other = dict(SEC_EX21, source_id="other", source_date="2026-10-16",
                     last_scraped_at="2026-10-18T00:00:00")
        assert "source_id" not in fold([held, other], held)

    def test_a_better_ranked_source_beats_the_same_source_s_newer_statement(self):
        old = dict(GLEIF, source_date="2016-01-01")
        new = dict(GLEIF, source_date="2026-10-16")
        assert fold([old, new, dict(PSC)], old)["source_id"] == "psc"
        assert fold([old, dict(PSC), new], old)["source_id"] == "psc"

    def test_the_shortcut_mark_is_never_copied(self):
        survivor = dict(GLEIF)
        other = dict(GLEIF, source_id="gleif2", shortcut=True)
        assert "shortcut" not in fold([survivor, other], survivor)

    def test_staleness_clears_when_any_edge_is_fresh(self):
        stale = dict(GLEIF, stale=True)
        assert fold([stale, dict(SEC_EX21, stale=False)], stale)["stale"] is False


def test_a_refuted_start_never_wins_a_merge():
    # GLEIF's archive refutes a start before since_not_before (gleif_rr_history)
    from app.scraper.owns_merge import combine_since, fold
    assert combine_since({"since": "2001-07-03"}, {"since": "2026-08-01", "since_basis": "gleif_first_seen"},
                         not_before="2026-07-01")["since"] == "2026-08-01"
    survivor = {"since": "2026-08-01", "since_basis": "gleif_first_seen", "since_not_before": "2026-07-01",
                "credibility_score": 92, "source_id": "g"}
    other = {"since": "2001-07-03", "credibility_score": 92, "source_id": "g"}
    assert "since" not in fold([survivor, other], survivor)
    # and the floor travels to a survivor that lacks it
    out = fold([other, survivor], other)
    assert out["since_not_before"] == "2026-07-01" and out["since"] == "2026-08-01"
