"""The one list of finishing steps (app/finishing.py).

Five shell lists ran these passes until November 2026 — two on the dev box, the
rebuild kit's step 7, two in deploy/ops.sh — and had drifted: the production
import left out `close-reported-by-parent`. The profiles are pinned here in
full: a step added to one and not to another is then a decision somebody
wrote down, not an omission nobody saw."""
import os
import sys

import pytest

from app import finishing
from app.finishing import PROFILES, RULES, STEPS, Use

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import manage  # noqa: E402


def _commands(profile, **kw):
    said = []
    assert finishing.run(profile, dry_run=True, say=said.append, **kw) == 0
    return said


class TestTheProfiles:
    def test_after_an_import(self):
        assert _commands("import") == [
            "manage.py dedupe-entities --limit 100000",
            "manage.py heal-ends-at-dissolution --apply",
            "manage.py close-reported-by-parent",
            "manage.py rebuild-search",
            "manage.py geocode",
            "manage.py mark-shortcuts",
        ]

    def test_after_the_nightly_delta(self):
        assert _commands("nightly") == [
            "manage.py gleif-isin-bridge --apply",
            "manage.py heal-ends-at-dissolution --apply",
            "manage.py mark-shortcuts",
            "manage.py geocode",
        ]

    def test_after_a_batch_of_scrapes(self):
        sec = "SCRAPER_ENABLED=true SCRAPER_SEC_EDGAR_ENABLED=true "
        assert _commands("after-scrapes") == [
            "manage.py gleif-isin-bridge --apply",
            sec + "manage.py dedupe-entities --limit 100000",
            "manage.py heal-ends-at-dissolution --apply",
            "manage.py ch-officers",
            "manage.py dedupe-persons",
            "manage.py geocode",
            "manage.py close-reported-by-parent",
            sec + "manage.py close-ended-classes",
            sec + "manage.py close-absent-from-table",
            "manage.py close-held-by-subsidiary",
            "manage.py mark-shortcuts",
            "manage.py mark-stale",
        ]

    def test_the_import_runs_without_the_scraper_flags(self):
        # a fresh database has nothing a register could be asked about; the
        # server's own .env decides there
        assert all(u.flags == () for u in PROFILES["import"] + PROFILES["nightly"])

    def test_every_import_step_is_fatal_and_no_scrape_step_is(self):
        assert all(u.fatal for u in PROFILES["import"])
        assert not any(u.fatal for u in PROFILES["after-scrapes"])
        assert {u.step: u.fatal for u in PROFILES["nightly"]} == {
            "securities-bridge": False, "dissolution-heal": True, "mark-shortcuts": True, "geocode": False}

    def test_every_step_is_a_manage_command_with_those_options(self):
        parser = manage._build_parser()
        for step in STEPS.values():
            args = parser.parse_args(list(step.command))            # exits on an unknown command or option
            assert hasattr(args, "func"), step.name
        for extra in (["gleif-isin-bridge", "--apply", "--dir", "/d", "--db-url", "http://x"],
                      ["geocode", "--limit", "200"]):
            assert hasattr(parser.parse_args(extra), "func")

    def test_every_flag_is_a_setting(self):
        # the rebuild kit passed SCRAPER_BODS_GLEIF_ENABLED for years; nothing read it
        from app.config import settings
        for uses in PROFILES.values():
            for use in uses:
                for flag in use.flags:
                    assert hasattr(settings, flag), flag

    def test_every_step_is_used_and_labels_are_distinct(self):
        used = {u.step for uses in PROFILES.values() for u in uses}
        assert used == set(STEPS)
        assert len({s.label for s in STEPS.values()}) == len(STEPS)

    def test_former_officers_are_never_loaded(self):
        # `ch-officers --history` waits for the DPIA paragraph (legal repository)
        assert STEPS["uk-officers"].command == ("ch-officers",)
        assert not any("--history" in s.command for s in STEPS.values())


class TestTheRules:
    def test_the_profiles_hold_them(self):
        assert finishing.broken_rules() == []

    @pytest.mark.parametrize("earlier,later", [(a, b) for a, b, _ in RULES])
    def test_each_rule_is_noticed_when_broken(self, earlier, later):
        broken = finishing.broken_rules({"x": (Use(later), Use(earlier))})
        assert any(f"{later} runs before {earlier}" in line for line in broken), broken

    def test_each_rule_names_steps_and_binds_somewhere(self):
        for earlier, later, why in RULES:
            assert earlier in STEPS and later in STEPS and why
            assert any({earlier, later} <= {u.step for u in uses} for uses in PROFILES.values()), (earlier, later)

    def test_an_unknown_step_a_doubled_step_and_a_history_load_are_named(self, monkeypatch):
        assert finishing.broken_rules({"x": (Use("nope"),)}) == ["x: unknown step nope"]
        assert finishing.broken_rules({"x": (Use("geocode"), Use("geocode"))}) == ["x: a step runs twice"]
        monkeypatch.setitem(finishing.STEPS, "uk-officers",
                            finishing.Step("uk-officers", "UK officers", ("ch-officers", "--history")))
        assert finishing.broken_rules({}) == [
            "uk-officers: former officers are not loaded by a finishing step (DPIA)"]

    def test_the_officers_load_sits_between_the_two_dedups(self):
        order = finishing.known_order("after-scrapes")
        assert order.index("entity-dedup") < order.index("uk-officers") < order.index("person-dedup")

    def test_the_shortcuts_come_after_everything_that_changes_an_edge(self):
        for name in PROFILES:
            order = finishing.known_order(name)
            changers = [s for s in order if s in ("securities-bridge", "entity-dedup", "dissolution-heal")
                        or s.startswith("close-")]
            assert all(order.index(s) < order.index("mark-shortcuts") for s in changers), name


class TestARun:
    def _run(self, profile, codes=None, **kw):
        calls, said = [], []

        def call(args, env):
            calls.append((args, {k: v for k, v in env.items() if k in finishing.SEC and v == "true"}))
            return (codes or {}).get(args[0], 0)
        code = finishing.run(profile, call=call, say=said.append, **kw)
        return code, calls, said

    def test_every_step_runs_in_order_as_its_own_command(self):
        code, calls, said = self._run("nightly")
        assert code == 0
        assert [a[0] for a, _ in calls] == ["gleif-isin-bridge", "heal-ends-at-dissolution", "mark-shortcuts", "geocode"]
        assert said[0].startswith("### 1/4 securities bridge — ") and said[-1].startswith("### 4/4 geocode — ")

    def test_a_fatal_step_stops_the_run_with_its_exit_code(self):
        code, calls, said = self._run("import", codes={"heal-ends-at-dissolution": 3})
        assert code == 3
        assert [a[0] for a, _ in calls] == ["dedupe-entities", "heal-ends-at-dissolution"]
        assert said[-1] == "  ! dissolution heal failed (exit 3) — stopping, the steps after it did not run"

    def test_another_step_s_failure_is_said_and_the_run_goes_on(self):
        code, calls, said = self._run("nightly", codes={"gleif-isin-bridge": 1, "geocode": 1})
        assert code == 0 and len(calls) == 4
        assert "  ! securities bridge failed (exit 1) — going on" in said
        assert said[-1] == "  ! geocode failed (exit 1) — going on"

    def test_no_scrape_step_stops_the_rebuild(self):
        code, calls, _ = self._run("after-scrapes", codes={c: 1 for c in ("ch-officers", "dedupe-entities")})
        assert code == 0 and len(calls) == len(PROFILES["after-scrapes"])

    def test_the_flags_reach_their_step_alone(self, monkeypatch):
        for flag in finishing.SEC:
            monkeypatch.setenv(flag, "false")
        _, calls, _ = self._run("after-scrapes")
        flagged = {args[0]: sorted(env) for args, env in calls if env}
        assert flagged == {"dedupe-entities": sorted(finishing.SEC),
                           "close-ended-classes": sorted(finishing.SEC),
                           "close-absent-from-table": sorted(finishing.SEC)}
        assert os.environ["SCRAPER_ENABLED"] == "false"            # the caller's own environment is untouched

    def test_only_runs_one_step_and_returns_its_exit_code(self):
        code, calls, said = self._run("after-scrapes", codes={"ch-officers": 1}, only="uk-officers")
        assert code == 1 and [a for a, _ in calls] == [["ch-officers"]]
        assert said == [said[0]] and said[0].startswith("### 1/1 UK officers")   # the caller says what failed

    def test_skip_leaves_steps_out(self):
        _, calls, _ = self._run("import", skip=["search-index", "geocode"])
        assert [a[0] for a, _ in calls] == ["dedupe-entities", "heal-ends-at-dissolution",
                                            "close-reported-by-parent", "mark-shortcuts"]

    @pytest.mark.parametrize("kw", [{"only": "geocod"}, {"skip": ["uk-officers"]}, {"skip": ["x"]}])
    def test_a_name_that_is_no_step_of_the_profile_is_an_error(self, kw):
        # a typo must not run everything (a skip that skips nothing) or nothing
        with pytest.raises(ValueError, match="is no step of the import profile"):
            finishing.run("import", call=lambda *a: pytest.fail("ran"), say=lambda s: None, **kw)

    def test_an_unknown_profile_is_an_error(self):
        with pytest.raises(ValueError, match="unknown profile"):
            finishing.plan("weekly")

    def test_the_callers_options_go_to_the_steps_that_take_them(self):
        said = _commands("nightly", db_url="http://db", isin_dir="/data/gleif-isin", geocode_limit=200)
        assert said == ["manage.py gleif-isin-bridge --apply --dir /data/gleif-isin --db-url http://db",
                        "manage.py heal-ends-at-dissolution --apply",
                        "manage.py mark-shortcuts",
                        "manage.py geocode --limit 200"]

    def test_a_step_is_its_own_manage_process(self, monkeypatch):
        seen = {}

        class Done:
            returncode = 7

        def fake(cmd, env, check):
            seen.update(cmd=cmd, env=env, check=check)
            return Done()
        monkeypatch.setattr(finishing.subprocess, "run", fake)
        assert finishing._call(["geocode"], {"A": "b"}) == 7
        assert seen["cmd"] == [sys.executable, str(finishing.MANAGE), "geocode"] and seen["env"] == {"A": "b"}
        assert finishing.MANAGE.name == "manage.py" and finishing.MANAGE.is_file()

    def test_a_real_process_hands_its_exit_code_back(self, capfd):
        # no stub: manage.py started for real, with a command that touches no database
        assert finishing._call(["finish", "--profile", "import", "--list"], dict(os.environ)) == 0
        assert "entity-dedup\tentity dedup (SEC/LEI bridge)" in capfd.readouterr().out
        assert finishing._call(["finish", "--profile", "import", "--only", "nope"], dict(os.environ)) == 2


class TestTheCommand:
    def _main(self, argv, monkeypatch, capsys):
        args = manage._build_parser().parse_args(["finish", *argv])
        with pytest.raises(SystemExit) as stop:
            args.func(args)
        return stop.value.code, capsys.readouterr()

    def test_list_prints_name_and_label_and_runs_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(finishing, "_call", lambda *a: pytest.fail("ran"))
        args = manage._build_parser().parse_args(["finish", "--profile", "nightly", "--list", "--skip", "geocode"])
        args.func(args)
        assert capsys.readouterr().out.splitlines() == [
            "securities-bridge\tsecurities bridge", "dissolution-heal\tdissolution heal",
            "mark-shortcuts\tmark-shortcuts"]

    def test_the_labels_the_rebuild_records_its_calls_under_are_kept(self):
        # the verify's diff compares "7 <label>" rows with the last rebuild's
        labels = [line.split("\t")[1] for line in finishing.listing("after-scrapes")]
        for kept in ("securities bridge", "entity dedup (SEC/LEI bridge)", "UK officers", "geocode",
                     "close-ended-classes", "close-absent-from-table", "close-held-by-subsidiary"):
            assert kept in labels

    def test_the_exit_code_is_the_runs(self, monkeypatch, capsys):
        monkeypatch.setattr(finishing, "run", lambda *a, **k: 5)
        assert self._main(["--profile", "import"], monkeypatch, capsys)[0] == 5

    def test_skip_takes_commas_and_repeats(self, monkeypatch, capsys):
        got = {}
        monkeypatch.setattr(finishing, "run", lambda profile, **k: got.update(k) or 0)
        self._main(["--profile", "import", "--skip", "geocode,search-index", "--skip", "mark-shortcuts"],
                   monkeypatch, capsys)
        assert got["skip"] == ["geocode", "search-index", "mark-shortcuts"]

    def test_a_wrong_step_name_exits_2_and_says_which_steps_there_are(self, monkeypatch, capsys):
        code, io = self._main(["--profile", "import", "--only", "geocod"], monkeypatch, capsys)
        assert code == 2 and "is no step of the import profile" in io.err and "search-index" in io.err
