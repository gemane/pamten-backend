"""
Tests for mapper.py — pure functions, no mocks needed.
"""

from app.scraper.mapper import (
    normalize_entity_name, is_person_name, is_nominee_name, derive_ownership_type,
)


class TestDeriveOwnershipType:
    """Ownership type is classified from the stake %; with no % it's 'unknown'
    (neither minority nor majority) — except a genuine SEC 13D/13G form signal."""

    def test_classifies_by_stake_percent(self):
        assert derive_ownership_type(99) == "full"
        assert derive_ownership_type(60) == "majority"
        assert derive_ownership_type(25) == "controlling"
        assert derive_ownership_type(6.2) == "minority"    # BlackRock in Alphabet
        assert derive_ownership_type(1.03) == "minority"   # a ~1% founder holding

    def test_no_stake_no_signal_is_unknown_not_majority(self):
        # The Alphabet bug: a Wikidata "owner"/founder edge with no % used to default
        # to 'majority'. With no disclosed stake and no form, it's 'unknown'.
        assert derive_ownership_type(None) == "unknown"
        assert derive_ownership_type(None, form_type=None) == "unknown"

    def test_sec_form_type_still_signals_when_no_stake(self):
        assert derive_ownership_type(None, "SC 13D") == "controlling"
        assert derive_ownership_type(None, "SC 13G") == "minority"


class TestNormalizeEntityName:
    """normalize_entity_name strips legal suffixes and lowercases for deduplication."""

    def test_strips_inc(self):
        assert normalize_entity_name("BlackRock, Inc.") == "blackrock"

    def test_strips_corp(self):
        assert normalize_entity_name("MICROSOFT CORP") == "microsoft"

    def test_strips_corporation(self):
        assert normalize_entity_name("Tesla Corporation") == "tesla"

    def test_strips_llc(self):
        assert normalize_entity_name("FMR LLC") == "fmr"

    def test_strips_ltd(self):
        assert normalize_entity_name("Baillie Gifford & Co Ltd") == "baillie gifford &"

    def test_already_normalized(self):
        assert normalize_entity_name("blackrock") == "blackrock"

    def test_collapses_whitespace(self):
        assert normalize_entity_name("Vanguard  Group  Inc") == "vanguard group"

    def test_removes_commas_and_periods(self):
        assert normalize_entity_name("Apple, Inc.") == "apple"

    def test_same_company_different_legal_forms(self):
        # The cross-source deduplication promise: all three normalize to the same string
        assert normalize_entity_name("BlackRock, Inc.") \
            == normalize_entity_name("BLACKROCK INC") \
            == normalize_entity_name("BlackRock")

    def test_empty_string(self):
        assert normalize_entity_name("") == ""


class TestIsPersonName:
    """is_person_name heuristic: 2–4 capitalised words, no digits, no legal suffixes."""

    def test_person_two_words(self):
        assert is_person_name("Elon Musk") is True

    def test_person_three_words(self):
        assert is_person_name("Timothy D Cook") is True

    def test_entity_has_suffix(self):
        assert is_person_name("BlackRock Inc") is False

    def test_entity_has_fund_suffix(self):
        assert is_person_name("Vanguard Fund") is False

    def test_entity_all_caps(self):
        # VANGUARD GROUP INC — has suffix, so False
        assert is_person_name("VANGUARD GROUP INC") is False

    def test_single_word(self):
        assert is_person_name("Tesla") is False

    def test_five_words(self):
        assert is_person_name("Jean Claude Van Damme Actor") is False

    def test_name_with_digit(self):
        assert is_person_name("John Smith 2nd") is False

    def test_empty(self):
        assert is_person_name("") is False


class TestIsNomineeName:
    def test_nominee_vehicles(self):
        assert is_nominee_name("TALBOT NOMINEES LIMITED") is True
        assert is_nominee_name("UBS Nominees Pty Ltd") is True
        assert is_nominee_name("Coach Nominee Inc.") is True

    def test_custodian_and_cede(self):
        assert is_nominee_name("Stichting SF0 Custodian") is True
        assert is_nominee_name("Global Custody Services Ltd") is True
        assert is_nominee_name("Cede & Co") is True
        assert is_nominee_name("Cede and Co.") is True

    def test_ordinary_companies_are_not_nominees(self):
        assert is_nominee_name("BlackRock, Inc.") is False
        assert is_nominee_name("Apple Inc") is False
        assert is_nominee_name("State Street Corporation") is False   # the bank itself, not its nominee
        assert is_nominee_name("") is False
        assert is_nominee_name(None) is False
    def test_new_categories(self):
        from app.scraper.mapper import infer_entity_type
        assert infer_entity_type(["Q1802419"]) == "government"   # state government
        assert infer_entity_type(["Q327333"]) == "government"    # government agency
        assert infer_entity_type(["Q157031"]) == "foundation"
        assert infer_entity_type(["Q845477"]) == "fund"          # ETF
        assert infer_entity_type(["Q791974"]) == "fund"          # mutual fund
        assert infer_entity_type(["Q79913"]) == "nonprofit"      # NGO
        assert infer_entity_type(["Q48204"]) == "nonprofit"      # voluntary association

    def test_sovereign_wealth_fund_is_fund_not_government(self):
        # Regression: Q1061648 (sovereign wealth fund, e.g. Mubadala Investment
        # Company) was mis-mapped to government despite being an investment vehicle.
        from app.scraper.mapper import infer_entity_type
        assert infer_entity_type(["Q1061648"]) == "fund"

    def test_fund_wins_over_government_when_both_present(self):
        # Kuwait Investment Authority is P31 both government agency (Q327333) and
        # sovereign wealth fund (Q1061648) — the fund role should win.
        from app.scraper.mapper import infer_entity_type
        assert infer_entity_type(["Q327333", "Q1061648"]) == "fund"
        # a pure government body (no fund P31) stays government
        assert infer_entity_type(["Q327333"]) == "government"

    def test_existing_categories_unchanged(self):
        from app.scraper.mapper import infer_entity_type
        assert infer_entity_type(["Q4830453"]) == "company"
        assert infer_entity_type(["Q219577"]) == "holding"
        assert infer_entity_type(["Q431289"]) == "brand"
        assert infer_entity_type([]) == "company"
        assert infer_entity_type(["Q999999999"]) == "company"    # unknown → default

    def test_priority_specific_over_company(self):
        from app.scraper.mapper import infer_entity_type
        # an entity that is both a foundation and a generic company → foundation
        assert infer_entity_type(["Q783794", "Q157031"]) == "foundation"
        assert infer_entity_type(["Q4830453", "Q845477"]) == "fund"
        assert infer_entity_type(["Q783794", "Q1802419"]) == "government"
