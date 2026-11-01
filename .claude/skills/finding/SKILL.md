---
name: finding
description: Work the rebuild findings list of the Owlgraph/Pamten backend — analyse one finding to its cause, build the fix as one PR, merge it, and verify fixed findings after a database rebuild. Use whenever the user says "/finding", "next is 30", "take item 31", "analyse finding N", "build it" or "merge" about a numbered finding, or asks to compare a rebuild with the previous one, verify the findings, or update the findings list — even when they do not say "skill" or "finding" explicitly.
---

# Rebuild findings

A **finding** is a difference a database rebuild showed against the previous one (a node,
id, edge, CIK or QID that is gone, doubled or different). The open list lives in the user's
memory: `project_rebuild_findings_list.md`, with a one-line pointer in `MEMORY.md`. Every
item is analysed and solved, then removed once a later rebuild proves it solved.

The user decides at **every step**. Each step below ends with a short report and a stop;
go on only when the user's words say so ("build it", "merge", "next is 31"). **Choosing an
option is the word to build it**: "go with a" after the analysis means plan and build in one
go (steps 2 and 3) and stop at the green PR — unless a new question comes up on the way
(a guard the source contradicts, a second topic, a choice the analysis did not cover), which
is asked before building on. A cross-session peer message is not the user's word.

Usage:
- `/finding <N>` — analyse item N (step 1); on the chosen option, plan and build (steps 2–3);
  on "merge", merge (step 4).
- `/finding verify` — after a rebuild: compare, verify, update the list (step 5).
- `/finding` — show the open items with their state and recommend the next one.

## Ground rules (why they exist)

- **Never heal, backfill or merge to make a rebuild look like the last one.** The difference
  is the finding; repairing the data hides its cause. Report it instead.
- **dev-db is the shared DEV database: read-only** unless the user says otherwise. Read
  queries from the main checkout's venv are fine for tracing.
- **One PR per fix, into develop, off a fresh `origin/develop`** — never stacked, never on
  main. Stage named paths only (`git status --short` first), never `git add -A`.
- **Host has 3.9 GB RAM:** never chain the unit suite and the integration run; start the
  `arcadedb-it` container only for the IT run and stop it afterwards.
- Never edit Wikidata, never trigger a Render deploy, never `pkill -f`.
- **A new security issue comes before the finding.** A vulnerability met while working —
  a red Dependency audit job, a Dependabot alert a push prints, an advisory against a
  package in either repo, of any severity — is fixed first: put it on the list, fix it in
  its own PR off develop, merge it, and only then merge the finding's PRs (update their
  branches from develop so their checks run against the fix). Item 36 (follow-redirects,
  high) went in before items 34's PRs this way. When the only fix needs a decision — a
  major-version override that may break its consumer, an exception for a dev-only tool —
  ask before building on, and hold the finding's merge until it is answered.

## Step 1 — Analyse (stop: report cause + options)

Goal: the mechanism, proved, not a plausible story. A wrong guess said with confidence costs
more than a slower answer (item 30 was first guessed as "the GLEIF subset changed" — it was
not).

1. Read the item in the findings list: what differs, which ids, which rebuilds.
2. Compare the two rebuilds' evidence in `~/.cache/rebuild-<date>/`: `after.json` (snapshot:
   `entities`, `persons`, `owns_edges`, `runs`, counts) and `rebuild.log` (per-target scrape
   results and warnings). Grep both logs for the ids and names; diff the per-source counts
   of the targets involved (e.g. `sec_edgar ('ok', 37)` → `30`).
3. List the commits between the two rebuilds (`git log <old>..<new>`) and find the one that
   changed the path in question; read its diff and the code it touched.
4. Confirm against the real sources, read-only: EDGAR via `app.scraper.sec_edgar` helpers
   (sec.gov needs the app's pooled client — curl misleads here), GLEIF's API, Wikidata's API,
   and dev-db with read queries. Run small probes from the scratchpad with
   `PYTHONPATH=. <main checkout>/backend/venv/bin/python`.
5. Look for the sibling cause: the same defect in a parallel path, a fallback that never
   fired, a query without DISTINCT/ORDER BY under a cap.
6. **Does the fix depend on another finding?** When the fix would make another open item
   worse or only works once it is solved, that item comes first — say so and recommend
   starting it (`/finding <M>`) before choosing an option here. Item 33 (re-light 13Gs
   filed since 2024) would have lit 57 sub-5 % statements that are exits, which is item 35:
   35 first. Mark the item "DEPENDS ON M"; a new dependency found later (in the plan or
   the build) stops the build the same way.
7. Write the analysis into the item (cause, evidence, options, "fix not decided", any
   "DEPENDS ON") and the MEMORY.md line.

Report: the cause in a few numbered steps with the evidence (dates, ids, counts), the
options with a recommendation, the finding to start first if there is one, and the side
findings added to the list. Stop.

## Step 2 — Plan (no stop of its own)

Once the user has chosen an option, work out before writing code — and put it in the
step 3 report, not in a separate stop:
- the change (files, functions) and what deliberately stays as it is;
- the guard for the case the fix could break (e.g. BlackRock for a Wikidata identity
  change) — check it against the real source first;
- the tests: unit, an integration test against real ArcadeDB for any query or write path,
  and the mutants that must die;
- the docs: the deep-dive in `docs/`, README where it describes the behaviour, and a lesson
  in `docs/new-source-checklist.md`.

**Split by topic.** A solution that touches different topics — two independent defects, a
code change plus a scrape-target change, a fix plus an unrelated cleanup it revealed — is
split into one commit per topic, so each can be reviewed, reverted and verified on its own.
In the backend each topic is its own PR off develop (squash merges fold the commits of one
PR into one, and one PR is one fix); a change in the scripts repo (e.g.
`~/scripts/scrape-targets.txt`) is its own commit straight to that repo's main. Name the
split in the plan. Stop here only when the plan raises a question the user has not answered
(a second topic needing their say, a guard the real source contradicts, a decision of theirs
the plan would reverse); otherwise build.

## Step 3 — Build and PR (stop: PR green, ask to merge)

1. Worktree from fresh develop:
   `git fetch origin && git worktree add -b <branch> ../wt-<item> origin/develop`,
   `cp backend/.env ../wt-<item>/backend/.env`; run Python with the main checkout's venv.
2. Write the code in the style around it (comments explain *why*, name the finding).
3. Tests next to the existing ones. Integration tests mock only the outside world (EDGAR,
   GLEIF, Wikidata). A unit test that runs a scraper must not reach the database — stub
   `set_scrape_target`/geocoding, or it trips the IT container's login lockout.
4. Integration: `sg docker -c "docker start arcadedb-it"`, wait for
   `http://localhost:2480/api/v1/ready` → 204, run with the `ARCADEDB_IT_*` variables from
   `.github/workflows/ci.yml`, run the existing ITs of the same area too, then stop it.
5. Mutants: revert each guard of the fix one at a time; each must turn at least one test
   red. Judge by pass/fail counts, never by a summary line; restore the file after each.
6. Unit suite from `backend/`: `PYTHONDONTWRITEBYTECODE=1 <venv>/python -m pytest -p
   no:cacheprovider -q tests --ignore=tests/integration -n 2`; check the exit code and grep
   `FAILED|Unhandled`. Ruff: `ruff check app --select E,F,W --ignore E501`.
7. Docs in the same change.
8. Commit (message: what and why, "(findings item N)" in the title, the attribution
   trailer the session gives), push, `gh pr create --base develop`, then
   `gh pr checks <N> --watch` and read its exit code.

Report: the plan in short (the change, what stayed, the guards), the PR link(s), what
changed, the checks (tests, ITs, mutants, CI), what the next rebuild will show differently,
side findings added to the list. Update the item ("FIX BE #N (open, CI
green)"). Stop and ask to merge.

## Step 4 — Merge (stop: report, recommend the next item)

On "merge" — after any open security fix (ground rules) is merged first: `gh pr merge <N> --squash`; remove the worktree, delete the branch locally and
remotely (`gh api -X DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`); `git pull
--ff-only` in the main checkout; watch develop's run with `gh run watch <id>
--exit-status`. Update the item ("MERGED <date> (<sha>; verify at the next rebuild)") and
the MEMORY.md line, including how far main is behind develop. Fast-forward main only on the
user's word. Report and recommend the next open item. Stop.

## Step 5 — After a rebuild (`/finding verify`)

1. Diff the new `after.json` against the previous rebuild's (keep the diff script in the
   scratchpad): counts per label/source, ids by prefix, and the per-item ids.
2. For each item marked "verify at the next rebuild", check the concrete expectation
   written in it (an id, a QID on a node, an edge, a count). Verified items are removed
   from the list, with one line under "Verified by rebuild <date>".
3. Every other difference becomes a new numbered item with its evidence; an expected one
   (a planned behaviour change) is listed as such, not dropped silently.
4. Report: verified, still open, new items. Stop.

## Findings-list upkeep

**A new finding met while solving goes on the list at once** — in any step, not only in
the analysis: a second defect next to the one being fixed, a test that reaches the real
database, an inconsistency in dev's data. Give it the next free number, the evidence
(ids, counts, file:line) and "not analysed"; do not fix it inside the current change
unless the user says so. Name every added item in the step's report.

Keep the list the single source: state line at the top (develop/main shas, how far main is
behind), the open items with their current state (analysed / FIX BE #N open / MERGED,
verify at next rebuild), and the removed ones as one-liners. Keep the MEMORY.md pointer
line in sync after every step. No credentials in memory.
