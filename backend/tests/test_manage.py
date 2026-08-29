"""
Tests for the manage.py wipe-source command (there is no whole-DB wipe — a fresh
start is a database drop). Covers the three safety guards that keep a delete from
ever running against the wrong database, and that it delegates to
maintenance.wipe_source once the guards pass.
"""
import types

import pytest


def _args(**kw):
    kw.setdefault("confirm_database", "test")  # matches ARCADEDB_DATABASE in conftest
    kw.setdefault("source", "UK PSC")
    return types.SimpleNamespace(**kw)


def _arm(monkeypatch):
    """Enable the dedicated wipe guard (Guard 1)."""
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_WIPE", "true")


def test_backfill_search_updates_entity_and_person(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.db.arcadedb.run_sql", lambda q, *a, **k: calls.append(q))

    import manage
    manage.cmd_backfill_search(_args(batch=20000))

    assert any("UPDATE Entity SET search_text" in c and "WHERE search_text IS NULL LIMIT 20000" in c
               for c in calls)
    assert any("UPDATE Person SET search_text" in c and "WHERE search_text IS NULL LIMIT 20000" in c
               for c in calls)
    # name is null-guarded so a null name can't leave search_text NULL and loop forever
    assert any("ifnull(name, '')" in c for c in calls)


def _stub_wipe(monkeypatch):
    """Record calls to maintenance.wipe_source without touching the DB."""
    calls: list[dict] = []
    def fake(source, batch=10000, id_prefixes=None, **kw):
        calls.append({"source": source, "batch": batch, "id_prefixes": id_prefixes})
        return {"edges": {"OWNS": 3}, "nodes": {"Entity": 2, "Person": 5}, "reindexed": 9}
    monkeypatch.setattr("app.scraper.maintenance.wipe_source", fake)
    return calls


def test_wipe_source_delegates_after_guards(monkeypatch):
    _arm(monkeypatch)
    calls = _stub_wipe(monkeypatch)

    import manage
    manage.cmd_wipe_source(_args(yes=True, source="UK PSC", batch=500, id_prefix="chpsc:,gb-coh:"))

    assert calls == [{"source": "UK PSC", "batch": 500, "id_prefixes": ["chpsc:", "gb-coh:"]}]


def test_wipe_source_refuses_without_the_dedicated_flag(monkeypatch):
    # Guard 1: DEBUG must NOT be enough — only ALLOW_DESTRUCTIVE_WIPE arms it.
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_WIPE", raising=False)
    monkeypatch.setenv("DEBUG", "true")
    calls = _stub_wipe(monkeypatch)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_source(_args(yes=True))
    assert calls == []  # bailed before touching the DB


def test_wipe_source_refuses_without_confirm_database(monkeypatch):
    # Guard 2: must name the target DB explicitly.
    _arm(monkeypatch)
    calls = _stub_wipe(monkeypatch)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_source(_args(yes=True, confirm_database=None))
    assert calls == []


def test_wipe_source_refuses_on_database_name_mismatch(monkeypatch):
    # Guard 2: the named DB must match the connected one — this stops a delete
    # aimed at the wrong (e.g. production) database.
    _arm(monkeypatch)
    calls = _stub_wipe(monkeypatch)

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_wipe_source(_args(yes=True, confirm_database="owlgraph"))  # != "test"
    assert calls == []


# ── set-password ──────────────────────────────────────────────────────────────
#
# The operator escape hatch: ADMIN_PASSWORD only seeds a *missing* account, the
# email reset flow needs SMTP (blocked on Render), and /auth/change-password needs
# the current password — so without this there is no way to rotate a known-but-
# unwanted password.

def _stub_sql(monkeypatch, select_rows):
    """Record run_sql calls; the first (the SELECT) returns select_rows."""
    calls: list[tuple] = []

    def _run_sql(query, params=None, *a, **k):
        calls.append((query, params or {}))
        return select_rows if query.strip().upper().startswith("SELECT") else []

    monkeypatch.setattr("app.db.arcadedb.run_sql", _run_sql)
    return calls


def test_set_password_hashes_and_updates(monkeypatch):
    from app.auth.security import verify_password
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])

    import manage
    manage.cmd_set_password(_args(email="boss@example.com", password="Zt9mQ2vLp4rK"))

    update = [(q, p) for q, p in calls if q.strip().upper().startswith("UPDATE")]
    assert len(update) == 1
    query, params = update[0]
    assert "SET password_hash" in query
    assert params["e"] == "boss@example.com"
    # Stores a bcrypt hash of the password, never the password itself.
    assert params["h"] != "Zt9mQ2vLp4rK"
    assert verify_password("Zt9mQ2vLp4rK", params["h"])


def test_set_password_normalises_the_email(monkeypatch):
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])

    import manage
    manage.cmd_set_password(_args(email="  BOSS@Example.COM  ", password="Zt9mQ2vLp4rK"))

    assert all(p.get("e") == "boss@example.com" for _, p in calls)


def test_set_password_exits_when_the_user_does_not_exist(monkeypatch):
    calls = _stub_sql(monkeypatch, [])  # SELECT finds nothing

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_set_password(_args(email="ghost@example.com", password="Zt9mQ2vLp4rK"))

    assert all(not q.strip().upper().startswith("UPDATE") for q, _ in calls)


def test_set_password_enforces_the_same_policy_as_the_api(monkeypatch):
    # Each of these is rejected by /auth/register and /auth/reset-password too —
    # both sides call password_policy_error.
    for bad in ("short", "a" * 73, "password123"):
        calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])
        import manage
        with pytest.raises(SystemExit):
            manage.cmd_set_password(_args(email="boss@example.com", password=bad))
        assert all(not q.strip().upper().startswith("UPDATE") for q, _ in calls), bad


def test_set_password_prompts_twice_and_rejects_a_mismatch(monkeypatch):
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])
    prompts = iter(["Zt9mQ2vLp4rK", "typo-on-the-repeat"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(prompts))

    import manage
    with pytest.raises(SystemExit):
        manage.cmd_set_password(_args(email="boss@example.com", password=None))

    assert all(not q.strip().upper().startswith("UPDATE") for q, _ in calls)


def test_set_password_reads_from_a_hidden_prompt_when_not_given(monkeypatch):
    # The password must never have to appear in argv (shell history, ps output).
    from app.auth.security import verify_password
    calls = _stub_sql(monkeypatch, [{"email": "boss@example.com"}])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "Zt9mQ2vLp4rK")

    import manage
    manage.cmd_set_password(_args(email="boss@example.com", password=None))

    update = [(q, p) for q, p in calls if q.strip().upper().startswith("UPDATE")]
    assert len(update) == 1
    assert verify_password("Zt9mQ2vLp4rK", update[0][1]["h"])


# ── geocode ───────────────────────────────────────────────────────────────────
#
# The summary line reads keys straight off backfill()'s return dict, and nothing
# checked that the two agreed. When Location was retired the dict lost
# `locations_total`/`locations_geocoded` and the command started dying with a
# KeyError *after* doing all its work — the tests for backfill() itself were
# updated, its one caller was not. These pin the contract from both ends.

class TestGeocodeCommand:
    def _run(self, monkeypatch, result):
        import manage
        from app.scraper import geocode_backfill

        printed: list[str] = []
        monkeypatch.setattr(geocode_backfill, "backfill",
                            lambda limit=None, target="both": result)
        monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
        manage.cmd_geocode(types.SimpleNamespace(limit=None, target="both"))
        return "\n".join(printed)

    def test_prints_a_summary_without_raising(self, monkeypatch):
        out = self._run(monkeypatch, {"entities_total": 7, "entities_geocoded": 3, "geocoded": 3,
                                      "passes": {"hq": {"total": 7, "geocoded": 3}}})
        assert "3" in out and "7" in out

    def test_reports_each_pass_separately(self, monkeypatch):
        """A single total hides the case that matters: the HQ pass working while
        the registered one geocodes nothing, which is what "the switch does
        nothing" looked like from the import log."""
        out = self._run(monkeypatch, {"entities_total": 10, "entities_geocoded": 6, "geocoded": 6,
                                      "passes": {"hq": {"total": 5, "geocoded": 5},
                                                 "registered": {"total": 5, "geocoded": 1}}})
        assert "hq" in out and "registered" in out
        assert "1" in out

    def test_only_reads_keys_backfill_actually_returns(self, monkeypatch):
        """The regression itself: the summary must not reach for a key that is
        no longer in the dict."""
        from app.scraper import geocode_backfill

        # Call the real function with the DB stubbed out, so the keys under test
        # are the ones the implementation genuinely produces.
        monkeypatch.setattr(geocode_backfill, "run_query", lambda *a, **k: [])
        monkeypatch.setattr(geocode_backfill, "run_command", lambda *a, **k: None)
        # The real return value, not a dict of its keys zeroed: the summary now
        # walks a nested `passes` map, and a flattened stand-in would pass while
        # the command crashed on the genuine shape.
        real = geocode_backfill.backfill()

        out = self._run(monkeypatch, real)
        assert out  # no KeyError, no AttributeError


class TestPruneAnalytics:
    """The command behind the retention promise.

    `prune-analytics` is the only thing that makes the published notice true —
    the privacy pages and the record of processing both tell people a usage
    total is deleted once it has been untouched for twelve months. It shipped
    with the analytics feature and went unscheduled for a fortnight, which is
    how a promise ends up being kept only on paper.
    """

    def test_the_retention_window_is_the_one_that_was_published(self):
        # A tripwire, deliberately. Nothing in the code breaks if this becomes
        # 30, but `public/legal/privacy.html`, its German twin, and Activity 3 of
        # the record of processing all say twelve months, and they would silently
        # start lying. Change them in the same commit as this number.
        from app.analytics import RETENTION_DAYS
        assert RETENTION_DAYS == 365

    def test_the_default_is_what_the_cron_gets(self, monkeypatch):
        # `cron-prune-analytics.sh` passes no --days, so the parser default is
        # the window actually enforced every night. If the two drifted, the
        # published figure and the enforced one would differ with nothing to say so.
        from app.analytics import RETENTION_DAYS

        import manage
        args = manage._build_parser().parse_args(["prune-analytics"])
        assert args.days == RETENTION_DAYS

    def test_it_can_be_pointed_straight_at_the_database(self, monkeypatch):
        # The same escape hatch the importers have. A year of counters behind
        # dev-db's 60s nginx timeout is the case this exists for: a DELETE that
        # 504s halfway is worse than one that never started.
        #
        # A URL nothing else could have set. The first version asserted
        # `localhost:2480`, which is what the test config already points at — so
        # it passed with the wiring removed entirely.
        from app.config import settings

        target = "http://db.invalid.test:2480"
        assert settings.ARCADEDB_URL != target, "pick a URL the config does not already use"
        monkeypatch.setattr(settings, "ARCADEDB_URL", settings.ARCADEDB_URL)
        seen: list = []
        monkeypatch.setattr("app.analytics.prune",
                            lambda days, dry_run: seen.append((days, dry_run)) or
                            {"SearchDemand": 0, "cutoff": "x"})
        import manage
        args = manage._build_parser().parse_args(["prune-analytics", "--db-url", target])
        manage.cmd_prune_analytics(args)

        assert settings.ARCADEDB_URL == target
        assert seen == [(365, False)]

    def test_a_dry_run_reaches_the_pruner(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr("app.analytics.prune",
                            lambda days, dry_run: seen.append((days, dry_run)) or
                            {"SearchDemand": 0, "cutoff": "x"})
        import manage
        args = manage._build_parser().parse_args(["prune-analytics", "--dry-run", "--days", "30"])
        manage.cmd_prune_analytics(args)
        assert seen == [(30, True)]


# ── ensure-user: put the service account back after a rebuild ────────────────
# new-database.sh drops the database, users included. Only ADMIN_EMAIL's account
# returns (the app re-provisions it at startup); the scraper account vanished and
# the next update.sh died on a 401 having scraped nothing.

def _ensure_args(**kw):
    kw.setdefault("role", "contributor")
    kw.setdefault("password_env", "ENSURE_USER_PASSWORD")
    kw.setdefault("confirm_database", "owlgraph")
    return _args(**kw)


def _db_name(monkeypatch, name="owlgraph"):
    from app.config import settings
    monkeypatch.setattr(settings, "ARCADEDB_DATABASE", name)


def test_ensure_user_creates_a_verified_account(monkeypatch):
    from app.auth.security import verify_password
    _db_name(monkeypatch)
    monkeypatch.setenv("ENSURE_USER_PASSWORD", "Zt9mQ2vLp4rK")
    calls = _stub_sql(monkeypatch, [])          # no such user yet

    import manage
    manage.cmd_ensure_user(_ensure_args(email="scraper@owlgraph.org"))

    inserts = [(q, p) for q, p in calls if q.strip().upper().startswith("INSERT")]
    assert len(inserts) == 1
    query, params = inserts[0]
    assert params["e"] == "scraper@owlgraph.org"
    assert params["r"] == "contributor"
    assert "email_verified = true" in query      # or it cannot log in at all
    assert params["h"] != "Zt9mQ2vLp4rK" and verify_password("Zt9mQ2vLp4rK", params["h"])


def test_ensure_user_is_idempotent_and_keeps_the_password(monkeypatch):
    """Called unconditionally by the rebuild, so a second run must correct the
    role without touching a password it was not given."""
    _db_name(monkeypatch)
    monkeypatch.setenv("ENSURE_USER_PASSWORD", "Zt9mQ2vLp4rK")
    calls = _stub_sql(monkeypatch, [{"email": "scraper@owlgraph.org", "role": "viewer"}])

    import manage
    manage.cmd_ensure_user(_ensure_args(email="scraper@owlgraph.org"))

    assert not [q for q, _ in calls if q.strip().upper().startswith("INSERT")]
    updates = [(q, p) for q, p in calls if q.strip().upper().startswith("UPDATE")]
    assert len(updates) == 1
    query, params = updates[0]
    assert params["r"] == "contributor" and "email_verified = true" in query
    assert "password_hash" not in query


def test_ensure_user_refuses_on_a_database_name_mismatch(monkeypatch):
    """It mints a privileged account from whatever the environment says, so it
    gets the same guard as the destructive commands."""
    _db_name(monkeypatch, "owlgraph")
    calls = _stub_sql(monkeypatch, [])
    import manage
    with pytest.raises(SystemExit) as e:
        manage.cmd_ensure_user(_ensure_args(email="x@owlgraph.org",
                                            confirm_database="some-other-db"))
    assert e.value.code == 2
    assert calls == [], "must not touch the database at all"


def test_ensure_user_refuses_to_create_without_a_password(monkeypatch):
    _db_name(monkeypatch)
    monkeypatch.delenv("ENSURE_USER_PASSWORD", raising=False)
    _stub_sql(monkeypatch, [])
    import manage
    with pytest.raises(SystemExit) as e:
        manage.cmd_ensure_user(_ensure_args(email="x@owlgraph.org"))
    assert e.value.code == 1


def test_ensure_user_enforces_the_password_policy(monkeypatch):
    _db_name(monkeypatch)
    monkeypatch.setenv("ENSURE_USER_PASSWORD", "short")
    _stub_sql(monkeypatch, [])
    import manage
    with pytest.raises(SystemExit) as e:
        manage.cmd_ensure_user(_ensure_args(email="x@owlgraph.org"))
    assert e.value.code == 1


def test_ensure_user_takes_the_password_from_the_environment_only(monkeypatch):
    """Never from argv: an argument lands in shell history and in `ps` output for
    every user on the box."""
    import manage
    p = manage._build_parser()
    action = next(a for a in p._subparsers._group_actions[0].choices["ensure-user"]._actions
                  if a.dest == "password_env")
    assert action.default == "ENSURE_USER_PASSWORD"
    opts = {o for a in p._subparsers._group_actions[0].choices["ensure-user"]._actions
            for o in a.option_strings}
    assert "--password" not in opts
