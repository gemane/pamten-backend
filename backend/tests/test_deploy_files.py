"""
The production deploy files agree with each other — deploy/setup-server.sh,
deploy/ops.sh, docker-compose.yml, the Caddyfile and .env.example.

These run nowhere near a real server, so what CI can check is that the pieces
fit: every variable compose requires is in the template, every cron entry names
a command ops.sh has, host-only secrets never reach a container — and the two
bugs the first real run found (ArcadeDB's home mounted over, a Caddy healthcheck
that could never pass) stay fixed.
"""
import re
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
COMPOSE = (DEPLOY / "docker-compose.yml").read_text()
CADDY = (DEPLOY / "Caddyfile").read_text()
SETUP = (DEPLOY / "setup-server.sh").read_text()
OPS = (DEPLOY / "ops.sh").read_text()
EXAMPLE = (DEPLOY / ".env.example").read_text()

EXAMPLE_KEYS = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", EXAMPLE, re.M))
HOST_KEYS = set(re.search(r'HOST_KEYS="([^"]*)"', SETUP, re.S).group(1).replace("\\", " ").split())


@pytest.mark.parametrize("script", ["setup-server.sh", "ops.sh"])
def test_the_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(DEPLOY / script)]).returncode == 0


def test_every_variable_compose_requires_is_in_the_template():
    required = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", COMPOSE))
    assert required and required <= EXAMPLE_KEYS, required - EXAMPLE_KEYS


def test_host_only_keys_are_in_the_template_and_never_reach_a_container():
    assert HOST_KEYS - {"SKIP_DNS_CHECK"} <= EXAMPLE_KEYS
    used_by_compose = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", COMPOSE))
    assert not (HOST_KEYS & used_by_compose)
    assert "GHCR_TOKEN" in HOST_KEYS and "BACKUP_REMOTE" in HOST_KEYS


def test_setup_checks_every_secret_it_needs():
    for key in ("DOMAIN", "APP_VERSION", "ARCADEDB_ROOT_PASSWORD", "SECRET_KEY",
                "ADMIN_EMAIL", "ADMIN_PASSWORD", "ARCADEDB_VERSION"):
        assert key in re.search(r"for k in (.*?); do", SETUP, re.S).group(1)


def test_every_cron_entry_is_a_command_ops_has():
    cron = re.findall(r"ops\.sh (\S+)\s+>>", SETUP)
    assert set(cron) == {"backup", "gleif-update", "weekly-report", "prune-analytics"}
    commands = set(re.findall(r"^\s+([a-z-]+)\)\s", OPS, re.M))
    assert set(cron) <= commands
    # and setup calls ops.sh for these
    for used in ("install-web", "count", "manage", "import"):
        assert used in commands


def test_the_backup_runs_before_the_delta():
    times = {job: (int(h), int(m)) for m, h, job in
             re.findall(r"^(\d+) (\d+) \* \* [\d*]\s+root \S+ops\.sh (\S+)", SETUP, re.M)}
    assert times["backup"] < times["gleif-update"]


def test_arcadedb_keeps_its_home_only_data_is_mounted():
    # mounting the data directory over /home/arcadedb hid bin/server.sh:
    # "./bin/server.sh: not found", restarting forever
    assert not re.search(r":/home/arcadedb\s*$", COMPOSE, re.M)
    assert ":/home/arcadedb/databases" in COMPOSE and ":/home/arcadedb/backups" in COMPOSE
    assert '"$ARCADEDB_DATA/databases"' in SETUP and "chown -R 1000:1000" in SETUP


def test_caddys_healthcheck_uses_its_internal_door():
    # the public site redirects to HTTPS and has no certificate for localhost
    assert "http://localhost:8080/health" in COMPOSE
    assert re.search(r"^:8080 \{\s*reverse_proxy /health api:8000", CADDY, re.M)
    caddy_ports = re.search(r"caddy:.*?ports:(.*?)environment:", COMPOSE, re.S).group(1)
    assert "8080" not in caddy_ports          # never published


def test_the_importers_files_are_mounted_where_ops_reads_them():
    assert "${DATA_DIR:-/srv/owlgraph/data}:/data" in COMPOSE
    assert "/data/lei-cdf/gleif-lei2.json.zip" in OPS
    assert "SCRAPER_TMP_DIR=/data/tmp" in OPS


def test_nothing_is_pinned_to_latest():
    assert ":latest" not in COMPOSE


def test_the_import_chain_tells_its_steps_the_lock_is_theirs():
    # without it every importer refused: "another import holds the lock" — ops
    # itself (found by the first real run)
    assert "${ORCHESTRATED:+-e IMPORT_ORCHESTRATED=1}" in OPS
    body = OPS[OPS.index("cmd_import()"):OPS.index("cmd_backup()")]
    acquire, orch = body.index("import-lock acquire"), body.index("\n  ORCHESTRATED=1")
    assert acquire < orch                        # set only once the lock is held
    assert "ORCHESTRATED= run_manage import-lock release" in body


def test_the_backup_marker_is_one_alerts_can_read():
    # alerts runs in the container and parses "<ISO timestamp> <archive> [offsite]"
    assert "ALERT_BACKUP_MARKER=/data/backup.last" in EXAMPLE
    assert '> "$DATA/backup.last"' in OPS
    from app.alerts import read_backup_marker
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".last", delete=False) as f:
        f.write("2026-10-05T09:39:42Z owlgraph-backup-20261005-093941822.zip offsite\n")
    try:
        m = read_backup_marker(f.name)
        assert m["archive"] == "owlgraph-backup-20261005-093941822.zip" and m["offsite"] is True
    finally:
        os.unlink(f.name)


def test_the_import_ends_with_the_backend_s_finishing_steps():
    # no importer merges GLEIF's company with its Companies House record or a
    # PSC foreign owner (a rebuild left 13 doubled companies), and the bulk
    # importers store dissolved_on without closing anything (Altaba, dissolved
    # 2019, still had 104 open edges). The steps and their order are the `import`
    # profile of app/finishing.py — until 2026-11-25 they were written out here,
    # without `close-reported-by-parent`, which the dev import ran.
    from app import finishing
    finish = OPS[OPS.index('step "finishing"'):OPS.index("cmd_gleif_update() {")]
    code = "\n".join(ln for ln in finish.splitlines() if not ln.lstrip().startswith("#"))
    assert 'if [ "$mode" = test ]; then run_manage finish --profile import\n' in code
    # a --bulk-load chain leaves the FULL_TEXT index complete; rebuilding it takes hours
    assert "else run_manage finish --profile import --skip search-index; fi" in code
    assert code.count("run_manage") == 2                      # nothing written out beside the list
    order = finishing.known_order("import")
    assert order[0] == "entity-dedup" and order[-1] == "mark-shortcuts"
    assert order.index("entity-dedup") < order.index("dissolution-heal") < order.index("mark-shortcuts")
    assert "close-reported-by-parent" in order
    assert all(use.fatal for use in finishing.PROFILES["import"])     # a failed step fails the import (set -e)
    assert "set -euo pipefail" in OPS


def test_the_gleif_history_keeps_its_file_where_the_host_sees_it():
    # in the container ~ is gone with the container: the intervals file must
    # land in DATA_DIR, or a re-apply downloads the whole archive again
    assert "gleif-history)" in OPS
    line = next(ln for ln in OPS.splitlines() if ln.strip().startswith("gleif-history)"))
    assert "gleif-rr-history --intervals /data/" in line
    assert "gleif-history" not in SETUP          # manual, run once — never cron


def test_the_daily_delta_ends_with_the_backend_s_finishing_steps():
    # a delta stores a dissolution without closing anything and can retire the
    # direct edge a shortcut stood in for; issuers a scrape gave a CUSIP join
    # their LEI daily (findings item 71) — the `nightly` profile of
    # app/finishing.py: the bridge first and never fatal, then the dissolution
    # pass, then the shortcuts. The cron runs `ops.sh gleif-update`.
    from app import finishing
    body = OPS[OPS.index("cmd_gleif_update() {"):]
    body = body[:body.index("\n}")]
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    delta = code.index("run_manage gleif-update")
    # the LEI-ISIN file lives in /data (the container's home is gone with it); no geocoding here
    assert delta < code.index("run_manage finish --profile nightly --skip geocode --isin-dir /data/gleif-isin")
    assert code.count("run_manage") == 2
    assert finishing.known_order("nightly")[:3] == ["securities-bridge", "dissolution-heal", "mark-shortcuts"]
    assert not finishing.PROFILES["nightly"][0].fatal
    assert 'gleif-update)          cmd_gleif_update "$@"' in OPS
    assert "gleif-isin" not in SETUP        # no cron line of its own: it rides on the delta
