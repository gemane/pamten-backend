"""
Searching Wikidata inside one country.

The country is part of the question, not a filter over the answer: `haswbstatement:P17`
asks the index only for items in that country. That is the difference between finding
Alphabet Fuhrparkmanagement — the German company called Alphabet — and finding nothing,
because it is nowhere near the global top hits for "Alphabet" and never will be.

What still has to be decided locally is *which* of the country's matches is meant. The
restricted search ranks by text relevance over the whole item, and reaches much deeper
than a global one, so the ranking here is doing real work. Every case below is one the
live API actually produced.
"""
from unittest.mock import patch

import pytest

from app.scraper import wikidata
from app.scraper.wikidata import best_match, country_item, rank_by_name, search_entity_in_country


def _api(responses):
    """Patch `_wd_get` with a queue of JSON payloads, in call order."""
    queue = list(responses)

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    return patch.object(wikidata, "_wd_get", side_effect=lambda *a, **k: _Resp(queue.pop(0)))


def _search(*qids):
    return {"query": {"search": [{"title": q} for q in qids]}}


def _entities(names):
    """names: {qid: [label, *aliases]}"""
    return {"entities": {
        qid: {"labels": {"en": {"value": n[0]}},
              "aliases": {"en": [{"value": a} for a in n[1:]]}}
        for qid, n in names.items()}}


@pytest.fixture(autouse=True)
def _no_cache():
    country_item.cache_clear()
    yield
    country_item.cache_clear()


class TestFindingTheCountry:
    def test_looks_the_country_up_by_its_iso_code(self):
        with _api([_search("Q183")]):
            assert country_item("DE") == "Q183"

    def test_is_case_insensitive(self):
        with _api([_search("Q183")]):
            assert country_item("de") == "Q183"

    def test_rejects_anything_that_is_not_an_iso_2_code(self):
        # No request at all — an empty queue would raise if one were made.
        with _api([]):
            assert country_item("Germany") is None
            assert country_item("D") is None
            assert country_item("") is None

    def test_a_country_wikidata_does_not_know_is_not_a_search(self):
        with _api([{"query": {"search": []}}]):
            assert country_item("ZZ") is None


class TestTheSearchItself:
    def test_asks_the_index_for_that_country_only(self):
        with _api([_search("Q183"), _search("Q2650924"),
                   _entities({"Q2650924": ["Alphabet Fuhrparkmanagement"]})]) as api:
            hits = search_entity_in_country("Alphabet", "DE")
        srsearch = api.call_args_list[1][0][1]["srsearch"]
        assert 'haswbstatement:P17=Q183' in srsearch      # the country is IN the query
        assert 'inlabel:"Alphabet"' in srsearch           # …and the text matches names
        assert [h["id"] for h in hits] == ["Q2650924"]

    def test_nothing_in_that_country_is_an_empty_answer(self):
        with _api([_search("Q142"), {"query": {"search": []}}]):
            assert search_entity_in_country("Alphabet", "FR") == []

    def test_an_unknown_country_searches_nothing(self):
        with _api([{"query": {"search": []}}]):
            assert search_entity_in_country("Alphabet", "ZZ") == []


class TestChoosingAmongOneCountrysMatches:
    def _hits(self, names, query, country="DE"):
        with _api([_search("Qc"), _search(*names.keys()), _entities(names)]):
            return search_entity_in_country(query, country)

    def test_the_company_named_beats_a_competition_named_after_it(self):
        # Real: "barclays" in the UK returns the Premier League first — it was the
        # Barclays Premier League and the alias is still on the item.
        hits = self._hits({"Q9448": ["Premier League", "Barclays Premier League"],
                           "Q245343": ["Barclays"]}, "Barclays", "GB")
        assert hits[0]["id"] == "Q245343"

    def test_an_item_actually_called_that_beats_one_that_answers_to_it(self):
        # Real: the cycling team labelled "T-Mobile" raced as Deutsche Telekom, so
        # it carries an exact alias — same tier, same length, and it came first.
        hits = self._hits({"Q897228": ["T-Mobile", "Deutsche Telekom"],
                           "Q9396": ["Deutsche Telekom", "DTAG"]}, "Deutsche Telekom")
        assert hits[0]["id"] == "Q9396"

    def test_the_matching_name_is_what_gets_measured_not_the_label(self):
        # Real: an Azure region labelled "westindia" carries "Microsoft Azure West
        # India" as an alias. Ranking on the nine-character label beat the fifteen
        # of "Microsoft India".
        hits = self._hits({"Q110188694": ["westindia", "Microsoft Azure West India"],
                           "Q6840143": ["Microsoft India"]}, "Microsoft", "IN")
        assert hits[0]["id"] == "Q6840143"

    def test_accents_do_not_decide(self):
        hits = self._hits({"Q7030863": ["Nestle Nido"], "Q160746": ["Nestlé"]}, "Nestle", "CH")
        assert hits[0]["id"] == "Q160746"

    def test_a_legal_suffix_does_not_decide(self):
        hits = self._hits({"Q1156938": ["Alphabet City"], "Q20800404": ["Alphabet Inc."]},
                          "Alphabet", "US")
        assert hits[0]["id"] == "Q20800404"

    def test_an_alias_only_match_still_counts(self):
        # Searching "DTAG" has to reach Deutsche Telekom, whose label is not that.
        hits = self._hits({"Q9396": ["Deutsche Telekom", "DTAG"]}, "DTAG")
        assert [h["id"] for h in hits] == ["Q9396"]

    def test_and_the_aliases_are_actually_asked_for(self):
        # The mock hands back whatever it is given, so the test above passes even
        # if the request never asks Wikidata for aliases. This is the assertion
        # that keeps it honest.
        with _api([_search("Qc"), _search("Q9396"),
                   _entities({"Q9396": ["Deutsche Telekom", "DTAG"]})]) as api:
            search_entity_in_country("DTAG", "DE")
        props = api.call_args_list[2][0][1]["props"]
        assert "aliases" in props


class TestItHasToBeCalledWhatYouTyped:
    """The country search reaches deep, and a country with no company by that name
    starts offering whatever else it has.

    Real: "Alphabet" in France comes back with a breast-cancer trial whose acronym
    is ALPHABET. Nothing is a truer answer than that.
    """

    def _hits(self, names, query):
        with _api([_search("Q142"), _search(*names.keys()), _entities(names)]):
            return search_entity_in_country(query, "FR")

    def test_a_name_that_merely_contains_the_query_is_dropped(self):
        assert self._hits({"Q113928025": ["Trastuzumab + ALpelisib … (ALPHABET)"]},
                          "Alphabet") == []

    def test_a_name_that_begins_with_the_query_is_kept(self):
        hits = self._hits({"Q97621257": ["Alphabet Brewing Company"]}, "Alphabet")
        assert [h["id"] for h in hits] == ["Q97621257"]

    def test_an_item_with_no_english_name_at_all_is_dropped(self):
        assert self._hits({"Q125981955": [""]}, "Alphabet") == []


class TestTheRankingRule:
    """`rank_by_name` on its own — it is also what a future source can reuse."""

    def item(self, *names):
        return {"id": names[0], "names": list(names), "label": names[0], "order": 0}

    def test_exact_beats_starts_with_beats_contains(self):
        cands = [self.item("The Acme Group"), self.item("Acme Industries"), self.item("Acme")]
        assert [c["id"] for c in rank_by_name(cands, "Acme")] == [
            "Acme", "Acme Industries", "The Acme Group"]

    def test_best_match_reports_the_tier_and_the_matched_length(self):
        assert best_match({"names": ["Acme"]}, "Acme") == (0, 4)
        assert best_match({"names": ["Acme Industries"]}, "Acme") == (1, 15)
        assert best_match({"names": ["Nothing here"]}, "Acme") == (3, 0)
