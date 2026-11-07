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
    assert search._rank(meta, "facebook", ["facebook"], 0, "facebook")[0] == 2   # ended: below an alias in force


def test_a_gleif_previous_name_ranks_below_the_company_that_bears_it_now():
    # findings item 16a: the 2024 parent's GLEIF record lists "BLACKROCK FUNDING,
    # INC." as a previous legal name; it no longer names that company today
    parent = {"id": "lei:529900VBK42Y5HHRMD23", "name": "BlackRock, Inc.", "name_normalized": "blackrock",
              "gleif_former_names": ["BLACKROCK FUNDING, INC.|2024-09-30"]}
    funding = {"id": "x", "name": "BlackRock Funding, Inc.", "name_normalized": "blackrock funding"}
    assert search._rank(parent, "blackrock funding", ["blackrock", "funding"], 0, "blackrock funding")[0] == 2
    assert search._rank(funding, "blackrock funding", ["blackrock", "funding"], 1, "blackrock funding")[0] == 0
    # on a day it was still in force it is the company's name
    assert search._rank(parent, "blackrock funding", ["blackrock", "funding"], 0, "blackrock funding",
                        "2024-01-01")[0] == 0


def test_a_gleif_trading_name_is_a_name_in_force():
    node = {"id": "lei:X", "name": "Alphabet Holdings LLC", "name_normalized": "alphabet holdings",
            "gleif_trading_names": ["Google"]}
    assert search._rank(node, "google", ["google"], 0, "google")[0] == 0


# Findings item 54 (2026-11-05): Wikidata's one item for T. Rowe Price carries
# the adviser's name as an alias and points at the Group's CIK; the adviser is a
# legal entity of its own (CIK 80255, the 13G/13F filer). With both exact, the
# Group won on `notable`, so "T. Rowe Price Associates, Inc." meant the Group
# whenever Wikidata had answered and the adviser when it had not.
GROUP = {"id": "sec:0001113169", "name": "PRICE T ROWE GROUP INC", "name_normalized": "price t rowe group",
         "aliases": ["T. Rowe Price Associates, Inc.", "T. Rowe Price Group, Inc."],
         "wikidata_id": "Q3511946", "name_credibility": 98}
# EDGAR's own spelling for the filer is surname-first; the natural spelling it
# was first written under stays an alias (the register's name replaced it)
ASSOCIATES = {"id": "sec:0000080255", "name": "PRICE T ROWE ASSOCIATES INC /MD/",
              "name_normalized": "price t rowe associates", "aliases": ["T. Rowe Price Associates, Inc."],
              "name_credibility": 98}


def test_the_node_that_bears_the_name_beats_the_node_that_has_it_as_an_alias():
    for rows in ([GROUP, ASSOCIATES], [ASSOCIATES, GROUP]):
        assert _resolve("T. Rowe Price Associates, Inc.", rows)["id"] == ASSOCIATES["id"]
        # the alias still finds the Group when no node bears the name itself
        assert _resolve("T. Rowe Price Group, Inc.", rows)["id"] == GROUP["id"]


# findings item 75 (2026-11-07): Q775031 states no LEI, so Wikidata made its
# own node; with only its name equal to the query it beat GLEIF's Grupo Bimbo
# S.A.B. (45 holdings) — and the subsidiary in between rose when the twin was
# moved instead of the register's node (the guard's catch).
TWIN = {"id": "wd:Q775031", "name": "Grupo Bimbo", "name_normalized": "grupo bimbo", "wikidata_id": "Q775031"}
UK = {"id": "lei:984500FEES8936E86D53", "name": "GRUPO BIMBO UK LIMITED", "name_normalized": "grupo bimbo uk",
      "lei_id": "984500FEES8936E86D53", "legal_form_code": "H0PO"}
SAB = {"id": "lei:5493000RIXURZEBFEV60", "name": "GRUPO BIMBO S A B DE C V",
       "name_normalized": "grupo bimbo sab de cv", "lei_id": "5493000RIXURZEBFEV60", "legal_form_code": "L9JC"}


def test_the_register_s_node_of_the_name_comes_before_a_wikidata_only_twin():
    assert _resolve("Grupo Bimbo", [TWIN, UK, SAB])["id"] == SAB["id"]
    assert [n["id"] for n in search._register_before_twin([TWIN, UK, SAB])] == [SAB["id"], TWIN["id"], UK["id"]]


def test_the_name_is_compared_without_the_register_s_legal_form():
    assert search._base_name_key(SAB) == search._base_name_key(TWIN) == "grupobimbo"
    assert search._base_name_key(UK) != "grupobimbo"


def test_a_twin_without_a_register_namesake_stays_where_it_ranks():
    dangote = {"id": "wd:Q3013553", "name": "Dangote Group", "name_normalized": "dangote group"}
    industries = {"id": "lei:0292007042B6O5C1G755", "name": "DANGOTE INDUSTRIES LIMITED",
                  "name_normalized": "dangote industries", "lei_id": "0292007042B6O5C1G755"}
    assert _resolve("Dangote Group", [dangote, industries])["id"] == dangote["id"]


def test_a_wikidata_node_with_a_register_id_is_no_twin():
    keyed = {**TWIN, "id": "wd:Q775031", "lei_id": "5493000RIXURZEBFEV60"}
    assert search._register_before_twin([keyed, UK, SAB])[0] is keyed
