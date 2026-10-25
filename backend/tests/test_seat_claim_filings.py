"""An SEC seat claim says which record stated it (`filing_type`, 2026-10-25):
the writers pass it, every call site in the runner passes it, and the heal
fills what an old claim can still tell about itself."""
import pathlib
import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app import roles
from app.scraper import sec_writer

RUNNER = pathlib.Path(__file__).resolve().parents[1] / "app" / "scraper" / "runner.py"


class TestFormLabel:
    @pytest.mark.parametrize("doc, label", [("4", "Form 4"), ("4/A", "Form 4"), ("3", "Form 3"),
                                            ("3/A", "Form 3"), (" 3 ", "Form 3"), (None, "Form 4"), ("", "Form 4")])
    def test_initial_statements_are_form_3_everything_else_form_4(self, doc, label):
        assert sec_writer.form_label(doc) == label


def _session(rows=()):
    session = MagicMock()
    session.run.return_value = list(rows)

    @contextmanager
    def _ctx():
        yield session
    return session, _ctx


class TestTheWritersPassIt:
    def test_a_seat_write_records_the_filing_on_its_claim(self):
        _, ctx = _session()
        with patch.object(sec_writer, "record_claim") as rc, patch.object(sec_writer.db, "get_session", ctx):
            sec_writer._upsert_role_sec("p1", "e1", "Director", "sec-edgar", source_url="u",
                                        source_date="2026-07-15", filing_type="Form 4")
        assert rc.call_args.kwargs["filing_type"] == "Form 4"
        assert rc.call_args.kwargs["role"] == "Director"

    def test_a_departure_records_the_filing_on_the_closing_claim(self):
        _, ctx = _session(rows=[{"role": "CEO"}])
        with patch.object(sec_writer, "record_claim") as rc, patch.object(sec_writer.db, "get_session", ctx):
            closed = sec_writer._close_role_sec("p1", "e1", "2026-01-01", role="CEO", source_id="sec-edgar",
                                                source_url="u", filing_type="8-K", read_from="narrative")
        assert closed == 1
        assert rc.call_args.kwargs["filing_type"] == "8-K" and rc.call_args.kwargs["until"] == "2026-01-01"

    def test_without_one_the_claim_says_nothing(self):
        _, ctx = _session()
        with patch.object(sec_writer, "record_claim") as rc, patch.object(sec_writer.db, "get_session", ctx):
            sec_writer._upsert_role_sec("p1", "e1", "Director", "sec-edgar")
        assert rc.call_args.kwargs["filing_type"] is None


class TestEveryRunnerCallSitePassesIt:
    _CALL = re.compile(r"_(?:upsert|close)_role_sec\((?P<args>.*?)\)\n", re.S)

    def test_each_seat_write_in_the_runner_names_its_filing(self):
        """The sibling-path rule: a Form D seat, a Form 3/4 seat, a "Former …"
        close and an 8-K close each say their record."""
        text = RUNNER.read_text()
        calls = [m.group("args") for m in self._CALL.finditer(text)]
        assert len(calls) >= 4
        assert all("filing_type=" in args for args in calls), [a[:60] for a in calls if "filing_type=" not in a]
        assert any('filing_type="Form D"' in a for a in calls)
        assert any('filing_type="8-K"' in a for a in calls)
        assert sum('form_label(exec_rec.get("document_type"))' in a for a in calls) == 2


class TestTheHeal:
    def _fake(self, counts):
        calls = []

        def run_sql(sql, params=None):
            calls.append(sql)
            if sql.startswith("UPDATE"):
                return [{"count": 1}]
            if "read_from = 'narrative'" in sql:
                return [{"n": counts["8k"]}]
            if "primary_doc.xml" in sql:
                return [{"n": counts["d"]}]
            return [{"n": counts["all"]}]
        return calls, run_sql

    def test_the_dry_run_counts_and_writes_nothing(self, monkeypatch):
        import app.db.arcadedb as arcadedb
        calls, fake = self._fake({"8k": 1, "d": 3, "all": 208})
        monkeypatch.setattr(arcadedb, "run_sql", fake)
        res = roles.heal_seat_claim_filings(apply=False)
        assert res == {"applied": False, "form_8k": 1, "form_d": 3, "unfilled": 204}
        assert not any(c.startswith("UPDATE") for c in calls)

    def test_apply_labels_each_kind_and_reports_what_is_left(self, monkeypatch):
        import app.db.arcadedb as arcadedb
        calls, fake = self._fake({"8k": 1, "d": 0, "all": 204})
        monkeypatch.setattr(arcadedb, "run_sql", fake)
        res = roles.heal_seat_claim_filings(apply=True)
        updates = [c for c in calls if c.startswith("UPDATE")]
        assert len(updates) == 1 and "SET filing_type = '8-K'" in updates[0]
        assert "source_id = 'sec-edgar'" in updates[0] and "filing_type IS NULL" in updates[0]
        assert res["unfilled"] == 204                                 # what the next scrape fills
