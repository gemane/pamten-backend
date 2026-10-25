"""Role synonym canon: one position, many spellings — and only true synonyms."""
from app.roles import canonical_role


class TestCanonicalRole:
    def test_board_member_and_director_are_one_seat(self):
        assert canonical_role("Board Member") == canonical_role("Director")
        assert canonical_role("Member of the Board of Directors") == "director"

    def test_c_suite_abbreviations_fold(self):
        assert canonical_role("Chief Executive Officer") == canonical_role("CEO")
        assert canonical_role("Chief Financial Officer") == canonical_role("CFO")
        assert canonical_role("Chairperson") == canonical_role("Chairman")
        assert canonical_role("Chairman of the Board") == "chairman"

    def test_broader_titles_stay_distinct(self):
        # "Executive Officer" (Form D) covers more than CEO; folding it would
        # assert precision the source never stated.
        assert canonical_role("Executive Officer") != canonical_role("CEO")
        assert canonical_role("President") == "president"

    def test_punctuation_case_and_none_are_formatting(self):
        assert canonical_role("board  member") == "director"
        assert canonical_role("C.E.O.") != "ceo"  # dotted forms split to letters — unknown stays itself
        assert canonical_role(None) == ""


class TestRoleClaimKeys:
    def test_two_roles_from_one_source_are_two_claims(self):
        from app.claims import claim_props
        a = claim_props(kind="role", from_id="p", to_id="e", source_id="s", role="CEO")
        b = claim_props(kind="role", from_id="p", to_id="e", source_id="s", role="Director")
        assert a["claim_key"] != b["claim_key"]

    def test_synonyms_share_a_claim_key(self):
        from app.claims import claim_props
        a = claim_props(kind="role", from_id="p", to_id="e", source_id="s", role="Director")
        b = claim_props(kind="role", from_id="p", to_id="e", source_id="s", role="Board Member")
        assert a["claim_key"] == b["claim_key"]

    def test_owns_keys_are_untouched(self):
        from app.claims import claim_key
        assert claim_key("owns", "a", "b", "s") == claim_key("owns", "a", "b", "s")
        assert claim_key("owns", "a", "b", "s") != claim_key("role", "a", "b", "s")


class TestSameStart:
    """One start date at two precisions is one date (Roslansky's LinkedIn seat)."""

    def test_a_month_contains_its_day(self):
        from app.roles import same_start
        assert same_start("2020-06-00", "2020-06-01")
        assert same_start("2020-06-01", "2020-06-00")

    def test_a_year_contains_its_days_and_months(self):
        from app.roles import same_start
        assert same_start("2014-00-00", "2014-10-01")
        assert same_start("2015-12-00", "2015-00-00")

    def test_two_different_days_are_two_dates(self):
        from app.roles import same_start
        assert not same_start("2022-07-11", "2022-07-14")
        assert not same_start("2020-06-00", "2020-07-01")
        assert not same_start("2020-00-00", "2021-03-01")

    def test_a_bare_year_is_its_own_year_only(self):
        # The separator is part of the test: "202" must not contain "2020-…".
        from app.roles import same_start
        assert same_start("2020-00-00", "2020")
        assert not same_start("202-00-00", "2020-01-01")

    def test_undated_is_no_match(self):
        from app.roles import same_start
        assert not same_start(None, "2020-06-01")
        assert not same_start("2020-06-01", None)
        assert not same_start(None, None)

    def test_equal_days_match(self):
        from app.roles import same_start
        assert same_start("2019-04-25", "2019-04-25")


class TestFinerStart:
    def test_the_day_beats_the_month_and_the_year(self):
        from app.roles import finer_start
        assert finer_start("2020-06-00", "2020-06-01") == "2020-06-01"
        assert finer_start("2020-06-01", "2020-06-00") == "2020-06-01"
        assert finer_start("2014-00-00", "2014-10-00") == "2014-10-00"


class TestOneSeatAtATime:
    """`periods_overlap` / `combined_start`: nobody holds one post at one
    company twice at once, so two assertions of a position whose periods
    touch are one seat, and that seat began on the earlier day."""
    def setup_method(self):
        from app.roles import combined_start, periods_overlap
        self.overlap, self.start = periods_overlap, combined_start

    def test_an_open_seat_and_a_later_open_assertion_are_one_seat(self):
        assert self.overlap("2022-07-11", None, "2022-07-14", None)
        assert self.overlap("2022-07-14", None, "2022-07-11", None)

    def test_an_end_for_a_touching_period_belongs_to_the_open_seat(self):
        assert self.overlap("2022-07-11", None, "2022-07-14", "2025-03-01")

    def test_an_old_spell_does_not_touch_a_seat_begun_after_it_ended(self):
        assert not self.overlap("1998-01-01", "2001-08-01", "2011-04-04", None)
        assert not self.overlap("2011-04-04", None, "1998-01-01", "2001-08-01")

    def test_a_reassertion_dated_after_a_close_is_a_new_spell(self):
        assert not self.overlap("2011-04-04", "2015-08-10", "2016-01-01", None)

    def test_a_reassertion_dated_before_the_close_is_that_spell(self):
        assert self.overlap("2011-04-04", "2015-08-10", "2014-01-01", None)

    def test_an_undated_side_is_open_on_that_side(self):
        assert self.overlap(None, None, "2011-04-04", None)
        assert self.overlap("1998-01-01", "2001-08-01", None, None)
        assert self.overlap("1998-01-01", "2001-08-01", None, "1999-01-01")

    def test_a_coarse_date_touches_the_days_it_contains(self):
        assert self.overlap("2020-03", None, "2020", "2020")        # the year holds March
        assert self.overlap("2020-01-01", "2020-00-00", "2020-06-15", None)
        assert not self.overlap("2020-01-01", "2020-00-00", "2021-02-01", None)

    def test_the_combined_start_is_the_earlier_day(self):
        assert self.start("2022-07-14", "2022-07-11") == "2022-07-11"
        assert self.start("2022-07-11", "2022-07-14") == "2022-07-11"

    def test_the_combined_start_keeps_the_finer_of_one_date(self):
        assert self.start("2020-06-00", "2020-06-01") == "2020-06-01"
        assert self.start("2016-00-00", "2016-12-01") == "2016-12-01"

    def test_an_undated_side_yields(self):
        assert self.start(None, "2020-06-01") == "2020-06-01"
        assert self.start("2020-06-01", None) == "2020-06-01"
        assert self.start(None, None) is None
