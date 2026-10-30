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


# The 2026-10-29b rebuild: the pre-2024 BlackRock filer, renamed "BlackRock
# Finance, Inc." on 2024-09-26, kept "BlackRock, Inc." among its aliases and
# carried Q219635; with both exact, the item id won every "BlackRock" lookup.
OLD_FILER = {"id": "sec:0001364742", "name": "BlackRock Finance, Inc.", "name_normalized": "blackrock finance",
             "wikidata_id": "Q219635", "name_credibility": 98,
             "aliases": ["BLK", "BlackRock, Inc.", "Blackrock Inc.", "New BlackRock, Inc."],
             "former_names": ["BlackRock Inc.|2024-09-26", "BlackRock, Inc.|2006-10-05",
                              "New BlackRock, Inc.|2006-09-29"]}
NEW_PARENT = {"id": "lei:529900VBK42Y5HHRMD23", "name": "BlackRock, Inc.", "name_normalized": "blackrock",
              "name_credibility": 92, "sec_cik": "0002012383"}


def test_the_name_in_force_beats_a_name_that_ended():
    for q in ("BlackRock", "BlackRock, Inc.", "BlackRock Inc."):
        assert _resolve(q, [OLD_FILER, NEW_PARENT])["id"] == NEW_PARENT["id"], q
        assert _resolve(q, [NEW_PARENT, OLD_FILER])["id"] == NEW_PARENT["id"], q


def test_on_a_day_before_the_rename_the_old_name_is_in_force_again():
    @contextmanager
    def session():
        yield MagicMock()
    with patch.object(search, "_entity_candidate_rows", return_value=[NEW_PARENT, OLD_FILER]), \
         patch.object(search.db, "get_session", session), \
         patch.object(search, "load_suppressed_nodes", return_value=set()):
        assert search.resolve_best_entity("BlackRock", on="2020-01-01")["id"] == OLD_FILER["id"]


def test_a_name_that_ended_still_finds_the_company_when_no_one_bears_it_now():
    meta = {"id": "sec:0001326801", "name": "Meta Platforms, Inc.", "name_normalized": "meta platforms",
            "aliases": ["Facebook Inc"], "former_names": ["Facebook Inc|2021-10-27"]}
    other = {"id": "x", "name": "Facebook Fans Club", "name_normalized": "facebook fans club"}
    assert _resolve("Facebook", [other, meta])["id"] == "sec:0001326801"
    assert search._rank(meta, "facebook", ["facebook"], 0, "facebook")[0] == 1


def test_a_gleif_previous_name_ranks_below_the_company_that_bears_it_now():
    # findings item 16a: the 2024 parent's GLEIF record lists "BLACKROCK FUNDING,
    # INC." as a previous legal name; it no longer names that company today
    parent = {"id": "lei:529900VBK42Y5HHRMD23", "name": "BlackRock, Inc.", "name_normalized": "blackrock",
              "gleif_former_names": ["BLACKROCK FUNDING, INC.|2024-09-30"]}
    funding = {"id": "x", "name": "BlackRock Funding, Inc.", "name_normalized": "blackrock funding"}
    assert search._rank(parent, "blackrock funding", ["blackrock", "funding"], 0, "blackrock funding")[0] == 1
    assert search._rank(funding, "blackrock funding", ["blackrock", "funding"], 1, "blackrock funding")[0] == 0
    # on a day it was still in force it is the company's name
    assert search._rank(parent, "blackrock funding", ["blackrock", "funding"], 0, "blackrock funding",
                        "2024-01-01")[0] == 0


def test_a_gleif_trading_name_is_a_name_in_force():
    node = {"id": "lei:X", "name": "Alphabet Holdings LLC", "name_normalized": "alphabet holdings",
            "gleif_trading_names": ["Google"]}
    assert search._rank(node, "google", ["google"], 0, "google")[0] == 0
