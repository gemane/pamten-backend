"""`resolve_best_entity` answers only with a credible match (2026-10-26):
the on-demand scrape acts on what it returns, and "Voting group · 8 parties"
resolved to Alibaba, whose graph the client drew around the group."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.routers import search


def _resolve(q, rows):
    @contextmanager
    def session():
        yield MagicMock()
    with patch.object(search, "_entity_candidate_rows", return_value=rows), \
         patch.object(search.db, "get_session", session), \
         patch.object(search, "load_suppressed_nodes", return_value=set()):
        return search.resolve_best_entity(q)


GROUPS = [{"id": "cg", "name": "Capital Group Companies", "name_normalized": "capital group companies"},
          {"id": "ali", "name": "Alibaba Group Holding Limited", "name_normalized": "alibaba group holding"},
          {"id": "mtn", "name": "MTN GROUP", "name_normalized": "mtn group"}]


def test_a_query_whose_words_no_candidate_carries_resolves_to_nothing():
    assert _resolve("Voting group · 8 parties", GROUPS) is None


def test_every_word_in_the_name_is_a_match():
    assert _resolve("Alibaba Group", GROUPS)["id"] == "ali"
    assert _resolve("berkshire", [{"id": "b", "name": "BERKSHIRE HATHAWAY INC.", "name_normalized": "berkshire hathaway"}])["id"] == "b"
    assert _resolve("Microsoft Corp", [{"id": "m", "name": "Microsoft Corporation", "name_normalized": "microsoft"}])["id"] == "m"


def test_the_normalised_name_or_an_alias_is_a_match():
    # the accent folds in the normaliser, not in the word test
    assert _resolve("Nestlé S.A.", [{"id": "n", "name": "NESTLE S.A.", "name_normalized": "nestle"}])["id"] == "n"
    assert _resolve("Google", [{"id": "a", "name": "Alphabet Inc.", "name_normalized": "alphabet",
                                "aliases": ["Google"]}])["id"] == "a"
    # a register's other name counts as a name
    assert _resolve("Samsung Electronics", [{"id": "s", "name": "삼성전자(주)", "name_normalized": "삼성전자",
                                             "other_names": ["SAMSUNG ELECTRONICS CO., LTD"]}])["id"] == "s"


def test_punctuation_is_not_a_word_and_nothing_is_nothing():
    assert _resolve("Johnson & Johnson", [{"id": "j", "name": "JOHNSON & JOHNSON", "name_normalized": "johnson johnson"}])["id"] == "j"
    assert _resolve("·", GROUPS) is None


def test_the_search_text_counts_as_it_is_what_the_index_matched():
    sx = {"id": "sx", "name": "Space Exploration Technologies Corp.", "name_normalized": "space exploration technologies",
          "search_text": "Space Exploration Technologies Corp. SpaceX"}
    assert _resolve("SpaceX", [sx])["id"] == "sx"
    # and a description sharing one word is still no match
    ali = {**GROUPS[1], "search_text": "Alibaba Group Holding Limited Chinese multinational technology group"}
    assert _resolve("Voting group · 8 parties", [ali]) is None
