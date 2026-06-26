"""
Tests for mapper.py — pure functions, no mocks needed.
"""

import pytest
from app.scraper.mapper import normalize_entity_name, is_person_name


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
