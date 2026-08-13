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
