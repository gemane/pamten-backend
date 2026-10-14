"""Catching up the persons written before the scraper read their portrait
(Wikidata P18) and their English Wikipedia article (the sitelink):
`backfill_person_images` asks Wikidata in batches and fills blanks only."""
from unittest.mock import patch

from app.scraper import maintenance
from app.scraper.maintenance import backfill_person_images

THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Bill_Gates_2017.jpg/250px-Bill_Gates_2017.jpg"
ARTICLE = "https://en.wikipedia.org/wiki/Bill_Gates"


def _rows(n, **have):
    return [{"id": f"p{i}", "name": f"Person {i}", "qid": f"Q{i}",
             "image_url": have.get("image_url"), "wikipedia_url": have.get("wikipedia_url")}
            for i in range(n)]


class TestBackfillPersonImages:
    def test_fills_the_blank_from_p18_and_skips_persons_without_one(self):
        fetched = []
        def fetch(qids):
            fetched.append(set(qids))
            return {"Q0": {"image_url": THUMB}, "Q1": {"image_url": None}}   # Q2: no row at all
        with patch.object(maintenance, "run_query", return_value=_rows(3)), \
             patch.object(maintenance, "run_command") as cmd:
            res = backfill_person_images(fetch=fetch)
        assert res["candidates"] == 3 and res["filled"] == 1 and res["without_image"] == 2
        assert res["linked"] == 0
        assert res["changes"] == [{"id": "p0", "name": "Person 0", "image_url": THUMB}]
        assert fetched == [{"Q0", "Q1", "Q2"}]
        assert cmd.call_count == 1
        cypher, params = cmd.call_args.args
        assert "COALESCE(p.image_url, $u)" in cypher        # a blank only, never a replacement
        assert "COALESCE(p.wikipedia_url, $w)" in cypher
        assert params == {"id": "p0", "u": THUMB, "w": None}

    def test_the_article_is_stored_too_and_alone_when_there_is_no_portrait(self):
        def fetch(qids):
            return {"Q0": {"image_url": None, "wikipedia_url": ARTICLE},
                    "Q1": {"image_url": THUMB, "wikipedia_url": ARTICLE}}
        with patch.object(maintenance, "run_query", return_value=_rows(2)), \
             patch.object(maintenance, "run_command") as cmd:
            res = backfill_person_images(fetch=fetch)
        assert res["filled"] == 1 and res["linked"] == 2 and res["without_image"] == 1
        assert res["links"][0] == {"id": "p0", "name": "Person 0", "wikipedia_url": ARTICLE}
        assert cmd.call_args_list[0].args[1] == {"id": "p0", "u": None, "w": ARTICLE}
        assert cmd.call_args_list[1].args[1] == {"id": "p1", "u": THUMB, "w": ARTICLE}

    def test_what_a_person_already_has_is_neither_asked_for_nor_counted(self):
        """A person with a portrait but no article gets the article only; the
        fetched portrait is not written over the stored one, nor counted."""
        def fetch(qids):
            return {"Q0": {"image_url": "https://upload.wikimedia.org/other.jpg", "wikipedia_url": ARTICLE}}
        with patch.object(maintenance, "run_query", return_value=_rows(1, image_url=THUMB)), \
             patch.object(maintenance, "run_command") as cmd:
            res = backfill_person_images(fetch=fetch)
        assert res["filled"] == 0 and res["linked"] == 1 and res["without_image"] == 0
        assert cmd.call_args.args[1] == {"id": "p0", "u": None, "w": ARTICLE}

    def test_asks_only_persons_with_a_wikidata_id_lacking_a_pointer(self):
        with patch.object(maintenance, "run_query", return_value=[]) as q, \
             patch.object(maintenance, "run_command"):
            backfill_person_images()
        assert "p.wikidata_id IS NOT NULL AND (p.image_url IS NULL OR p.wikipedia_url IS NULL)" in q.call_args.args[0]

    def test_batches_and_limit(self):
        batches = []
        def fetch(qids):
            batches.append(len(qids))
            return {q: {"image_url": THUMB} for q in qids}
        with patch.object(maintenance, "run_query", return_value=_rows(120)), \
             patch.object(maintenance, "run_command") as cmd:
            res = backfill_person_images(limit=70, batch=50, fetch=fetch)
        assert batches == [50, 20]
        assert res["candidates"] == 70 and res["filled"] == 70 and cmd.call_count == 70

    def test_a_failed_batch_is_skipped_not_fatal(self):
        calls = []
        def fetch(qids):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("WDQS 429")
            return {q: {"image_url": THUMB} for q in qids}
        with patch.object(maintenance, "run_query", return_value=_rows(4)), \
             patch.object(maintenance, "run_command") as cmd:
            res = backfill_person_images(batch=2, fetch=fetch)
        assert res["filled"] == 2 and cmd.call_count == 2
