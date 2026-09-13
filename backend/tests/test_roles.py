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
