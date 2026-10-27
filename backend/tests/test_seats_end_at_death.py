"""A seat ends no later than its holder's death (app.roles.end_seats_at_death,
2026-10-25), and a departure is cited beside the appointment, not over it
(app.claims.close_role_claim, `_END_KEPT`)."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app import claims, roles


class TestFounderIsNotASeat:
    @pytest.mark.parametrize("role", ["Founder", "Co-Founder", "founder", "Co-founder"])
    def test_founders_stay_open(self, role):
        assert roles.is_founder_role(role)

    @pytest.mark.parametrize("role", ["CEO", "Board Member", "Director", "Chairman", None])
    def test_every_other_seat_ends(self, role):
        assert not roles.is_founder_role(role)


class _Db:
    def __init__(self, person, seats):
        self.person, self.seats, self.updates = person, seats, []

    def run_sql(self, sql, params=None, timeout=None):
        if sql.startswith("UPDATE"):
            self.updates.append((sql, params))
            return [{"count": 1}]
        if "FROM Person" in sql:
            return [self.person] if self.person else []
        if "FROM HAS_ROLE WHERE until IS NULL AND @out.id" in sql:
            return self.seats
        return []


def _patch(monkeypatch, person, seats):
    import app.db.arcadedb as arcadedb
    db = _Db(person, seats)
    monkeypatch.setattr(arcadedb, "run_sql", db.run_sql)
    return db


class TestEndSeatsAtDeath:
    SEATS = [{"rid": "#74:1", "role": "Board Member", "since": "1965-05-10"},
             {"rid": "#74:2", "role": "Founder", "since": None},
             {"rid": "#74:3", "role": "Director", "since": "2022-12-19"},     # the source dates it after the death
             {"rid": "#74:4", "role": "CEO", "since": None}]

    def test_closes_every_seat_but_the_founders_and_the_one_dated_after(self, monkeypatch):
        db = _patch(monkeypatch, {"death_date": "2022-01-01", "wikidata_id": "Q47213"}, self.SEATS)
        assert roles.end_seats_at_death("p1") == {"closed": 2, "founders_kept": 1, "after_death": 1}
        assert [u[0] for u in db.updates] == ["UPDATE #74:1 SET until = :d, until_reason = :why, until_source_url = :u",
                                              "UPDATE #74:4 SET until = :d, until_reason = :why, until_source_url = :u"]
        assert db.updates[0][1] == {"d": "2022-01-01", "why": "death", "u": "https://www.wikidata.org/wiki/Q47213"}

    def test_without_a_wikidata_item_the_end_is_not_cited(self, monkeypatch):
        db = _patch(monkeypatch, {"death_date": "2022-01-01", "wikidata_id": None}, self.SEATS[:1])
        roles.end_seats_at_death("p1")
        assert db.updates[0][0] == "UPDATE #74:1 SET until = :d, until_reason = :why"
        assert "u" not in db.updates[0][1]

    def test_the_living_and_the_unknown_are_left_alone(self, monkeypatch):
        db = _patch(monkeypatch, {"death_date": None, "wikidata_id": "Q1"}, self.SEATS)
        assert roles.end_seats_at_death("p1") == {"closed": 0, "founders_kept": 0, "after_death": 0}
        db2 = _patch(monkeypatch, None, self.SEATS)
        assert roles.end_seats_at_death("missing")["closed"] == 0
        assert db.updates == [] and db2.updates == []

    def test_a_database_failure_does_not_fail_the_scrape(self, monkeypatch):
        import app.db.arcadedb as arcadedb

        def boom(sql, params=None, timeout=None):
            raise RuntimeError("down")
        monkeypatch.setattr(arcadedb, "run_sql", boom)
        assert roles.end_seats_at_death("p1") == {"closed": 0, "founders_kept": 0, "after_death": 0}


def _session(rows=()):
    session = MagicMock()
    run_mock = MagicMock()
    run_mock.single.return_value = None
    run_mock.__iter__.return_value = iter(list(rows))
    session.run.return_value = run_mock

    @contextmanager
    def _ctx():
        yield session
    return session, _ctx


class TestTheWritersApplyIt:
    def test_the_person_writer_applies_it_when_a_death_date_arrives(self):
        from app.scraper import runner
        session, ctx = _session()
        with patch("app.scraper.runner.db.get_session", ctx), patch.object(runner, "record_claim", create=True), \
             patch.object(runner, "end_seats_at_death") as rule:
            pid = runner._upsert_person("Paul Allen", None, None, "Q1", death_date="2018-10-15")
            assert rule.call_args.args == (pid,)
            rule.reset_mock()
            runner._upsert_person("Alive Person", None, None, "Q2")
            assert not rule.called

    def test_the_seat_writers_apply_it_to_a_seat_they_open(self):
        from app.scraper import graph_writer, sec_writer
        for module, write in ((graph_writer, lambda: graph_writer._upsert_role("p", "e", "CEO", "wikidata")),
                              (sec_writer, lambda: sec_writer._upsert_role_sec("p", "e", "CEO", "sec-edgar"))):
            _, ctx = _session()
            with patch.object(module, "record_claim"), patch.object(module.db, "get_session", ctx), \
                 patch.object(module, "end_seats_at_death") as rule:
                write()
            assert rule.call_args.args == ("p",), module.__name__

    def test_a_seat_written_already_ended_needs_no_rule(self):
        from app.scraper import graph_writer
        _, ctx = _session()
        with patch.object(graph_writer, "record_claim"), patch.object(graph_writer.db, "get_session", ctx), \
             patch.object(graph_writer, "end_seats_at_death") as rule:
            graph_writer._upsert_role("p", "e", "CEO", "wikidata", until="2011-08-24", source_url="w")
        assert not rule.called


class TestTheDepartureIsCitedBesideTheAppointment:
    def test_the_close_leaves_the_seats_own_citation_alone(self):
        from app.scraper import sec_writer
        session, ctx = _session(rows=[{"role": "CEO"}])
        with patch.object(sec_writer, "close_role_claim"), patch.object(sec_writer.db, "get_session", ctx):
            sec_writer._close_role_sec("p1", "e1", "2026-01-01", role="CEO", source_id="sec-edgar", source_url="u8k")
        cypher = session.run.call_args.args[0]
        assert "r.until_source_url = COALESCE($surl, r.until_source_url)" in cypher
        assert "r.source_url" not in cypher and "r.source_date" not in cypher

    def test_claim_props_carry_the_end_citation(self):
        bag = claims.claim_props(kind="role", from_id="p", to_id="e", source_id="s", role="CEO",
                                 until="2026-01-01", until_source_url="u8k", until_read_from="narrative")
        assert (bag["until_source_url"], bag["until_read_from"]) == ("u8k", "narrative")

    def test_a_re_read_without_an_end_keeps_it_for_roles_only(self):
        rule = claims._LISTING_DATE_KEPT["until"]
        assert ":until IS NULL AND :kind = 'role' AND until IS NOT NULL" in rule
        assert "(:since IS NULL OR :since = since)" in rule            # a new spell replaces it
        for name in ("until_source_url", "until_read_from"):
            assert claims._END_KEPT in claims._LISTING_DATE_KEPT[name]

    def test_the_end_rules_are_assigned_before_the_start(self):
        """ArcadeDB applies SET assignments in order; the end rule compares the
        STORED start, so `until = CASE …` must come before `since = …`."""
        seen = []
        with patch("app.db.arcadedb.run_sql", lambda sql, params=None: seen.append(sql) or []), \
             patch("app.stake_history.rows_for_batch", lambda rows: []), patch("app.stake_history.deferred", lambda p: False):
            claims.record_claim(kind="role", from_id="p", to_id="e", source_id="s", role="CEO", since="2027-01-10")
        (sql,) = seen
        assert sql.index("until = CASE") < sql.index(", since = CASE")
        assert sql.index("until_source_url = CASE") < sql.index(", since = CASE")

    def test_close_role_claim_updates_the_sources_claim_and_records_one_only_when_absent(self):
        calls = []

        def run_sql(sql, params=None):
            calls.append((sql, params))
            return [{"count": 1}]
        with patch("app.db.arcadedb.run_sql", run_sql), patch.object(claims, "record_claim") as rc:
            claims.close_role_claim(from_id="p", to_id="e", source_id="sec-edgar", role="Director",
                                    until="2026-01-01", until_source_url="u8k", until_read_from="narrative")
        (sql, params), = calls
        assert sql.startswith("UPDATE Claim SET until = :until, until_source_url = :u, until_read_from = :r")
        assert params["key"] == claims.claim_key("role", "p", "e", "sec-edgar", role_key="director")
        assert not rc.called
        with patch("app.db.arcadedb.run_sql", lambda sql, params=None: [{"count": 0}]), \
             patch.object(claims, "record_claim") as rc:
            claims.close_role_claim(from_id="p", to_id="e", source_id="sec-edgar", role="Director",
                                    until="2026-01-01", until_source_url="u8k", until_read_from="narrative",
                                    filing_type="8-K")
        kw = rc.call_args.kwargs
        assert kw["until"] == "2026-01-01" and kw["source_url"] == "u8k" and kw["filing_type"] == "8-K"
        assert kw["until_source_url"] == "u8k" and kw["read_from"] == "narrative"
