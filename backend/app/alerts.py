"""Operational alerts — the things an operator must hear about between digests.

The weekly digest says what happened; this says what is wrong, and only when
something is. Four checks, each a pure function over what it is handed (rows,
a file, disk figures, an HTTP answer), so the rules are testable without a
box to break:

- **imports**: an import run that FAILED in the last 24 h, or a run stuck
  `running` past the run log's stale threshold (a crashed process).
- **backup**: the newest verified backup — the marker `backup-database.sh`
  writes after verify + offsite copy — older than the allowed age, or missing.
  Read from a marker, not the archive directory, because the archive lives on
  the machine the database runs on and this may not.
- **disk**: usage above the warn / crit thresholds on the configured paths.
- **health**: `GET /health` (the honest one) from wherever this runs — the
  outside check, when run on the dev box against production. Reported on
  CHANGE only (down, then recovered), remembered in a state file, so a
  five-minute cron does not mail every five minutes.

`manage.py alerts` prints them and, with `--email`, mails them when there are
any. It exits 0 whenever the checks ran: an alert is an answer, not a failure
— the failure case is the checks themselves crashing, which the cron dead
man's switch reports (see docs/operations.md, Monitoring).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CHECKS = ("imports", "backup", "disk", "health")

#: run-log sources that are imports; a failed one is worth a mail on its own
IMPORT_SOURCES = frozenset({"gleif-update", "ch-psc-update", "gleif-rr", "gleif-lei-cdf",
                            "ch-psc", "ch-company-data"})


@dataclass(frozen=True)
class Alert:
    check: str            # one of CHECKS
    level: str            # "crit" | "warn" | "info"
    message: str


def _iso(s: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


# ── imports ──────────────────────────────────────────────────────────────────

def check_imports(runs: list[dict], now: datetime, window_hours: int = 24) -> list[Alert]:
    """Failed import runs inside the window, and stale `running` rows of any
    source (the run log flags those; a crashed importer also leaves the DB
    import lock behind, which the next run reports on its own)."""
    since = now - timedelta(hours=window_hours)
    out: list[Alert] = []
    for r in runs:
        started = _iso(r.get("started_at"))
        if r.get("status") == "failed" and r.get("source") in IMPORT_SOURCES \
                and started and started >= since:
            err = (r.get("error") or "").strip().splitlines()[0][:160] if r.get("error") else "no error recorded"
            out.append(Alert("imports", "crit",
                             f"{r['source']} {r.get('target') or ''} failed at {r['started_at']}: {err}".strip()))
        elif r.get("stale"):
            out.append(Alert("imports", "warn",
                             f"{r.get('source')} {r.get('target') or ''} has been 'running' since "
                             f"{r.get('started_at')} — a crashed run".strip()))
    return out


# ── backup ───────────────────────────────────────────────────────────────────

def read_backup_marker(path: str) -> dict | None:
    """The marker `backup-database.sh` writes: `<ISO timestamp> <archive> [offsite]`."""
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return None
    parts = p.read_text().strip().split()
    if not parts:
        return None
    return {"at": _iso(parts[0]), "archive": parts[1] if len(parts) > 1 else None,
            "offsite": len(parts) > 2 and parts[2] == "offsite"}


def check_backup(marker: dict | None, now: datetime, max_age_hours: int,
                 expect_offsite: bool) -> list[Alert]:
    if marker is None or marker.get("at") is None:
        return [Alert("backup", "crit", "no verified backup on record — the marker file is missing "
                                        "or unreadable (has backup-database.sh ever succeeded here?)")]
    age = now - marker["at"]
    out: list[Alert] = []
    if age > timedelta(hours=max_age_hours):
        out.append(Alert("backup", "crit",
                         f"newest verified backup is {age.total_seconds() / 3600:.0f} h old "
                         f"({marker.get('archive') or '?'}); allowed {max_age_hours} h"))
    if expect_offsite and not marker.get("offsite"):
        out.append(Alert("backup", "crit",
                         f"the newest backup ({marker.get('archive') or '?'}) was not copied offsite"))
    return out


# ── disk ─────────────────────────────────────────────────────────────────────

def check_disk(paths: list[str], warn_pct: int, crit_pct: int,
               usage=shutil.disk_usage) -> list[Alert]:
    out: list[Alert] = []
    for path in paths:
        try:
            u = usage(path)
        except OSError as exc:
            out.append(Alert("disk", "warn", f"{path}: cannot read usage ({exc})"))
            continue
        pct = round(100 * u.used / u.total) if u.total else 0
        free_gb = u.free / 1e9
        if pct >= crit_pct:
            out.append(Alert("disk", "crit", f"{path} is {pct}% full ({free_gb:.1f} GB free)"))
        elif pct >= warn_pct:
            out.append(Alert("disk", "warn", f"{path} is {pct}% full ({free_gb:.1f} GB free)"))
    return out


# ── health ───────────────────────────────────────────────────────────────────

def probe_health(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """(is up, detail) for the honest /health. Any non-200 or error is down."""
    import httpx
    try:
        r = httpx.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - down is down, whatever the reason
        return False, f"{type(exc).__name__}: {exc}"[:200]
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    return True, "ok"


def check_health(url: str, up: bool, detail: str, state: dict, now: datetime) -> tuple[list[Alert], dict]:
    """Alerts on a CHANGE of state only; returns the new state to persist.

    Down → one "crit" when it first goes down (with when it was last seen up);
    up again → one "info" recovery. Steady state, up or down, is silent — the
    checker that pings this job knows it ran; a mail every five minutes while
    something is down helps nobody."""
    was_up = state.get("up")
    new = {"up": up, "checked_at": now.isoformat(), "detail": detail,
           "last_up_at": now.isoformat() if up else state.get("last_up_at"),
           "down_since": state.get("down_since") if not up and was_up is False else (None if up else now.isoformat())}
    if was_up is None:
        return ([] if up else [Alert("health", "crit", f"{url} is DOWN: {detail}")]), new
    if was_up and not up:
        return [Alert("health", "crit", f"{url} is DOWN: {detail} (last up {state.get('last_up_at')})")], new
    if not was_up and up:
        return [Alert("health", "info", f"{url} is up again (down since {state.get('down_since')})")], new
    return [], new


def load_state(path: str) -> dict:
    p = Path(os.path.expanduser(path))
    try:
        return json.loads(p.read_text()) if p.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    p = Path(os.path.expanduser(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1))


# ── the run ──────────────────────────────────────────────────────────────────

def collect(only: set[str] | None = None, now: datetime | None = None) -> list[Alert]:
    """Run the configured checks and return what is wrong. Each check is
    wrapped: one that crashes becomes a "warn" saying so, never a lost run."""
    from app.config import settings
    now = now or datetime.now(timezone.utc)
    want = set(only) if only else set(CHECKS)
    alerts: list[Alert] = []

    def guarded(name, fn):
        try:
            alerts.extend(fn())
        except Exception as exc:  # noqa: BLE001 - a broken check is itself the alert
            log.warning("alerts: check %s crashed: %s", name, exc)
            alerts.append(Alert(name, "warn", f"the {name} check could not run: {type(exc).__name__}: {exc}"[:200]))

    if "imports" in want:
        from app.scraper.run_log import list_runs
        guarded("imports", lambda: check_imports(list_runs(limit=500), now))
    if "backup" in want and settings.ALERT_BACKUP_MARKER:
        guarded("backup", lambda: check_backup(read_backup_marker(settings.ALERT_BACKUP_MARKER), now,
                                               settings.ALERT_BACKUP_MAX_AGE_HOURS,
                                               settings.ALERT_BACKUP_EXPECT_OFFSITE))
    if "disk" in want and settings.ALERT_DISK_PATHS:
        paths = [p.strip() for p in settings.ALERT_DISK_PATHS.split(",") if p.strip()]
        guarded("disk", lambda: check_disk(paths, settings.ALERT_DISK_WARN_PCT, settings.ALERT_DISK_CRIT_PCT))
    if "health" in want and settings.ALERT_HEALTH_URL:
        def _health():
            up, detail = probe_health(settings.ALERT_HEALTH_URL)
            found, new_state = check_health(settings.ALERT_HEALTH_URL, up, detail,
                                            load_state(settings.ALERT_STATE_FILE), now)
            save_state(settings.ALERT_STATE_FILE, new_state)
            return found
        guarded("health", _health)
    return alerts


def format_text(alerts: list[Alert], now: datetime) -> str:
    if not alerts:
        return f"Owlgraph alerts — {now:%Y-%m-%d %H:%M} UTC: nothing to report\n"
    order = {"crit": 0, "warn": 1, "info": 2}
    lines = [f"Owlgraph alerts — {now:%Y-%m-%d %H:%M} UTC: {len(alerts)}", ""]
    for a in sorted(alerts, key=lambda a: (order.get(a.level, 9), a.check)):
        lines.append(f"[{a.level.upper():4}] {a.check:8} {a.message}")
    lines += ["", "Automated from the Owlgraph backend (manage.py alerts). Internal; not for redistribution."]
    return "\n".join(lines) + "\n"


def subject(alerts: list[Alert]) -> str:
    crit = sum(1 for a in alerts if a.level == "crit")
    kinds = ", ".join(sorted({a.check for a in alerts}))
    return f"Owlgraph ALERT — {crit} critical ({kinds})" if crit else f"Owlgraph notice — {kinds}"
