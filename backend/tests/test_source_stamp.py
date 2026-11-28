"""A record's `source_id` names who wrote it FIRST — on every write path.

A scrape fills it only where it is empty (`graph_writer`); the bulk writer and
the GLEIF delta's placeholder overwrote it, so GLEIF's import stamped itself on
a company EDGAR had created (the user, 2026-11-28).
"""
from unittest.mock import patch

import pytest


@pytest.mark.usefixtures("fake_arcadedb")
def test_the_bulk_writer_fills_the_source_only_where_it_is_empty():
    from app.scraper.bulk_import import _BatchWriter
    flushes: list = []
    with patch("app.scraper.bulk_import.run_sqlscript",
               side_effect=lambda script, params=None: flushes.append((script, params))):
        b = _BatchWriter(batch_size=100)
        b.entity("lei:1", {"name": "A", "source_id": "gleif"})
        b.person("p:1", {"full_name": "C", "source_id": "uk-psc"})
        b.flush()
    script = "\n".join(s for s, _ in flushes)
    assert script.count("source_id = COALESCE(source_id, :source_id__") == 2   # both labels
    assert "source_id = :" not in script                # never a plain overwrite
    assert "name = :name__0" in script                  # the other fields are written as before
    assert flushes[0][1]["source_id__0"] == "gleif"


def test_the_delta_placeholder_fills_the_source_only_where_it_is_empty():
    from app.scraper import gleif_incremental
    seen: list = []
    with patch.object(gleif_incremental, "canonical_id", side_effect=lambda i: i), \
         patch.object(gleif_incremental, "run_sql", side_effect=lambda q, p=None: seen.append(q)):
        assert gleif_incremental._ensure_lei_node("L" * 20, "gleif") == "lei:" + "L" * 20
    assert "source_id = COALESCE(source_id, :src)" in seen[0]
    assert "source_id = :src" not in seen[0]
