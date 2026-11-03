"""Tests for the pure name-matching helpers extracted from the scraper router."""

from app.scraper.names import (
    _name_words,
    _person_name_variants,
    _entity_name_variants,
)


def test_name_words_strips_punctuation():
    assert _name_words("Warren E. Buffett") == ["Warren", "E", "Buffett"]
    assert _name_words("Page, Larry") == ["Page", "Larry"]


def test_person_variants_reverses_two_word_names():
    assert "Brin Sergey" in _person_name_variants("Sergey Brin")


def test_person_variants_expands_nicknames():
    variants = _person_name_variants("Larry Page")
    assert "Page Larry" in variants          # reversal
    assert "Lawrence Page" in variants        # nickname -> formal
    assert "Page Lawrence" in variants        # formal, reversed


def test_person_variants_handle_three_word_names():
    variants = _person_name_variants("Warren E. Buffett")
    assert "Buffett Warren E" in variants
    assert "Warren Buffett" in variants


def test_person_variants_are_deduped_and_include_original():
    variants = _person_name_variants("Sergey Brin")
    assert variants[0] == "Sergey Brin"
    assert len(variants) == len(set(variants))


def test_entity_variants_strip_leading_the_and_punctuation():
    variants = _entity_name_variants("The Vanguard Group, Inc.")
    assert "Vanguard Group, Inc." in variants   # no "The"
    assert "Vanguard Group Inc" in variants      # no "The", no punctuation
    assert "The Vanguard Group Inc" in variants  # punctuation only


