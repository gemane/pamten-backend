---
name: finding-auto
description: Work the open rebuild findings of the Owlgraph/Pamten backend on your own — analyse each item, take the recommended option, build it as one green PR, merge it, go on to the next — until a rebuild is the better next step, asking the user only for a decision the analysis cannot make. Use whenever the user says "/finding-auto", "work the findings", "do all the fixes", "run through the open items", "fix everything until the rebuild" — even when they do not say "skill".
---

# Work the findings until a rebuild

The `finding` skill (`.claude/skills/finding/SKILL.md`) is the method: step 1 analyse, step 2
plan, step 3 build and PR, step 4 merge, step 5 verify after a rebuild. There, the user
speaks at every step boundary. Here, the user has spoken ONCE — "do the fixes as you
recommend" — and this skill says what that word covers, where it ends, and when to stop
and ask. Everything else is the `finding` skill, followed verbatim: its ground rules, its
proofs against the real sources, its guards, tests, mutants, docs, PR shape and
findings-list upkeep. Read it first, every time.

Usage:
- `/finding-auto` — start, or continue after an answer. The run picks up where the
  findings list (`project_rebuild_findings_list.md`) says it stopped.

**Scope, unless the user names something else:** the findings only, until a rebuild is the
better next step (the stop rule below). A planned build that sits on the list but is not a
correction — a new source the user decided to add (item 86, the proxy ownership table), a
feature — is not part of the run; it waits for the user's own word (user, 2026-11-10).

The first thing a run does is read the clock (`date`, stated in Vienna time) and write the
start into the state line: "finding-auto run <date> START hh:mm Vienna". A run that resumes
after a question writes "RESUMED hh:mm" the same way. The end time goes in when the run
stops (see *The final report*). The clock, never a guess: the rebuild record once carried
a start time written from memory that was 90 minutes off (2026-11-08b).

## What the user's word covers

For every open item, in the order below:

1. **Analyse** (finding step 1) until the mechanism is proved, write the analysis into the
   item as always.
2. **Choose the recommended option yourself** — when the recommendation is one the user
   would make without thinking twice (see *What to decide alone*). Write "option X CHOSEN
   <date> by /finding-auto" into the item, so the list shows who chose.
3. **Build** (finding steps 2–3): worktree off fresh develop, guard against the real
   source and dev (read-only), tests + ITs + mutants, unit suite, ruff, docs and checklist
   lesson, one PR per topic into develop, `gh pr checks --watch` and its exit code.
4. **Merge** (finding step 4) once CI is green: squash, delete the branch, pull develop,
   watch develop's run **for the merge commit's sha** (`gh run list --json databaseId,headSha`
   and select the sha — the newest row can still be the previous run), update the item
   ("MERGED <date> hh:mm (BE #N <sha>, <M> min from the item's start; verify at the next
   rebuild)") and the state line (develop sha, how far main is behind). The item's start is
   the clock reading when its analysis began; write it into the item then, so the minutes
   survive a compaction.
5. **Next item**, until the stop rule below says a rebuild is the better next step.

Never: fast-forward main, start a rebuild, write to dev, edit Wikidata, trigger a deploy.
Those stay the user's word, exactly as in `finding` and `rebuild`.

## What to decide alone — and what not

Decide alone when ALL of these hold:
- The analysis proved the mechanism (not a plausible story) and names one recommended
  option whose alternatives are rejected for a stated reason (data-dependent, order-
  dependent, out of scope), as items 54, 55, 56 and 58 were.
- The fix is a correction of code or of the kit: a scraper, resolver, writer, dedup, snapshot
  or diff behaving against its own documented intent. "Fix it properly" is the standing
  rule — the full fix, not a patch.
- Nothing the user decided earlier is reversed or re-proposed (check the memory: the
  parked, deferred and "do not re-propose" items — pending deals, size as a sort key,
  Wikidata-only entity types, the OC bridge, after-prod list).
- No new source, no licence or legal question, no personal-data path, no data-model
  change (a new vertex or edge type, a new hard-id scheme, a new node property with a
  policy behind it), no change to what a user sees that is not the correction of a wrong
  display.
- No committed heal or backfill (one-off dev repairs stay the user's word; a maintenance
  command like dedupe-* is fine).
- The guard agrees with the real source. A guard the source contradicts is a question.
- The scripts repo change, if any, is a scrape-target addition the fix needs (item 54's
  'T. Rowe Price Group, Inc.'): its own commit on that repo's main, named in the report.

**Stop and ask** (one `AskUserQuestion`, then end the turn; the run resumes on
`/finding-auto`) when any of these comes up:
- Two options are both defensible and differ in what the product does or stores — e.g.
  item 61's "which LEI of a two-LEI Wikidata item does the pointer take", item 60's "should
  Wikidata create a `wd:` node for an item with no register pointer at all".
- The fix needs a data-model change, a new source, a heal, a cost (a paid API, a bigger
  box), or touches licensing or personal data.
- A security issue (red Dependency audit, Dependabot alert) whose only fix is a major-version
  override or an exception — the ground rule says ask; a plain bump is done at once, in its
  own PR, before the finding's PR.
- CI fails for a reason outside the change (a flaky live test, a runner outage) twice.
- The analysis cannot prove the mechanism after the real-source checks and the two
  rebuilds' evidence — report what is proved and what is not, and ask whether to build on
  the best explanation or wait for the next rebuild.
- An item would reverse or widen a decision of the user's.

Put the question in a sentence the user can answer with one word, name the item, the two
or three options with their consequence, and your recommendation first. Write
"WAITING ON USER: <question>" into the item so a later run sees it and skips the item.

## When a rebuild is the better next step — the stop rule

Stop the run and recommend `/rebuild` when the first of these is true:
1. **No item is left that can be worked alone**: every open item is MERGED (waiting for
   the verify), WAITING ON USER, or needs the next rebuild's evidence to be analysed (a side
   finding whose cause can only be seen in a fresh `after.json`).
2. **The next fix would blur the verify**: it changes the same path a merged, unverified
   item changed (the same runner phase, the same resolver rank, the same writer), so the
   rebuild could no longer tell which fix did what. Three or four merged items on
   different paths (54 retries/A2, 55 person dedup, 56 kit, 58 Exhibit 21 own row) are
   fine; a second fix on the Exhibit 21 own-row rule before 58 is verified is not.
3. **Half the open items depend on verifying a merged one** (item 59's EDGAR name search
   only matters once item 54's resolver change shows which node the targets land on).

Then report (see below) and stop. Never run the rebuild yourself.

## Order of work

Take the items in the order the last merge report recommended; else: an item whose fix
other items wait on first (DEPENDS ON), then the side findings of the items just merged
(they share the evidence already gathered), then the rest by number. A security issue
that appears goes first, always.

## Budget and pace

One item is roughly an hour of work and one PR — the per-item minutes recorded at each
merge are the measure, and a run whose items take far longer is worth a line in the final
report. Keep going across context compactions:
the findings list is the state, and every step writes it before moving on, so a run that
loses its context continues from the list. Say in one line what you are doing when you
move from one item to the next. Do not parallelise items: they share dev reads, the IT
container (3.9 GB RAM, never chain the unit suite and an IT run) and develop.

## Patterns from items 54–58 (the shape every fix takes)

- **Prove, then build.** Item 58 was proved by parsing the real exhibit on EDGAR with and
  without the alias (34 vs 35 rows) before a line was changed; item 55 by a full dedup
  scan on dev that found exactly the two stranded pairs; item 56 by counting the 37
  unnamed edge owners in the two snapshots.
- **The guard replays the change against everything it could touch, old vs new, and
  diffs.** Item 58 re-parsed every phase-C filer's exhibit with the old and new name set
  (182 targets; only the two expected rows changed — and the second one, WesBanco, was a
  find). Item 56 took a snapshot with the new kit and self-diffed it with re-minted uuids
  (0/0). Item 54 resolved all 208 targets on dev before and after the ranking change.
  Run the old side from the main checkout and the new from the worktree when the code
  itself changed; read dev only.
- **Tests at three levels**: unit cases next to the existing ones (parametrised rows for
  a comparison function), an integration test against the real ArcadeDB for every query or
  write path (mock only EDGAR/GLEIF/Wikidata), and the kit's tests for kit changes.
  A unit test that runs a scraper must not reach the database.
- **Mutants by counts.** Each guard of the fix is reverted on its own and must turn at
  least one test red; the pass/fail counts decide, never a summary line; files are restored
  from `.bak` copies and `git status --short` is checked afterwards. A mutant that survives
  is either pinned by a new test (item 58's possessive) or named in the report as not a
  guard.
- **Docs in the same PR**: the deep-dive in `docs/` where the behaviour is described, the
  README where it describes it, a lesson in `docs/new-source-checklist.md` written as the
  rule plus the case that taught it, the rebuild skill for kit changes.
- **One PR per topic**, title "<what>: <why> (findings item N)", body with Finding / Cause /
  Change (and what stays) / Guard / Checks / Next rebuild / Docs, the session's
  attribution trailer. A fix plus a scrape-target change is two commits in two repos.
- **The report after each merge** names the PR, the verify expectation written into the
  item (ids, counts), side findings added, and how far main is behind develop.

## Findings-list upkeep in this mode

As in `finding`, plus: every choice this skill makes is written as "CHOSEN by
/finding-auto" with the one-line reason; every question as "WAITING ON USER"; the state
line gets "finding-auto run <date> hh:mm→hh:mm Vienna (<N> min): items done …, stopped
because <rule>" — the START written when the run began, the end and the minutes when it
stops; a run with a RESUMED time lists both spans. A new finding
met on the way goes on the list at once with its evidence, "not analysed", and is worked
in its turn if the rules allow, never fixed inside the current change.

## The final report

When the run stops — on the stop rule or on a question — read the clock again, write the
end time and the minutes into the state line, and report in one message:
- the run's start and end time (Vienna) and its length in minutes, first;
- the items merged in this run, each with its PR, its minutes and the one-line verify
  expectation;
- the items waiting on the user, each with its question;
- the items left open and why they wait for the rebuild;
- develop's sha, how far main is behind, and the recommendation (`/rebuild`, then
  `/finding verify`, then the ff of main on the user's word).
