# Deduplication — design notes

How Pamten finds and resolves duplicates. Three kinds, each with its own signals:

- **Persons** — same person named differently across sources (`app/routers/persons.py`). Covered below.
- **Entities** — the same company as multiple nodes (`app/scraper/maintenance.py`). See [Entity deduplication](#entity-deduplication).
- **OWNS edges** — the same ownership fact recorded twice. See [OWNS edge deduplication](#owns-edge-deduplication).

# Person deduplication

Implementation lives in `app/routers/persons.py`; the UI is the Scraper tab's
**Review duplicate persons** modal.

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


# Entity deduplication

Two ways the same company becomes multiple `Entity` nodes, with different fixes.

## Same identifier, two nodes — `deduplicate_entities`

The old recordId-keyed BODS importer could create two nodes for one company. These
**share an LEI / Companies House id**, so `deduplicate_entities` merges them by that
id (sharded by id prefix to stay under ArcadeDB's query-heap cap). Exposed at
`POST /scraper/deduplicate-entities` (background job; `strategy=bulk` deletes losers,
`strategy=merge` migrates edges first).

## Same company, different identifiers — detection only

The current importer keys each entity on its LEI/CH id (`bods._entity_node_id`), so
the same company recorded under **two different LEIs** becomes two nodes the id-based
dedup can't see. Example: BlackRock, Inc. as `549300…` (US) and `529900…` (German).
An LEI's first four chars are the **issuing LOU, not the country**, so different
prefixes don't imply different companies — but two *distinct* LEIs do mean GLEIF
treats them as distinct legal entities, so `name_normalized` alone can't decide.

`maintenance.find_duplicate_entity_names` groups entities by `name_normalized`
(sharded server-side by name prefix — a global `GROUP BY` over millions of names
OOMs the heap) and tags each group with a **confidence** it's the same company:

| Confidence | Signal |
|---|---|
| `definitive` | members share a `wikidata_id` / `sec_cik` / `companies_house_id` |
| `high` | same `registered_address` (GLEIF registered office) |
| `medium` | same `country` **and** `founded` year |
| `low` | name only — differing address/country ⇒ probably *different* firms |

`registered_address` is captured at import from GLEIF `recordDetails.addresses`
(`bods._registered_address`, normalized, indexed) — it's the discriminator: same
name **+ same registered address** ⇒ merge; same name **+ different address** ⇒
leave alone. It populates on the next import.

This **detects, it does not auto-merge** (a name clash isn't a duplicate). Surfaced
in the BODS import result as `duplicate_names`, via `GET /scraper/duplicate-entities/{name-count,name-candidates}`
(`?min_confidence=`), and `manage.py duplicate-names`. Merging a confirmed group is
manual — reuse the labelled, fast `maintenance._migrate_entity_edges` (copy identity
fields, drop self-loops, then `deduplicate_owns_edges`).


# OWNS edge deduplication

A single BODS relationship statement can list several `interests` for the same
owner→owned pair (e.g. voting rights + board appointment, both → "controlling"; or
GLEIF direct + ultimate consolidation). The importer used to emit one OWNS edge per
interest, so **one import** created duplicate edges (fixed at the source — it now
collapses interests to one edge per `ownership_type`, keeping the largest stake).

To clean existing duplicates, `maintenance.deduplicate_owns_edges` pages active OWNS
edges by `@rid`, groups by (`@out`, `@in`) **in Python** (a global `GROUP BY` OOMs),
keeps the largest-stake edge per pair, and deletes the rest by `DELETE FROM <rid>`
(direct record access — `WHERE @rid = …` scans the whole edge type). Read-only count
at `GET /scraper/duplicate-edges/count`; collapse at `POST /scraper/deduplicate-edges`.
The entity profile also dedupes owners/subsidiaries by node id at read time, so the
UI never shows a node twice even before the DB is cleaned.
