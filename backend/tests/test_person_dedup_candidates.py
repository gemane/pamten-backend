"""The scoped dedup's fetch for spellings LONGER than a seed's (findings item 55).

`_name_lookup_variants` can only shorten a name, so a seed written under the
shorter spelling — Wikidata's "Lachlan Murdoch" — never retrieved EDGAR's
"Lachlan K. Murdoch". `_fulltext_name_candidates` asks the full-text index for
the surname as a word and narrows by the given name. These tests pin the query
it sends and the names it refuses to send one for; the real-index behaviour is
in tests/integration/test_person_autodedup_it.py.
"""
import pytest

from app.routers import persons


@pytest.fixture
def sql(monkeypatch):
    calls: list[dict] = []

    def fake_run_sql(query, params=None):
        calls.append({"query": query, **(params or {})})
        return [{"id": "sec:1", "full_name": "Lachlan K. Murdoch", "aliases": []}]

    monkeypatch.setattr(persons, "run_sql", fake_run_sql)
    return calls


def test_the_surname_is_the_indexed_word_and_the_given_name_the_filter(sql):
    rows = persons._fulltext_name_candidates("Lachlan Murdoch")
    assert [r["id"] for r in rows] == ["sec:1"]
    assert sql[0]["q"] == "murdoch" and sql[0]["pat"] == "%lachlan%"
    assert "CONTAINSTEXT :q" in sql[0]["query"] and "LIKE :pat" in sql[0]["query"]
    assert f"LIMIT {persons._FULLTEXT_CANDIDATE_CAP}" in sql[0]["query"]


def test_a_middle_name_and_an_honorific_change_nothing(sql):
    # Wikidata's "Robert James Thomson" must reach EDGAR's "Robert J Thomson"
    persons._fulltext_name_candidates("Dr. Robert James Thomson")
    assert (sql[0]["q"], sql[0]["pat"]) == ("thomson", "%robert%")


def test_edgar_word_order_asks_for_the_same_two_words(sql):
    # both words must be present either way, so the fetch is symmetric
    persons._fulltext_name_candidates("Murdoch Lachlan")
    assert (sql[0]["q"], sql[0]["pat"]) == ("lachlan", "%murdoch%")


def test_an_initial_is_not_a_given_name(sql):
    # "J. Smith" would fetch every Smith — the scorer keeps such an initial for
    # the same reason (`_name_key`), so no query is sent
    assert persons._fulltext_name_candidates("J. Smith") == []
    assert persons._fulltext_name_candidates("Madonna") == []
    assert persons._fulltext_name_candidates("") == []
    assert sql == []


def test_a_generational_mark_is_not_the_surname(sql):
    persons._fulltext_name_candidates("Thomas S Murphy Jr")
    assert (sql[0]["q"], sql[0]["pat"]) == ("murphy", "%thomas%")


def test_the_pattern_carries_only_letters_and_digits(sql):
    # the LIKE wildcards cannot come from a name: punctuation splits words
    persons._fulltext_name_candidates("Shaquille O'Neal_%")
    assert (sql[0]["q"], sql[0]["pat"]) == ("neal", "%shaquille%")
