"""The alert rules (app/alerts.py), each over what it is handed — no box, no DB."""
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from unittest.mock import patch

from app import alerts as alerts_mod
from app.alerts import (
    Alert, check_backup, check_disk, check_health, check_imports, check_probes,
    check_refused, format_text, read_backup_marker, subject,
)
from app.config import settings
from app.source_contracts import Probe

NOW = datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)


def _run(source, status, hours_ago, error="", stale=False, target="t"):
    return {"source": source, "target": target, "status": status, "error": error, "stale": stale,
            "started_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


class TestImports:
    def test_a_failed_import_in_the_window_is_critical(self):
        [a] = check_imports([_run("gleif-update", "failed", 3, "delta 404\nmore")], NOW)
        assert a.level == "crit" and a.check == "imports"
        assert "gleif-update" in a.message and "delta 404" in a.message and "more" not in a.message

    def test_outside_the_window_or_not_an_import_is_silent(self):
        assert check_imports([_run("gleif-update", "failed", 30, "old")], NOW) == []
        assert check_imports([_run("wikidata", "failed", 1, "a scrape, not an import")], NOW) == []
        assert check_imports([_run("gleif-update", "ok", 1)], NOW) == []

    def test_a_stale_running_row_of_any_source_is_a_warning(self):
        [a] = check_imports([_run("sec-13f", "running", 5, stale=True)], NOW)
        assert a.level == "warn" and "crashed run" in a.message

    def test_a_failure_without_an_error_text_still_reports(self):
        [a] = check_imports([_run("ch-psc-update", "failed", 1)], NOW)
        assert "no error recorded" in a.message


class TestBackup:
    def test_the_marker_is_read(self, tmp_path):
        p = tmp_path / "backup.last"
        p.write_text("2026-09-30T03:05:00+00:00 owlgraph-backup-20260930.zip offsite\n")
        m = read_backup_marker(str(p))
        assert m["archive"] == "owlgraph-backup-20260930.zip" and m["offsite"] is True
        assert m["at"].hour == 3
        assert read_backup_marker(str(tmp_path / "missing")) is None

    def test_missing_marker_is_critical(self):
        [a] = check_backup(None, NOW, 26, True)
        assert a.level == "crit" and "missing" in a.message

    def test_fresh_and_offsite_is_silent(self):
        m = {"at": NOW - timedelta(hours=4), "archive": "x.zip", "offsite": True}
        assert check_backup(m, NOW, 26, True) == []

    def test_too_old_is_critical(self):
        m = {"at": NOW - timedelta(hours=27), "archive": "x.zip", "offsite": True}
        [a] = check_backup(m, NOW, 26, True)
        assert a.level == "crit" and "27 h old" in a.message and "x.zip" in a.message

    def test_not_offsite_is_critical_only_when_expected(self):
        m = {"at": NOW - timedelta(hours=1), "archive": "x.zip", "offsite": False}
        [a] = check_backup(m, NOW, 26, True)
        assert "not copied offsite" in a.message
        assert check_backup(m, NOW, 26, False) == []


Usage = namedtuple("Usage", "total used free")


class TestDisk:
    def test_thresholds(self):
        gb = 1e9
        usage = {"/": Usage(100 * gb, 50 * gb, 50 * gb),
                 "/srv": Usage(100 * gb, 90 * gb, 10 * gb),
                 "/data": Usage(100 * gb, 97 * gb, 3 * gb)}
        out = check_disk(["/", "/srv", "/data"], 85, 95, usage=lambda p: usage[p])
        assert [(a.level, a.message.split(" is ")[0]) for a in out] == [("warn", "/srv"), ("crit", "/data")]
        assert "3.0 GB free" in out[1].message

    def test_an_unreadable_path_is_a_warning_not_a_crash(self):
        def boom(p): raise OSError("no such mount")
        [a] = check_disk(["/nope"], 85, 95, usage=boom)
        assert a.level == "warn" and "cannot read" in a.message


class TestHealth:
    URL = "https://owlgraph.example.test/health"

    def test_first_sight_down_alerts_first_sight_up_is_silent(self):
        alerts, st = check_health(self.URL, True, "ok", {}, NOW)
        assert alerts == [] and st["up"] is True and st["last_up_at"] == NOW.isoformat()
        alerts, st = check_health(self.URL, False, "ConnectError", {}, NOW)
        assert [a.level for a in alerts] == ["crit"] and st["down_since"] == NOW.isoformat()

    def test_only_a_change_of_state_speaks(self):
        up = {"up": True, "last_up_at": "2026-09-30T06:55:00+00:00"}
        alerts, down = check_health(self.URL, False, "HTTP 503", up, NOW)
        assert len(alerts) == 1 and "DOWN" in alerts[0].message and "last up 2026-09-30T06:55" in alerts[0].message
        # still down five minutes later: silent, and down_since is kept
        later = NOW + timedelta(minutes=5)
        alerts2, still = check_health(self.URL, False, "HTTP 503", down, later)
        assert alerts2 == [] and still["down_since"] == down["down_since"] == NOW.isoformat()
        # recovery: one info, naming when it went down
        alerts3, back = check_health(self.URL, True, "ok", still, later + timedelta(minutes=5))
        assert [a.level for a in alerts3] == ["info"] and NOW.isoformat() in alerts3[0].message
        assert back["up"] is True and back["down_since"] is None


class TestRendering:
    def test_text_orders_critical_first_and_names_the_checks(self):
        alerts = [Alert("disk", "warn", "/ is 88% full"), Alert("imports", "crit", "gleif-update failed"),
                  Alert("health", "info", "up again")]
        t = format_text(alerts, NOW)
        assert t.splitlines()[0] == "Owlgraph alerts — 2026-09-30 07:00 UTC: 3"
        assert t.index("[CRIT]") < t.index("[WARN]") < t.index("[INFO]")
        assert "Internal; not for redistribution." in t
        assert subject(alerts) == "Owlgraph ALERT — 1 critical (disk, health, imports)"
        assert subject([Alert("health", "info", "up again")]) == "Owlgraph notice — health"

    def test_nothing_to_report(self):
        assert format_text([], NOW).endswith("nothing to report\n")


class TestRefused:
    """A source that refuses requests (a 4xx that is not "not there") — EDGAR's
    HTTP 400 to `count=5`, 540 in one rebuild (findings item 132)."""

    def test_a_day_of_404s_is_silent(self):
        assert check_refused({"sec-edgar": {"ok": 12891, "4xx": 596, "5xx": 9}}, 5, 50) == []

    def test_below_the_threshold_is_silent(self):
        assert check_refused({"gleif": {"ok": 800, "refused": 4}}, 5, 50) == []

    def test_a_handful_is_a_warning(self):
        [a] = check_refused({"wikidata": {"ok": 1380, "refused": 5}}, 5, 50)
        assert (a.check, a.level) == ("refused", "warn")
        assert "wikidata refused 5 requests" in a.message and "1,380 answered" in a.message

    def test_dozens_are_critical(self):
        [a] = check_refused({"sec-edgar": {"ok": 18321, "4xx": 576, "refused": 540}}, 5, 50)
        assert a.level == "crit" and "sec-edgar refused 540 requests" in a.message

    def test_each_source_is_judged_alone_and_the_bucket_is_not_one(self):
        found = check_refused({"gleif": {"refused": 60}, "sec-edgar": {"refused": 2},
                               "bucket:edgar": {"refused": 99}, "wikidata": {"refused": 7}}, 5, 50)
        assert [(a.level, a.message.split()[0]) for a in found] == [("crit", "gleif"), ("warn", "wikidata")]


class TestProbes:
    def test_each_failed_probe_is_a_critical_alert_with_its_reason(self):
        probe = Probe("sec-edgar", "company-name search", lambda: None)
        [a] = check_probes([(probe, "HTTP 400: Invalid count")])
        assert (a.check, a.level) == ("probes", "crit")
        assert a.message == "sec-edgar: company-name search — HTTP 400: Invalid count"

    def test_nothing_failed_nothing_said(self):
        assert check_probes([]) == []


class TestCollectAsksTheSources:
    def test_refused_reads_yesterday_and_today(self):
        with patch("app.source_calls.totals", return_value={"sec-edgar": {"refused": 540}}) as totals:
            [a] = alerts_mod.collect({"refused"}, NOW)
        assert totals.call_args.args == ("2026-09-29", "2026-10-01")      # end exclusive
        assert a.check == "refused" and a.level == "crit"

    def test_probes_run_and_report(self):
        probe = Probe("gleif", "an LEI record", lambda: None)
        with patch.object(settings, "ALERT_SOURCE_PROBES", True), \
             patch("app.source_contracts.run", return_value=[(probe, "no legal name")]):
            [a] = alerts_mod.collect({"probes"}, NOW)
        assert a.message == "gleif: an LEI record — no legal name"

    def test_probes_can_be_switched_off(self):
        with patch.object(settings, "ALERT_SOURCE_PROBES", False), \
             patch("app.source_contracts.run", side_effect=AssertionError("must not run")):
            assert alerts_mod.collect({"probes"}, NOW) == []

    def test_a_crashed_probe_run_is_a_warning_not_a_lost_run(self):
        with patch.object(settings, "ALERT_SOURCE_PROBES", True), \
             patch("app.source_contracts.run", side_effect=RuntimeError("boom")):
            [a] = alerts_mod.collect({"probes"}, NOW)
        assert a.level == "warn" and "probes check could not run" in a.message

    def test_both_are_part_of_a_plain_run(self):
        assert {"refused", "probes"} <= set(alerts_mod.CHECKS)
        assert type(settings).model_fields["ALERT_SOURCE_PROBES"].default is True   # on outside the tests

