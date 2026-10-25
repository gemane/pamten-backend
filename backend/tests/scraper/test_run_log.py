"""The scrape-run log (app.scraper.run_log) stores what it knows and nothing
in place of what it does not: no `''` placeholders, and every per-company
runner names the company it resolved."""
import pathlib
import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.scraper import run_log as m

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def _session():
    session = MagicMock()

    @contextmanager
    def _ctx():
        yield session
    return session, _ctx


class TestNoPlaceholders:
    def test_a_new_row_carries_neither_an_error_nor_a_company_nor_an_end(self):
        session, ctx = _session()
        with patch.object(m.db, "get_session", ctx):
            m._safe_create("r1", "wikidata", "Acme", reason=None)
        query, kw = session.run.call_args.args[0], session.run.call_args.kwargs
        assert "''" not in query
        for absent in ("entity_id", "finished_at", "error"):
            assert absent not in query
        assert kw["why"] is None                       # no reason is no reason, not ""
        assert kw["tgt"] == "Acme" and kw["src"] == "wikidata"

    def test_a_reason_is_kept_when_given(self):
        session, ctx = _session()
        with patch.object(m.db, "get_session", ctx):
            m._safe_create("r1", "wikidata", "Acme", reason="forced")
        assert session.run.call_args.kwargs["why"] == "forced"

    def test_a_clean_finish_stores_no_error_and_no_company_when_none_resolved(self):
        session, ctx = _session()
        with patch.object(m.db, "get_session", ctx), patch.object(m, "_prune"):
            m._safe_finish("r1", "ok", 3, "", None)
        kw = session.run.call_args.kwargs
        assert kw["err"] is None and kw["eid"] is None and kw["tot"] == 3

    def test_a_failure_and_a_company_are_stored_as_given(self):
        session, ctx = _session()
        with patch.object(m.db, "get_session", ctx), patch.object(m, "_prune"):
            m._safe_finish("r1", "failed", 0, "boom", "lei:1")
        kw = session.run.call_args.kwargs
        assert kw["err"] == "boom" and kw["eid"] == "lei:1"

    def test_the_block_hands_the_company_to_the_finish(self):
        with patch.object(m, "_safe_create") as create, patch.object(m, "_safe_finish") as finish:
            with m.record_run("sec_edgar", "Acme") as run:
                run["entity_id"] = "lei:1"
                run["total"] = 2
        assert create.call_args.args[1:] == ("sec_edgar", "Acme", None)
        assert finish.call_args.args[1:] == ("ok", 2, "", "lei:1")


class TestHeal:
    def test_counts_each_column_and_removes_only_on_apply(self):
        calls = []

        def fake(sql, params=None):
            calls.append(sql)
            return [{"n": 7 if "entity_id" in sql else 0}]
        with patch("app.db.arcadedb.run_sql", fake):
            dry = m.heal_placeholders(apply=False)
            assert dry == {"applied": False, "entity_id": 7, "finished_at": 0, "error": 0, "reason": 0}
            assert not any(c.startswith("UPDATE") for c in calls)
            calls.clear()
            res = m.heal_placeholders(apply=True)
        assert res["entity_id"] == 7
        updates = [c for c in calls if c.startswith("UPDATE")]
        assert updates == ["UPDATE ScrapeRun REMOVE entity_id WHERE entity_id = ''"]   # only the column that had any


#: Runs that are not about one company: a bulk file, a delta, a snapshot.
BULK_SOURCES = {"ch-psc-update", "gleif-rr-status", "gleif-rr-stakes", "gleif-rr-validation",
                "gleif-update"}
_BLOCK = re.compile(r"^(?P<indent>[ \t]*)with (?:ExitStack\(\) as stack, )?record_run\((?P<src>[^,]+),.*\) as run:\n",
                    re.M)


def _run_blocks():
    """Every `with record_run(...) as run:` block in the application, with the
    source expression and the block's text (the lines indented deeper)."""
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text()
        for match in _BLOCK.finditer(text):
            indent = len(match.group("indent"))
            body = []
            for line in text[match.end():].splitlines():
                if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                    break
                body.append(line)
            yield str(path.relative_to(APP)), match.group("src").strip(), "\n".join(body)


class TestEveryCompanyRunNamesItsCompany:
    def test_the_blocks_are_found(self):
        srcs = {src for _, src, _ in _run_blocks()}
        assert '"sec-ex21"' in srcs and '"all"' in srcs and "spec.name" in srcs

    def test_each_per_company_block_sets_entity_id(self):
        """A block that scrapes one company must say which: the weekly digest
        names the company by it and the dev log had 271 of 298 rows without
        one (2026-10-25). Bulk runs have no single company and are exempt."""
        missing = [(f, src) for f, src, body in _run_blocks()
                   if src.strip("\"'") not in BULK_SOURCES and 'run["entity_id"]' not in body]
        assert missing == []
