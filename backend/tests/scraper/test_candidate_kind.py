"""
A name is not a kind.

Searching Wikidata for "Steve Jobs" returns the 2015 film first, the book second
and the man third. Taking the top hit wrote a Danny Boyle picture into an
ownership graph as a company — `infer_entity_type` falls back to "company" for
any P31 it does not recognise — and, because that counted as success, the person
was never scraped at all.

So each path picks its own kind out of the candidates rather than trusting the
ranking. These are the real QIDs and the real order.
"""
from unittest.mock import patch

import pytest

from app.scraper import wikidata

# The actual first three hits for "Steve Jobs".
HITS = [
    {"id": "Q18754959", "label": "Steve Jobs", "description": "2015 film"},
    {"id": "Q16460065", "label": "Steve Jobs", "description": "book"},
    {"id": "Q19837", "label": "Steve Jobs", "description": "American entrepreneur"},
]

FACTS = {
    "Q18754959": {"instances": ["Q11424"], "is_human": False, "is_company": False},
    "Q16460065": {"instances": ["Q3331189"], "is_human": False, "is_company": False},
    "Q19837": {"instances": ["Q5"], "is_human": True, "is_company": False},
}


@pytest.fixture(autouse=True)
def _classified(monkeypatch):
    monkeypatch.setattr(wikidata, "classify_candidates", lambda qids: FACTS)


class TestPickingAPerson:
    def test_finds_the_man_behind_the_film_and_the_book(self):
        assert wikidata.pick_candidate(HITS, "person") == "Q19837"

    def test_finds_nobody_when_no_hit_is_human(self):
        assert wikidata.pick_candidate(HITS[:2], "person") is None


class TestPickingACompany:
    def test_refuses_a_film(self):
        # The bug exactly: something had to be written, so the film was.
        assert wikidata.pick_candidate(HITS, "company") is None

    def test_takes_a_company_when_there_is_one(self, monkeypatch):
        hits = [*HITS, {"id": "Q312", "label": "Apple Inc."}]
        monkeypatch.setattr(wikidata, "classify_candidates",
                            lambda qids: {**FACTS, "Q312": {"instances": ["Q4830453"],
                                                            "is_human": False,
                                                            "is_company": True}})
        assert wikidata.pick_candidate(hits, "company") == "Q312"

    def test_prefers_the_earlier_hit_when_several_qualify(self, monkeypatch):
        # The search ranking still decides among candidates of the right kind.
        hits = [{"id": "Q1"}, {"id": "Q2"}]
        monkeypatch.setattr(wikidata, "classify_candidates", lambda qids: {
            "Q1": {"instances": ["Q4830453"], "is_human": False, "is_company": True},
            "Q2": {"instances": ["Q4830453"], "is_human": False, "is_company": True}})
        assert wikidata.pick_candidate(hits, "company") == "Q1"


class TestNoHits:
    def test_nothing_to_pick_from(self):
        assert wikidata.pick_candidate([], "person") is None
        assert wikidata.pick_candidate([], "company") is None


class TestTheLabelTrap:
    """Steve Jobs has no English label — Wikidata moved names identical across
    languages to `mul` in 2024 — and asking for `en` alone made him look like
    not-a-person, which is a worse failure than a missing name."""

    def test_the_label_request_asks_for_mul_as_well(self, monkeypatch):
        seen = {}

        class _Resp:
            @staticmethod
            def json():
                return {"entities": {"Q19837": {"labels": {"mul": {"value": "Steve Jobs"}}}}}

        monkeypatch.setattr(wikidata, "_fetch_person_details",
                            lambda qids: {"Q19837": {"is_human": True, "aliases": []}})
        monkeypatch.setattr(wikidata, "_wd_get",
                            lambda url, params, timeout: seen.update(params) or _Resp())

        detail = wikidata.fetch_person_details_for("Q19837")
        assert "mul" in seen["languages"]
        assert detail["full_name"] == "Steve Jobs"

    def test_a_person_without_any_label_is_still_a_person(self, monkeypatch):
        class _Resp:
            @staticmethod
            def json():
                return {"entities": {"Q19837": {"labels": {}}}}

        monkeypatch.setattr(wikidata, "_fetch_person_details",
                            lambda qids: {"Q19837": {"is_human": True, "aliases": []}})
        monkeypatch.setattr(wikidata, "_wd_get", lambda url, params, timeout: _Resp())

        # The caller has the name from the search hit; humanity is what decides.
        detail = wikidata.fetch_person_details_for("Q19837")
        assert detail is not None and detail["is_human"] is True
