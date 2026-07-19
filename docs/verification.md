# Import verification — Phase A design notes

> **Status: backend implemented; frontend pending.** The `moderator` role, the
> `Flag` node, and the endpoints (`POST /flags`, `GET /flags`, `GET
> /flags/summary`, `PATCH /flags/{id}`) are live — see
> [api-reference.md](api-reference.md). The ⚑ Report control, disputed badge, and
> moderator queue UI are the remaining frontend slice.

## The problem

Almost every node and edge in Pamten comes from a scraper (Wikidata, SEC EDGAR,
OpenCorporates, BODS). Scrapers are wrong sometimes — a bad name match, a stale
ownership %, a person who isn't really an officer, an entity that shouldn't
exist. Today a reader who spots this has nowhere to say so, and we have no list
of what's disputed.

Phase A gives readers a **"⚑ Report" action** and gives admins **a queue of
what's been reported**. That's it — capture and surface. Actually *correcting*
the data is Phase B (see [Non-goals](#non-goals-phase-b-and-beyond)).

## Why not "just fix it"

The obvious answer — let someone edit the value in our database — is a dead end
in a scraper-first system:

- **Fixing in place gets clobbered.** The importer backfills onto existing edges
  and refreshes `last_scraped_at` on every run. A hand-edited `stake_percent`
  would be silently overwritten by the next scrape.
- **We can't fix at the source.** We don't own SEC EDGAR, GLEIF, UK PSC, or
  OpenCorporates. The one editable source is **Wikidata**, where the right move
  is a *deep link* ("suggest a correction upstream"), not us writing to it.

So corrections live in **our** database, but as a **separate overlay** that is
kept apart from scraped facts and survives re-scrapes. This is the same shape as
the dedup **keep-separate / merge log** ([deduplication.md](deduplication.md)):
a user decision the scraper is taught to respect on every subsequent run. Phase A
builds the *reporting* half of that overlay; Phase B builds the *resolution* half.

`Flag` (user-submitted, "this looks wrong") is deliberately distinct from the
`Conflict` node in the data model ([data-model.md](data-model.md)), which is for
*system-detected* disagreement between two sources. Phase A is `Flag` only.

## Data model — the `Flag` node

One vertex per report:

| Property | Meaning |
|---|---|
| `id` | uuid |
| `target_kind` | `owns` \| `role` \| `entity` \| `person` |
| `from_id`, `to_id` | endpoints, for edge targets (`owns`/`role`) |
| `role` | discriminator for `role` edges (a person can hold several) |
| `node_id` | the node, for node targets (`entity`/`person`) |
| `category` | `wrong-owner` \| `wrong-percent` \| `wrong-role` \| `not-real` \| `outdated` \| `duplicate` \| `other` |
| `note` | optional free text (bounded length) |
| `status` | `open` \| `reviewing` \| `resolved` \| `rejected` |
| `reporter_kind` | `user` \| `anon` |
| `reporter_id` | user id when logged in; else null |
| `reporter_fp` | salted hash of client IP — abuse control only, never displayed |
| `created_at`, `updated_at` | ISO timestamps |

### Addressing an edge stably

Edges have no user-facing id, but the importer already treats them by a **natural
key** — an active `OWNS` is matched on `(from_id, to_id, until IS NULL)`, a
`HAS_ROLE` additionally on `role`. A `Flag` reuses that same composite key, so a
flag stays attached to *the same relationship* across re-scrapes even though the
edge's RID may change. (A future option is stamping a stable `edge_id` uuid on
edges at import time; not needed for Phase A.)

## Who can flag — **anonymous, rate-limited** (open decision 1, resolved)

Anyone can file a report — **anonymously or logged in**. A logged-in user of any
role (`viewer`, `contributor`, `moderator`, `admin`) can flag; being signed in
just raises their rate ceiling and attaches `reporter_id` for accountability.
Rationale: more coverage, and a flag on a person's data is effectively a GDPR
rectification/objection intake we *want* to make frictionless (see
[below](#gdpr)). Abuse is contained rather than prevented by a login wall:

- **Rate limit** anonymous `POST /flags` per `reporter_fp` (salted-hashed IP) —
  **2/hour**, tunable via config. Logged-in users get a higher ceiling.
- **Collapse duplicates.** A repeat `(target, category)` from the same
  `reporter_fp` doesn't create a second row; the queue shows a **count**
  ("12 reports") per target+category, not 12 rows.
- **Cap open flags per target** so one target can't be spammed into noise.
- `reporter_fp` is a salted hash (not the raw IP), stored only for abuse control,
  with a short retention window.

## Who can moderate — new `moderator` role

Reviewing the queue is gated to a **new `moderator` role**, added alongside the
existing `admin` / `contributor` / `viewer` (see the auth roles in `app/auth/`).
`admin` implies moderator (admins can always moderate); a plain `contributor` or
`viewer` **cannot**. This keeps day-to-day flag triage delegable without handing
out full admin. Implementation: extend the role enum and add a
`require_moderator` dependency (satisfied by `moderator` **or** `admin`) in
`app/auth/dependencies.py`, mirroring `require_admin` / `require_contributor`.

## Endpoints (Phase A)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/flags` | public — anonymous *or* any logged-in user (rate-limited) | file a report: `{target, category, note?}` |
| `GET` | `/flags` | moderator (or admin) | the moderation queue; filter `?status=`, `?target_kind=`, `?category=` |
| `GET` | `/flags/summary` | public | open-flag counts per target, for the "disputed" badge |
| `PATCH` | `/flags/{id}` | moderator (or admin) | status transitions: `open ⇄ reviewing`, `→ rejected` |

`POST /flags` is open to everyone — signing in is optional and only affects the
rate ceiling and whether `reporter_id` is recorded. The queue and status changes
require the `moderator` role (`admin` also qualifies).

`resolved` is reachable only once Phase B resolution actions exist; Phase A can
`reject` (source is correct / not actionable) and move things to `reviewing`.

## Read-side effect — the "disputed" badge

A target with ≥1 `open` flag is **disputed**, and that's useful *before* anyone
resolves anything:

- `GET /search/entity/{id}/full-profile` and the edge payloads gain an open-flag
  count per node/edge (from `/flags/summary`, joined at read time).
- The frontend shows a **"⚑ Disputed (n)"** badge on the node/edge in `NodePanel`.
- Optionally **dock the displayed credibility** for disputed edges. Compute this
  at read time — **do not** mutate the stored `credibility_score`; the scraper
  owns that field.

## Frontend (Phase A)

- **Report affordance** — a "⚑ Report" control on edges and nodes in
  `NodePanel.tsx`; opens a small category picker + optional note. Works
  logged-out.
- **Disputed badge** — rendered wherever an edge/node is shown once its open-flag
  count > 0.
- **Review queue** — a **moderator** panel (visible to `moderator`/`admin`)
  listing open flags with target, category, count, note, and reporter kind; reuse
  the pattern from `DuplicatesModal.tsx` / `ScraperActivity.tsx`. Actions in
  Phase A: **mark reviewing**, **reject**.
- i18n: all new strings added to `src/i18n/locales/{en,de,es}.json`.

## GDPR

`Person` edges are personal data, so a report on one **is a right-to-rectification
/ right-to-object request**. This is a reason *to* build the feature — it gives us
a compliant intake and audit trail — but it shapes Phase A:

- Capture `reporter_kind` + timestamps + status history from the start (audit).
- Keep `reporter_fp` a salted hash with a short retention window; never surface it.
- Design so a person's data can later be **suppressed** on request even if a
  source keeps re-reporting it (the Phase-B suppress override). Phase A must at
  least *record* such a request as a flag with `category = other` + note.

Flag anything touching named individuals for a compliance check before shipping,
per the project constraints.

## Testing

Per project convention (mocked unit tests + real-ArcadeDB integration —
[deduplication.md](deduplication.md), `tests/integration/`):

- **Unit:** flag creation + validation; edge natural-key addressing; rate-limit
  and duplicate-collapse logic; queue filtering; `/flags/summary` counts; badge
  count surfaced in the profile payload.
- **Integration (real ArcadeDB):** `Flag` vertex create/query, `summary`
  aggregation, and that a re-scrape of a flagged edge leaves its flags intact
  (the survives-re-scrape guarantee). Run via
  `tests/integration/arcadedb-it.sh test`.
- **Frontend (Vitest):** report control renders logged-out; disputed badge
  appears when count > 0; queue actions call the right endpoints.

## Phase B — resolution

- **Suppress (implemented).** `POST /flags/{id}/suppress` (moderator) resolves an
  *edge* flag: it deletes the wrong OWNS/HAS_ROLE edge now and records a
  `Suppression` override keyed by the edge's natural key. Enforcement is
  **read-time** (`app/suppressions.py`): the read endpoints (`full-profile`,
  `person/full-profile`, `/relationships/owners`) load the small suppression set
  and drop matching edges — so a suppressed edge stays hidden even if a later
  import recreates it. `GET /flags/suppressions` lists them; `DELETE
  /flags/suppressions/{id}` un-suppresses. (Node suppression + graph-tree/expand
  filtering are not yet covered.)
- **Pin (deferred)** — a corrected value re-scrape treats as higher-authority;
  deferred until real flag categories are observed.

## Non-goals (beyond Phase B)
- **Manual data entry** — adding entities/people/relationships by hand
  (postponed indefinitely; the focus is the scraper).
- **System-detected source conflicts** — the `Conflict` node; separate feature.
