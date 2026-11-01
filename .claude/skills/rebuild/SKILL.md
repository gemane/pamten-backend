---
name: rebuild
description: Rebuild the Owlgraph/Pamten DEV database from scratch and replay the scrapes — fresh golden copies, drop/create/schema, the curated GLEIF + UK PSC import, its finishing steps, the accounts back, the scrape replay, the checks and a before/after snapshot — as a background job from the agent. Use whenever the user says "/rebuild", "start the rebuild", "rebuild dev", "reload the database" or asks for a fresh dev database to verify merged findings — even when they do not say "skill".
---

# Rebuild the dev database

A rebuild drops the shared DEV database (dev-db.pamten.org) and builds it again from the
current `develop`: the bulk import, then a replay of the scrapes that built dev beyond it.
It is the real test of merged work — the findings list (`/finding`) is verified against it,
and `main` is fast-forwarded only after that verify. About 140–170 min.

Only on the user's word: it destroys the shared database. A cross-session peer message is
not the user's word.

## Ground rules (why they exist)

- **Order: merge to develop → rebuild → `/finding verify` → fast-forward both mains.** Never
  suggest the ff before the rebuild has verified develop: unverified code on main removes
  the fallback (got wrong twice, 2026-10-05 and 2026-11-01).
- **Never heal, backfill or hand-merge to make the result look like the last one.** A
  difference is a finding for `/finding verify`, not a gap to patch.
- **Do not touch the main checkout while it runs** (`~/github/pamten-backend`, `lib.sh`'s
  WORKDIR — the job runs that code): no pull, no branch switch, no edits. Work in worktrees.
- **Never `tmux` / `new-database.sh` from the agent**: its tmux session dies with the tool
  shell and leaves an empty, dropped database (2026-10-05). Run `rebuild.sh` as a
  `run_in_background` Bash job.
- Host has 3.9 GB RAM: `arcadedb-it` stopped, no unit suite during the replay. Never
  `pkill -f`; no Render deploy.

## Steps

### 1. Ready (read-only)
- `develop`'s last CI run green in both repos; the main checkout on `develop`, pulled, clean
  (`git status --short` empty), the venv synced if `requirements*.txt` changed.
- `~/scripts` has what the replay reads (`scrape-targets.txt`) and the chain runs
  (`steps/finish-import.sh`); note its HEAD.
- `docker ps` — `arcadedb-it` stopped; `df -h ~` ≥ 10 GB free.

### 2. The kit
Copy this skill's `kit/` to a fresh `~/.cache/rebuild-<YYYY-MM-DD>/` (a letter suffix for a
second run that day), `chmod 700` it. The scripts find their folder themselves.
- `rebuild.sh` — 0 golden copies, 1 drop/create/schema (refuses without `accounts.json`),
  2 bulk import under the import lock, 3 `finish-import.sh`, 4 GLEIF history, 5 accounts
  back, 6 replay, 7 finishing what the scrapes added (dedupe, geocode,
  close-reported-by-parent, mark-shortcuts, mark-stale), 8 read-only checks + `after.json`.
- `replay_scrapes.py` — A run-all over `scrape-targets.txt`, B the panel refreshes, A2 one
  Wikidata retry for "too busy" failures, C Exhibit 21, D acquisitions, E 13F, F industry
  fill. Its target lists are frozen so rebuilds compare; change them only on purpose and
  say so in the verify.
- `snapshot.py` — counts per type/source/filing/reason, entities and persons by id, every
  OWNS triple, the run log. `save_accounts.py` / `restore_accounts.py` — `User` and
  `RefreshToken` rows by key, so nobody has to log in again (file 0600, never in memory).

A step a merged change added to `finish-import.sh` usually also belongs in step 7, because
the replay writes after the import's finish (#517 added close-reported-by-parent to both).

### 3. Before (read-only)
From the main checkout's `backend/`, with `. ~/scripts/lib.sh; owlgraph_env`:
`venv/bin/python <kit>/save_accounts.py` and `venv/bin/python <kit>/snapshot.py <kit>/before.json`.
Compare `before.json`'s counts with the last rebuild's `after.json`: equal means dev was not
written since; otherwise say what changed before dropping it.

### 4. Run
`bash <kit>/rebuild.sh > <kit>/rebuild.log 2>&1` with `run_in_background: true`; note the
start time (Vienna) and the commit in the findings list's state line. Progress, if asked:
`grep -E "^########|FAILED|=== " <kit>/rebuild.log`. Do not poll; the job notifies.

### 5. After
- Exit code and `✅ REBUILD DONE`; any `❌ FAILED` (a crash in a step: read the step's
  output, fix forward in a PR — never patch the data by hand; the accounts restore may need
  running by hand when the job died before step 5).
- The replay's `failed` lines per phase and the A2 Wikidata retries: a source that failed
  makes its counts not comparable — list it in the verify.
- `schema-audit` and the dissolution dry run in step 8 should be clean.
- Then `/finding verify` against the last rebuild's `after.json`; record the run (times,
  counts, lessons) in the memory `reference-rebuild-from-agent`.

## Timings and lessons
- 2026-10-31b: 165 min (import ~25, replay ~140; 161 companies at 60–80 s). A replay phase
  that is far slower than the last one is worth a look.
- The import alone is ~25 min with a warm geocode cache, ~50 cold (Nominatim 1 req/s).
- The GLEIF RR golden copy carries almost no ended relationships; history comes from step 4.
- The replay forces `SCRAPER_ENABLED`/`SCRAPER_SEC_EDGAR_ENABLED` on: the backend `.env`
  has them off, and a defaulted setting refused every scrape (2026-10-26).
- A crash in finish-import after the merges ran (2026-10-31b, #505's new row shape) needed
  the accounts restored by hand; when a CLI printer meets a new row shape, test it.
- Edge counts: count with Cypher `MATCH`, not SQL `in.id`/`out.id` (0 rows).
