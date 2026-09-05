"""The public source catalogue: what the platform draws on, and how far to trust it.

`GET /scraper/sources` is unauthenticated — where the data comes from is the case
for the whole platform, not something to keep behind a login. These pin the
catalogue's contract with the UI, and that the metadata has exactly one home.
"""
from unittest.mock import patch

import pytest

from app.scraper.sources import KNOWN_SOURCES

EXPECTED = {"wikidata", "sec_edgar", "open_corporates", "bods_gleif", "bods_uk_psc"}


class TestCatalogueMetadata:
    def test_every_source_is_fully_described(self):
        for name, meta in KNOWN_SOURCES.items():
            for field in ("kind", "label", "url", "credibility", "quality",
                          "description", "region", "coverage"):
                assert meta.get(field) not in (None, ""), f"{name} is missing {field}"

    def test_every_source_states_its_region_from_the_known_set(self):
        # The coverage page renders these as tags; free-form drift would
        # produce a tag zoo. "200+ jurisdictions" is OpenCorporates' honest
        # special case.
        allowed = {"Global", "US", "GB", "200+ jurisdictions"}
        for name, meta in KNOWN_SOURCES.items():
            assert meta["region"] in allowed, name

    def test_urls_are_absolute_links_to_the_source(self):
        for name, meta in KNOWN_SOURCES.items():
            assert meta["url"].startswith("https://"), name

    def test_credibility_is_a_usable_ranking_score(self):
        # These are the tie-breakers app/claims.py ranks conflicting claims by.
        for name, meta in KNOWN_SOURCES.items():
            assert 0 < meta["credibility"] <= 100, name

    def test_statutory_sources_outrank_community_ones(self):
        """The ordering is the point — a legally mandated filing must beat a
        community-edited knowledge base when the two disagree."""
        assert KNOWN_SOURCES["sec_edgar"]["credibility"] > KNOWN_SOURCES["wikidata"]["credibility"]
        assert KNOWN_SOURCES["bods_uk_psc"]["credibility"] > KNOWN_SOURCES["wikidata"]["credibility"]

    def test_quality_bands_are_from_a_known_set(self):
        assert {m["quality"] for m in KNOWN_SOURCES.values()} <= {
            "statutory", "official", "aggregated", "community"}

    def test_kinds_are_known(self):
        assert {m["kind"] for m in KNOWN_SOURCES.values()} <= {"instant", "bulk"}


class TestRunnerReadsTheCatalogue:
    """The provenance stamped onto scraped data and the public catalogue must be
    the same values — they used to be separate constants that could drift."""

    def test_runner_constants_come_from_the_catalogue(self):
        from app.scraper import runner

        assert runner.WIKIDATA_CREDIBILITY == KNOWN_SOURCES["wikidata"]["credibility"]
        assert runner.SEC_EDGAR_SOURCE_URL == KNOWN_SOURCES["sec_edgar"]["url"]
        assert runner.GLEIF_SOURCE_NAME == KNOWN_SOURCES["bods_gleif"]["label"]
        assert runner.BODS_UK_PSC_CREDIBILITY == KNOWN_SOURCES["bods_uk_psc"]["credibility"]


class TestListSources:
    def _rows(self):
        from app.scraper import sources

        records = [{"name": n, "enabled": True, "description": m["description"],
                    "kind": m["kind"], "data_mode": None}   # pre-field rows read as full
                   for n, m in KNOWN_SOURCES.items()]
        session = type("S", (), {"run": lambda self, *a, **k: records})()
        ctx = type("C", (), {"__enter__": lambda s: session, "__exit__": lambda *a: False})()
        with patch.object(sources, "_ensure_sources"), \
             patch.object(sources.db, "get_session", return_value=ctx):
            return sources.list_sources()

    def test_serves_the_catalogue_fields(self):
        row = next(r for r in self._rows() if r["name"] == "sec_edgar")
        assert row["label"] == "SEC EDGAR"
        assert row["url"] == "https://www.sec.gov/edgar"
        assert row["credibility"] == 98
        assert row["quality"] == "statutory"
        assert row["region"] == "US"
        assert row["data_mode"] == "full", "a null mode reads as full — opt-in restriction"
        assert "13F" in row["coverage"]

    def test_lists_every_source_including_the_bulk_ones(self):
        # Both bulk sources are toggled off, yet their data is loaded and in use —
        # the catalogue describes what the platform draws on, not what is running.
        assert {r["name"] for r in self._rows()} == EXPECTED

    @pytest.mark.parametrize("field", ["name", "enabled", "kind", "label", "url",
                                       "credibility", "quality", "description",
                                       "region", "coverage", "data_mode"])
    def test_every_row_carries_every_field_the_ui_reads(self, field):
        for row in self._rows():
            assert field in row
