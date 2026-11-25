"""The finishing steps — what a graph needs after something was loaded into it.

An import, the nightly GLEIF delta and a batch of scrapes each leave the graph
unfinished: companies two sources wrote are not merged yet, a dissolved
company's holdings are still open, new nodes have no coordinates, the shortcut
flags are stale. The passes that finish it are `manage.py` commands, and until
November 2026 five shell lists ran them — `finish-import.sh` and
`cron-gleif-update.sh` on the dev box, the rebuild kit's step 7, and
`deploy/ops.sh` twice — each with its own order and subset. They had drifted:
the production import left out `close-reported-by-parent`, which the dev
import ran.

This is the one list. `STEPS` says what each step runs; a profile says which
steps a caller needs, in which order, which of them stop the run when they
fail and which run with the scraper switched on; `RULES` holds the orders that
must never be broken, and a profile that breaks one does not load. The callers
are one line each: `manage.py finish --profile <name>`.

Each step runs as its own `manage.py` process, as it did from the shell: a
step's memory is given back when it ends, and a setting forced on for one step
(the scraper flags) cannot leak into the next.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MANAGE = Path(__file__).resolve().parent.parent / "manage.py"

#: The settings that let a pass read EDGAR. The backend `.env` of a dev box has
#: them off, so a pass that needs the register gets them for its own process
#: only (findings item 70: step 7's entity dedup never ran its SEC/LEI bridge
#: until it did). GLEIF has no such setting — its catalogue switch is its only
#: gate; the rebuild kit passed a `SCRAPER_BODS_GLEIF_ENABLED` that nothing read.
SEC = ("SCRAPER_ENABLED", "SCRAPER_SEC_EDGAR_ENABLED")


@dataclass(frozen=True)
class Step:
    """One finishing pass: the name a caller selects it by, the label a log
    line and the rebuild's call record give it, and its `manage.py` arguments."""
    name: str
    label: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class Use:
    """A step as one profile runs it."""
    step: str
    fatal: bool = False              # a failure stops the profile (else it is said and the run goes on)
    flags: tuple[str, ...] = ()      # settings forced to "true" for this step's process


STEPS: dict[str, Step] = {s.name: s for s in (
    # issuers whose CUSIP a scrape stored join their LEI through GLEIF's LEI-ISIN file (item 71)
    Step("securities-bridge", "securities bridge", ("gleif-isin-bridge", "--apply")),
    # an import never merges across sources by itself; with the scraper on it also asks
    # EDGAR / GLEIF whether a same-name pair is one company
    Step("entity-dedup", "entity dedup (SEC/LEI bridge)", ("dedupe-entities", "--limit", "100000")),
    # the bulk importers store dissolved_on without closing anything
    Step("dissolution-heal", "dissolution heal", ("heal-ends-at-dissolution", "--apply")),
    # the SERVING officers only: former ones (`--history`) wait for the DPIA paragraph
    Step("uk-officers", "UK officers", ("ch-officers",)),
    Step("person-dedup", "person dedup", ("dedupe-persons",)),
    Step("close-reported-by-parent", "close-reported-by-parent", ("close-reported-by-parent",)),
    Step("close-ended-classes", "close-ended-classes", ("close-ended-classes",)),
    Step("close-absent-from-table", "close-absent-from-table", ("close-absent-from-table",)),
    Step("close-held-by-subsidiary", "close-held-by-subsidiary", ("close-held-by-subsidiary",)),
    # a non-bulk import does not maintain the FULL_TEXT index
    Step("search-index", "search index", ("rebuild-search",)),
    Step("geocode", "geocode", ("geocode",)),
    # redundancy is a property of the finished graph: after everything that changes an edge
    Step("mark-shortcuts", "mark-shortcuts", ("mark-shortcuts",)),
    Step("mark-stale", "mark-stale", ("mark-stale",)),
)}

PROFILES: dict[str, tuple[Use, ...]] = {
    # After a bulk import (`finish-import.sh`, `ops.sh import`). Every step must
    # succeed: a half-finished import is not to look like a finished one.
    "import": (
        Use("entity-dedup", fatal=True),
        Use("dissolution-heal", fatal=True),
        Use("close-reported-by-parent", fatal=True),
        Use("search-index", fatal=True),
        Use("geocode", fatal=True),
        Use("mark-shortcuts", fatal=True),
    ),
    # After the daily GLEIF delta (`cron-gleif-update.sh`, `ops.sh gleif-update`).
    # The delta's data is committed by then: a GLEIF mapping or Nominatim hiccup
    # must not make a successful delta look like a failed one.
    "nightly": (
        Use("securities-bridge"),
        Use("dissolution-heal", fatal=True),
        Use("mark-shortcuts", fatal=True),
        Use("geocode"),
    ),
    # After a batch of scrapes (the rebuild's step 7). A failed pass is said and
    # the rest still run: the verify reads what each one printed.
    "after-scrapes": (
        Use("securities-bridge"),
        Use("entity-dedup", flags=SEC),
        Use("dissolution-heal"),
        Use("uk-officers"),
        Use("person-dedup"),
        Use("geocode"),
        Use("close-reported-by-parent"),
        Use("close-ended-classes", flags=SEC),
        Use("close-absent-from-table", flags=SEC),
        Use("close-held-by-subsidiary"),
        Use("mark-shortcuts"),
        Use("mark-stale"),
    ),
}

#: (earlier, later, why) — held for every profile that runs both.
RULES: tuple[tuple[str, str, str], ...] = (
    ("securities-bridge", "entity-dedup", "the dedup folds what the bridge attached (item 89)"),
    ("securities-bridge", "dissolution-heal", "an attach writes GLEIF's record, dissolution date included"),
    ("entity-dedup", "dissolution-heal", "one company, one dissolution date"),
    ("entity-dedup", "uk-officers", "a seat lands on the node that stays"),
    ("uk-officers", "person-dedup", "the person dedup folds an officer onto a scraped person"),
    ("entity-dedup", "search-index", "the index is built over the merged graph"),
    ("entity-dedup", "geocode", "coordinates for the node that stays"),
    ("securities-bridge", "mark-shortcuts", "a join merges two nodes"),
    ("entity-dedup", "mark-shortcuts", "a merge moves edges"),
    ("dissolution-heal", "mark-shortcuts", "a closed edge is no path"),
    ("close-reported-by-parent", "mark-shortcuts", "a closed edge is no path"),
    ("close-ended-classes", "mark-shortcuts", "a closed edge is no path"),
    ("close-absent-from-table", "mark-shortcuts", "it closes edges and writes new ones"),
    ("close-held-by-subsidiary", "mark-shortcuts", "a closed edge is no path"),
)


def broken_rules(profiles: dict[str, tuple[Use, ...]] | None = None) -> list[str]:
    """What is wrong with the profiles: an unknown step, a step twice, an order
    `RULES` forbids, a former-officers load. Empty when they are sound."""
    out = []
    for name, uses in (PROFILES if profiles is None else profiles).items():
        order = [u.step for u in uses]
        for step in order:
            if step not in STEPS:
                out.append(f"{name}: unknown step {step}")
        if len(set(order)) != len(order):
            out.append(f"{name}: a step runs twice")
        for earlier, later, why in RULES:
            if earlier in order and later in order and order.index(earlier) > order.index(later):
                out.append(f"{name}: {later} runs before {earlier} — {why}")
    for step in STEPS.values():
        if "--history" in step.command:
            out.append(f"{step.name}: former officers are not loaded by a finishing step (DPIA)")
    return out


_broken = broken_rules()
if _broken:                                             # a profile edited into a wrong order does not load
    raise RuntimeError("finishing steps: " + "; ".join(_broken))


def plan(profile: str, only: str | None = None, skip: Iterable[str] = ()) -> list[Use]:
    """The steps a run of ``profile`` takes, in order. ``only`` and ``skip``
    name steps of that profile; a name that is none of them is an error — a
    typo must not silently run everything, or nothing."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r} (one of: {', '.join(PROFILES)})")
    uses = PROFILES[profile]
    known = {u.step for u in uses}
    skip = set(skip)
    for name in ({only} if only else set()) | skip:
        if name not in known:
            raise ValueError(f"{name!r} is no step of the {profile} profile (its steps: {', '.join(known_order(profile))})")
    return [u for u in uses if u.step not in skip and (only is None or u.step == only)]


def known_order(profile: str) -> list[str]:
    return [u.step for u in PROFILES[profile]]


def argv(use: Use, db_url: str | None = None, isin_dir: str | None = None,
         geocode_limit: int | None = None) -> list[str]:
    """The `manage.py` arguments of one step, with the caller's options on the
    steps that take them: the direct database URL and the LEI-ISIN folder on
    the securities bridge, the limit on the geocoder."""
    args = list(STEPS[use.step].command)
    if use.step == "securities-bridge":
        if isin_dir:
            args += ["--dir", isin_dir]
        if db_url:
            args += ["--db-url", db_url]
    if use.step == "geocode" and geocode_limit is not None:
        args += ["--limit", str(geocode_limit)]
    return args


def _call(args: list[str], env: dict[str, str]) -> int:
    return subprocess.run([sys.executable, str(MANAGE), *args], env=env, check=False).returncode  # noqa: S603


def run(profile: str, only: str | None = None, skip: Iterable[str] = (), db_url: str | None = None,
        isin_dir: str | None = None, geocode_limit: int | None = None, dry_run: bool = False,
        call: Callable[[list[str], dict[str, str]], int] = _call,
        say: Callable[[str], None] = lambda line: print(line, flush=True)) -> int:
    """Run the profile's steps; the exit code of the run.

    A fatal step that fails ends the run with its exit code. Another step's
    failure is said and the run goes on — and still ends 0, as the shell lists
    did with `|| echo`. With ``only`` the one step's own exit code is returned
    whatever the profile says, so a caller looping over the steps decides."""
    uses = plan(profile, only, skip)
    for i, use in enumerate(uses, 1):
        step = STEPS[use.step]
        args = argv(use, db_url, isin_dir, geocode_limit)
        flags = "".join(f"{f}=true " for f in use.flags)
        if dry_run:
            say(f"{flags}manage.py {' '.join(args)}")
            continue
        say(f"### {i}/{len(uses)} {step.label} — {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
        code = call(args, {**os.environ, **{f: "true" for f in use.flags}})
        if code == 0:
            continue
        if only or use.fatal:
            if not only:
                say(f"  ! {step.label} failed (exit {code}) — stopping, the steps after it did not run")
            return code
        say(f"  ! {step.label} failed (exit {code}) — going on")
    return 0


def listing(profile: str, skip: Iterable[str] = ()) -> list[str]:
    """`name<TAB>label` per step, for a caller that runs them one by one (the
    rebuild records each step's calls to the outside sources in between)."""
    return [f"{u.step}\t{STEPS[u.step].label}" for u in plan(profile, None, skip)]
