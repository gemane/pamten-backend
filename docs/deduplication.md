# Person deduplication — design notes

How Pamten finds and resolves duplicate `Person` nodes. Implementation lives in
`app/routers/persons.py`; the UI is the Scraper tab's **Review duplicate persons**
modal.

## Why duplicates happen

Different sources name the same person differently, and none of them agree:

| Source | "Larry Page" appears as | "Bill Gates" appears as |
|---|---|---|
| Wikidata | `Larry Page` (+ aliases) | `Bill Gates` (+ aliases) |
| SEC EDGAR (Form 3/4) | `Page Lawrence` (last-first) | `Gates William H Iii` |
| BODS / manual | `Lawrence Page` | `William H. Gates III` |

Because the scrapers upsert people by `wikidata_id` **or** exact `full_name`,
these land as separate nodes. External-id reconciliation (the trick that works
for *entities* via LEI/CIK/QID) doesn't save us here: most SEC-only filers have
no shared identifier. So person identity has to be resolved on the **names
themselves** — and **every scrape recreates the duplicates**, which is why
auto-merge runs as part of scraping (below).

## The scan — `GET /persons/duplicates`

`scan_duplicate_groups()` loads every person once, then buckets them by three
independent signals. A bucket with ≥2 people becomes a candidate group.

### Signal 1 — name/alias token set (primary)

`_name_key(name)` reduces a name to an **order/case/honorific-insensitive token
tuple**: lowercase, split on non-alphanumerics, drop honorifics (mr/dr/…), sort.
So `Page Lawrence` and `Lawrence Page` both key to `(lawrence, page)`.

Crucially, each person is indexed under the key of their `full_name` **and every
Wikidata alias**. That's what links a node stored under a legal name to a
common-name node:

```
"Gates William H Iii"  → key (gates, h, iii, william)
"Bill Gates"           → keys (bill, gates)  AND  (gates, h, iii, william)   ← alias "William H. Gates III"
                                              └── shared bucket → matched
```

Distinctiveness is judged on the token set that *actually matched* (a hit on the
4-token alias is "distinctive" even though "Bill Gates" is only two tokens).

### Signal 2 — birth date + place

Same `(birth_date, normalized birth_place)` → same person across unrelated name
spellings (e.g. "Larry Page" / "Lawrence Page" if both carry a birth date).
Birth place may be missing (BODS gives date only); a shared date alone still
counts.

### Signal 3 — surname + shared company + compatible given name

Catches nickname/legal-name variants that share **no** token set or birth date —
`Larry`/`Laurence` Fink, `Rob`/`Robert` Kapito. Two people qualify when they:

1. share a surname (`_surname_key` — parsed `last_name`, else the final name token), **and**
2. have **compatible given names** (`_first_compatible`), **and**
3. share a connected company (checked in `_emit`).

`_first_compatible(a, b)` is deliberately lenient — exact match, a small
nickname map (`_NICKNAMES`: Bob↔Robert, Larry↔Lawrence, …), a prefix
(Dave↔David), or a shared two-letter stem (Larry↔Laurence). The **shared-company
requirement is the gate**: without it, everyone named "Smith" would match. With
it, siblings like **Elon/Kimbal Musk** are *not* flagged (different given names,
incompatible), while variants of one person are.

### Confidence

`_emit` ranks each group:

| Confidence | When |
|---|---|
| `high` | name/alias-token match **and** (a shared company **or** a shared birth date) |
| `medium` | distinctive (3+ token) name match, **or** a surname+company variant |
| `low` | common 2-token name match, no corroboration |
| + `likely_distinct` | conflicting birth dates on an otherwise-matching group ⇒ probably different people (Keith vs Rupert Murdoch) — never auto-merged |

Groups already confirmed distinct via keep-separate (below) are dropped from the
scan (`_all_pairs_dismissed`).

## Auto-merge — `POST /persons/deduplicate` (+ during scraping)

`deduplicate_high_confidence(apply=True)` runs the scan and merges **only
`high`-confidence, non-`likely_distinct`** groups; everything else is returned
under `needs_review`. It runs:

- on demand via `POST /persons/deduplicate` (`apply=false` = dry run), and
- automatically after every `run-all` scrape, gated by `SCRAPER_AUTODEDUP_ENABLED`
  (default on) — best-effort, so a dedup failure never fails the scrape.

Medium/low and father/son cases are left for a human, deliberately.

## Merging — `POST /persons/merge`

`merge_person_records(keep, dup)` folds `dup` into `keep`:

1. **Re-home edges** — `OWNS` (`OWNS_PROPS`) folds onto the kept person's edge to
   the same target with `COALESCE` (blanks backfilled, existing values kept);
   `HAS_ROLE` (`ROLE_PROPS`) is created (distinct tenures dedupe on display);
   `RELATED_TO` folds in both directions.
2. **Backfill bio** — `BIO_COALESCE` fields (`wikidata_id`, `sec_cik`, birth/death,
   `wikipedia_url`) fill only where the kept person is blank.
3. **Alias** — the dup's `full_name` and aliases become aliases of the kept
   person, so it stays findable (and feeds Signal 1 on the next scan).
4. **Log + delete** — write a `MergeLog` row, then `DETACH DELETE` the dup.

**Which node is kept** (`suggested_keep_id`): prefer a Wikidata node, then the
most-connected, then the shortest name.

### ArcadeDB gotcha (important)

The production ArcadeDB **silently no-ops** Cypher that reads a *second* edge's or
node's properties in the same statement — `properties(r)`, `COALESCE(a.x, b.x)`
across nodes. So the merge **reads the dup's edges into Python first**, then writes
them onto the kept person with bound `$params` (`SET nr.x = COALESCE(nr.x, $x)`).
Only proven patterns are used: `CREATE`/`MERGE` with params and single-node
`COALESCE(existing, $param)`.

## Keep-separate — confirmed-different people

`POST /persons/keep-separate {ids}` writes a `NOT_DUPLICATE` edge between every
pair in the group; the scan then drops any group whose pairs are all marked
(`_all_pairs_dismissed`). Reversible via `DELETE /persons/keep-separate`; listed
via `GET /persons/kept-separate`. This is how Keith Murdoch stays distinct from
his son Rupert without being re-suggested on every scrape.

## Merge log — "already merged"

Every merge upserts a `MergeLog` vertex keyed on `(keep_id, dup_name)`, so
repeated auto-merges of a re-scraped node bump `count`/`at` instead of piling up
rows. `GET /persons/merge-log` returns them newest-first.

## Endpoint summary

| Endpoint | Purpose |
|---|---|
| `GET /persons/duplicates` | Scan; returns groups with confidence + suggested keep |
| `POST /persons/deduplicate` | Auto-merge high-confidence (`apply=false` = dry run) |
| `POST /persons/merge` | Merge one pair (`keep_id`, `dup_id`) |
| `POST` / `DELETE /persons/keep-separate` | Mark / unmark a group as distinct |
| `GET /persons/kept-separate` | The "not to be merged" list |
| `GET /persons/merge-log` | The "already merged" history |
