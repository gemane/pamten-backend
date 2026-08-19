# Owlgraph Backend

![CI](https://github.com/gemane/pamten-backend/actions/workflows/ci.yml/badge.svg)
[![Licence: MIT](https://img.shields.io/badge/Code-MIT-yellow.svg)](LICENSE)
[![Data Licence: ODbL](https://img.shields.io/badge/Data-ODbL-brightgreen.svg)](DATA_LICENSE.md)

FastAPI backend for the Owlgraph ownership mapping platform. Stores corporate ownership hierarchies in an ArcadeDB graph database and exposes a REST API consumed by the frontend.

**Live API (dev):** https://api-dev.owlgraph.org  
**Docs (Swagger):** https://api-dev.owlgraph.org/docs  
**Frontend (dev):** https://dev.owlgraph.org

The `*.onrender.com` URLs still serve the same deployments, but the owlgraph.org
domains are canonical and are the only origins in `CORS_ORIGINS` — reaching the
frontend on its onrender URL loads the page but every API call fails CORS.

---

## Branch model

Two long-lived branches:

| Branch | Deploys to | Purpose |
|---|---|---|
| `develop` | Render (dev) — auto-deploy on push | Integration branch. Everything lands here first and runs against the dev database. |
| `main` | nothing yet (production, once it exists) | Only ever contains code that has been verified running on the dev deploy. |

The flow is: **feature branch → PR into `develop` → verify on the dev deploy → fast-forward `main` to `develop`**. `develop` is the repository's **default branch**, so a new PR targets it automatically.

Promotion is a fast-forward, never a merge or squash, so `main` is always literally a commit that already exists on `develop` — the two histories can't drift:

```bash
git checkout main && git pull
git merge --ff-only origin/develop
git push origin main
```

If `--ff-only` refuses, `main` has picked up a commit `develop` doesn't have (a merge commit from a promotion done the old way, say). Fix the ancestry once by merging `main` into `develop` — don't force-push `main`.

Both branches are protected by a repository **ruleset** (Settings → Rules → Rulesets — *not* the older Settings → Branches protection, which is unused here):

- `Tests`, `Lint`, and `Integration (real ArcadeDB)` are required on both. Context names must match the job `name:` in `.github/workflows/ci.yml` **exactly**, or the check is silently never required.
- `develop` requires a pull request; direct pushes to it are rejected. No approving review is needed, so a solo maintainer can self-merge a green PR.
- `main` takes **no pull request** — that's what makes the fast-forward push possible, since GitHub's merge button has no fast-forward option. It is not a hole: a push is accepted only if that exact commit already passed all required checks, which only happens after it ran on `develop`. Pushing anything unverified is rejected with *"3 of 3 required status checks are expected"*.
- `main` has **no bypass actors** — admins included. Force-pushes and deletion are blocked on both branches.
- `develop` allows an admin to force-merge a red PR when a dev-only experiment warrants it.

CI runs on pushes and PRs to both branches.

---

## Tech stack

| Layer | Library |
|---|---|
| Framework | FastAPI 0.111 |
| Database | ArcadeDB (graph, Cypher-compatible) |
| Auth | PyJWT + passlib/bcrypt |
| HTTP client | httpx |
| Config | pydantic-settings |
| Server | Uvicorn |
| Hosting | Render (web service) |

---

## Getting started

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload   # http://localhost:8000
```

Create a `.env` file with your credentials:

```env
ARCADEDB_URL=http://<your-instance>:2480
ARCADEDB_USERNAME=root
ARCADEDB_PASSWORD=<password>
ARCADEDB_DATABASE=owlgraph
SECRET_KEY=<long-random-string>
SCRAPER_ENABLED=false
SCRAPER_SEC_EDGAR_ENABLED=false
```

---

## Project structure

```
backend/
└── app/
    ├── main.py              # FastAPI app, CORS, router registration
    ├── config.py            # Settings loaded from environment variables
    ├── database.py          # ArcadeDB HTTP client + Neo4j-compatible shim
    ├── models/              # Pydantic request/response models
    │   ├── entity.py
    │   ├── person.py
    │   ├── location.py
    │   ├── relationship.py
    │   └── source.py
    ├── routers/             # REST endpoints
    │   ├── entities.py
    │   ├── persons.py
    │   ├── relationships.py
    │   ├── search.py
    │   └── sources.py
    ├── auth/                # JWT authentication
    │   ├── router.py        # /auth/register, /auth/login, /auth/me
    │   ├── security.py      # Password hashing, token create/decode
    │   └── dependencies.py  # FastAPI Depends: get_current_user, require_admin, etc.
    └── scraper/
        ├── router.py        # All /scraper/* endpoints
        ├── sources.py       # Per-source toggle switches
        ├── runner.py        # Orchestration: search → fetch → write to DB
        ├── wikidata.py      # Wikidata SPARQL client
        ├── sec_edgar.py     # SEC EDGAR scraper (ownership filings + executives)
        ├── open_corporates.py  # OpenCorporates client (requires API key)
        └── mapper.py        # Entity type inference, name normalisation
```

---

## Data model

Nodes (`Entity`, `Person`, `Source`, `Claim`, `MergeLog`, `Peer`, `ScrapeRun`,
`ScraperSource`, `User`) and their edges (`OWNS`, `HAS_ROLE`, `RELATED_TO`,
`NOT_DUPLICATE`, `DUAL_LISTED_WITH`, location edges) with all properties:
**[`docs/data-model.md`](docs/data-model.md)**.

---

## API

The full REST reference — Auth, Entities, Persons (incl. deduplication), Search,
Sources, Relationships, Scraper, Federation, and maintenance/advanced endpoints —
lives in **[`docs/api-reference.md`](docs/api-reference.md)**. An interactive
version is served at `/docs` (Swagger) and `/redoc` on a running instance.

**Everything is served under `/v1`** — `/v1/search/?q=…`, `/v1/auth/login`, and so on. The same routes are *also* still served unversioned (`/search/?q=…`), hidden from the schema and deprecated: released clients pin the path they shipped with, and federation peers call a hardcoded `{base_url}/federation/export`, so the old mount stays until every known caller has moved. New clients should use `/v1`; new endpoints get both mounts automatically.

`/` and `/health` are deliberately **not** versioned — uptime monitoring and Render's health check need a URL that never moves.

---


## Authentication

Access tokens are JWTs signed with `SECRET_KEY` (HS256, **15-minute** expiry — `ACCESS_TOKEN_EXPIRE_MINUTES`). Nothing is consulted when one is presented, so the TTL *is* the blast radius of a stolen token — hence a short one. `SECRET_KEY` must be **at least 32 characters**; the app refuses to start with a shorter one, in any mode, since short HS256 keys are brute-forceable. Set a strong random key in production:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**Sessions (refresh tokens).** A 15-minute access token would mean logging in four times an hour, so sessions are carried by a separate refresh token — the opposite shape to a JWT: opaque, stored server-side (hashed, in `RefreshToken`), and therefore **revocable**. It is delivered as an `httpOnly` cookie, so JavaScript cannot read it and an XSS bug cannot steal the long-lived credential.

`POST /auth/refresh` trades the cookie for a new access token and **rotates** the cookie; `POST /auth/logout` revokes it. Both are unauthenticated by design — they are called exactly when the access token has expired. Refreshing re-reads the account, so a role change or a deletion takes effect within one access-token lifetime rather than lingering for the life of the session.

Rotation makes theft detectable. A stolen token works only until the real client refreshes; whoever refreshes second presents an already-consumed token, which cannot happen normally. That burns the whole **family** (the chain of successors from one login), logging out thief and victim alike — other sessions, being separate families, are untouched. Session lifetime is **absolute** (`REFRESH_TOKEN_EXPIRE_DAYS`, default 30): rotation renews the secret, not the clock.

Sessions end on password change (other sessions only — the caller stays signed in), password reset (all), and account deletion (rows erased, not just revoked). Already-issued access tokens are the exception in every case: they are checked against nothing, so another session stays usable until its token expires. Bounding that window is what the short TTL is for.

> **Same-site requirement.** The cookie is `SameSite=Lax`, so the frontend and API must share one registrable domain — `dev.owlgraph.org` + `api-dev.owlgraph.org`. It is *not* sent from a differently-registered origin, so a frontend dev server on `localhost` talking to a deployed API gets no session refresh and falls back to re-login every 15 minutes; run the backend locally (with `REFRESH_COOKIE_SECURE=false` for plain http) to avoid that. The old `*.onrender.com` pair could never have worked: `onrender.com` is on the Public Suffix List, making those two hosts separate sites.

**The admin account.** Set `ADMIN_EMAIL` + `ADMIN_PASSWORD` and that account is provisioned as admin on every startup (created if it doesn't exist, from the hashed `ADMIN_PASSWORD`; never overwritten if it already exists). With `ADMIN_EMAIL` set, self-registration only ever creates `viewer`s. This is the recommended way to get an admin on a fresh database — it avoids the race where, on an empty DB, whoever hits the public `/auth/register` first would become admin. If `ADMIN_EMAIL` is **not** set, the legacy fallback applies: the first account to register becomes admin.

**Email verification & password reset.** Registration creates the account with `email_verified=false`, emails a verification link, and — with `REQUIRE_EMAIL_VERIFICATION=true` (default) — **blocks login until the email is verified** (`403 email_not_verified`, which the UI turns into a *resend* prompt). `POST /auth/forgot-password` emails a reset link and always returns `200` (no account-existence leak); `POST /auth/reset-password` sets the new password. The verify/reset links are **self-contained signed JWTs** (purpose-scoped, TTL-bounded) — no server-side token table; a reset link embeds a fingerprint of the current password hash so it **self-invalidates** once used. The env/bootstrap admin and the legacy first-user admin are stamped verified so they're never locked out; run `python manage.py verify-users` once to mark pre-existing accounts verified.

**Changing a password.** A signed-in user rotates their own with `POST /auth/change-password` (`{current_password, new_password}`) — proving ownership with the current password rather than an email round-trip, so it works even where outbound SMTP is blocked. Reusing the current password is rejected. Other sessions are revoked and the caller's is re-issued, so a change evicts anyone else holding the old password without logging you out of the browser you are using.

For the operator case — a password that must be rotated but nobody knows it, or an inbox that can't receive the reset mail — use the CLI:

```bash
python manage.py set-password someone@example.com   # prompts twice, hidden input
```

Note that `ADMIN_PASSWORD` is **not** a way to change a password: `bootstrap_admin()` only creates a *missing* account and never overwrites an existing one, so editing that env var on an account that already exists has no effect at all.

**Deleting an account.** `DELETE /auth/me` with `{password}` permanently deletes the caller's own account — required by both app stores for any app that allows account creation, and the mechanism behind a GDPR erasure request. It re-authenticates with the password, so a stolen access token alone isn't enough.

It removes the `User` node (and with it the password hash, TOTP secret and recovery codes) plus the account's rate-limit counters. Flags the user filed are **anonymised, not deleted** (`reporter_kind='deleted'`, reporter id and fingerprint cleared): the reports are about companies rather than about the reporter, so deleting them would silently rewrite moderation history — only the link back to the person is severed.

Two cases are refused rather than half-honoured:

- the **`ADMIN_EMAIL` bootstrap account**, because `bootstrap_admin()` recreates it on the next startup — the deletion would quietly undo itself. Unset `ADMIN_EMAIL` first.
- the **last remaining admin**, which would leave the instance with nobody able to administer it. Promote someone else first.

Admins can still delete *other* users with `DELETE /auth/users/{id}`; that path does not anonymise flags.

**Password policy.** The rules live in one place, `app/auth/password_policy.py` (`password_policy_error`), shared by register, reset, change, and `manage.py set-password`, so the API and CLI can't drift apart. User-chosen passwords must be **at least 8 characters** and are checked against a **common-password blocklist** — the top ~10k most common passwords (`app/auth/common_passwords.txt`, from [SecLists](https://github.com/danielmiessler/SecLists), MIT licence), so weak-but-long-enough choices like `password123` are rejected. There are deliberately **no character-composition rules** (per NIST SP 800-63B). Online guessing is further limited by login rate-limiting (5 attempts / 15 min) and optional TOTP MFA. The env `ADMIN_PASSWORD` bootstrap is exempt (operator-set, not user input).

**Email transport** is provider-agnostic (`app/notifications/email.py`) — `EMAIL_BACKEND` picks the backend: `resend` (Resend HTTPS API via `RESEND_API_KEY` — recommended for a real deploy, since **Render blocks outbound SMTP**), `smtp` (stdlib `smtplib`; works with Gmail locally), or the default `console` (logs the message + link, so local dev and tests need no credentials). Adding another provider is a ~15-line `EmailSender` subclass. `EMAIL_FROM` must be a sender the backend accepts (a Resend-verified domain, or your SMTP account); `APP_BASE_URL` sets the origin used in the emailed links.

**Two-factor auth (TOTP).** Users can enable an authenticator-app second factor (`POST /auth/mfa/setup` → scan the returned `otpauth://` QR → `POST /auth/mfa/enable` with a code, which returns 10 one-time **recovery codes**). Once enabled, `login` returns `{mfa_required, mfa_token}` instead of an access token; the client exchanges that pending token + a 6-digit code (or a recovery code) at `POST /auth/mfa/verify` for the real token. TOTP uses `cryptography` (RFC 6238, ±1 step for clock drift); the secret + hashed recovery codes live as schemaless `User` props. `mfa/verify` is rate-limited per account, and disabling requires a current code.

Protected routes use FastAPI `Depends`:

| Dependency | Requirement |
|---|---|
| `get_current_user` | Any valid JWT |
| `require_admin` | Role must be `admin` |
| `require_moderator` | Role must be `admin` or `moderator` (data-moderation / verification queue) |
| `require_contributor` | Role must be `admin` or `contributor` |

---

## Scrapers

Wikidata, SEC EDGAR, and OpenCorporates run per-company via `/scraper/run-all`;
BODS (GLEIF / UK PSC) is a separate bulk dataset import. Each source has an
independent on/off toggle (`/scraper/sources`).

🧩 **Adding a source:** [`docs/scraper-plugin-guide.md`](docs/scraper-plugin-guide.md) is a step-by-step guide (API module → source toggle → config flags → runner → endpoints → dedup) with a pre-deploy checklist. [`docs/new-source-checklist.md`](docs/new-source-checklist.md) covers the judgement calls around it — what to take from a source, how its records find the companies already in the graph, and the mistakes already made here.

### Wikidata
Imports corporate ownership data via SPARQL. For a company it fetches subsidiaries, owners, parent, executives and HQ, plus the **hard external identifiers** LEI (P1278) and SEC CIK (P5531) — stored as `lei_id` / `sec_cik`. Those matter beyond provenance: a Wikidata-sourced company and its GLEIF node otherwise share no identifier, so the deduplicator can't tell they're the same company and the two sit side by side (one holding the ownership graph, the other the executives). SEC's own LEI field is null for most operating companies, which makes P1278 the bridge that actually exists. A malformed value is dropped rather than stored — `lei_id` is a merge key, and a wrong one would fold unrelated companies together — and an identifier already on the node is never overwritten, since the issuing register outranks a crowd-edited source. It then recursively expands **down the subsidiary tree** to `depth` levels (0–3, default 2; owners/executives are recorded but not recursed). Each node expands up to 15 subsidiaries, so cost grows ~exponentially with depth — `depth=1` is fast (the company + its immediate relations), `depth=3` can mean thousands of Wikidata calls and hours for a large conglomerate (and risks rate-limiting). Only affects the Wikidata scrape, not the BODS bulk import. Controlled by `SCRAPER_ENABLED`.

- Searches Wikidata by company name, picks the best-matching entity
- Writes to the DB using upsert — safe to re-run, no duplicates
- Caps at 15 subsidiaries and 3 CEOs per entity
- 400 ms delay between requests (Wikidata rate limit)

### SEC EDGAR
Imports investor data from SC 13D/13G ownership filings and executive data from Form 3/4 XML. Controlled by `SCRAPER_SEC_EDGAR_ENABLED`.

**Two directions.** Those filings name the company as the *subject* — who owns it, who its insiders are. An institutional investor has none: Vanguard is privately held and isn't a listed issuer, so scraping it by name finds nothing however often you try. What it has is ~3,400 filings it makes **about others**, so the scraper also reads the filer side and writes `OWNS` edges pointing *out* of the company:

```bash
python manage.py sec-holdings 0002100119 --limit 200
python manage.py sec-holdings 0002100119 --succeeds 0000102909   # record a handover
```

A normal company scrape does this too, capped at `HOLDINGS_SCRAPE_LIMIT` subjects; a company that files no 13D/13G costs one extra JSON read and nothing more. EDGAR splits a prolific filer's index into `filings.recent` plus archive pages — Vanguard's old CIK has 13, reaching back to 1999 — so the archive is read lazily, newest page first and only while the fetch budget lasts. That matters for closing a position: an exit amendment sits in `recent` while the percentage it ends was disclosed years earlier, so without the archive the scraper sees an exit for a stake it has no record of. Walking back is slow by nature (measured: ~700 s to reach five closed holdings behind the ~1,800 exit amendments), so raise `--limit` deliberately. Modern filings are structured XML (`primary_doc.xml`), so the subject's CIK, its name and the percentage are read as fields rather than scraped out of HTML; pre-XML filings are skipped rather than guessed at.

**A 0% amendment is an exit, not a gap.** When a filer drops below the 5% threshold it amends to 0%, so the last real percentage is written with `until` set to the amendment date — history rather than a current position. This is not hypothetical: Vanguard moved its reporting from `VANGUARD GROUP INC` (CIK 0000102909) to `VANGUARD CAPITAL MANAGEMENT LLC` (0002100119) in spring 2026, closing ~1,800 positions with 0% amendments on the way out. Keeping only the newest filing per company would report that the old entity owns nothing — true, and useless. `--succeeds` records that handover as a `SUCCEEDED_BY` edge; it is given explicitly because inferring a corporate relationship from filing patterns would be a guess written into the graph as fact.

**Group structure from the 13F cover page.** A fund group files one 13F per manager, and the cover page names the group's *other* managers with their CIKs — ten for Vanguard, including the entity that took over its 13G reporting. That is an authoritative statement of group membership from the filer itself, and it costs one document fetch, so a SEC scrape records it as `(:Entity)-[:RELATED_TO {relation:'affiliate'}]->(:Entity)` with a link back to the filing.

Deliberately **not** an `OWNS` edge: "reports 13F holdings alongside" is the whole of what the form establishes, and writing it as ownership would invent a fact. It is also not `SUCCEEDED_BY` — a filer that hands its reporting to an affiliate has not been replaced, and Vanguard Group Inc went on filing after the change.

Note that the CIK a user searches for may be the retired one — which is exactly why holdings are keyed on CIK rather than name.

Company lookup uses a three-vector strategy to avoid false matches:

1. **`company_tickers.json`** — instant lookup for all US-listed companies
2. **`browse-edgar` name index** — EDGAR's registered company-name search; returns 0 results for companies not on EDGAR (Nestlé, Samsung, Volkswagen, etc.), preventing false positives from full-text matches
3. **EFTS full-text search** — last resort, guarded by a name-similarity check (SequenceMatcher ratio ≥ 0.55 after stripping legal suffixes)

Investor names are classified as Person or Entity using heuristics that recognise common legal suffixes including European forms (S.A.R.L., GmbH, S.A., N.V., AG, etc.).

📄 **Deep dive:** [`docs/sec_edgar_scraper.md`](docs/sec_edgar_scraper.md) — research and implementation notes (which EDGAR APIs, CIK resolution, 13D/13G & Form 3/4 parsing, per-company request budgets).

### OpenCorporates
Requires a paid API key (`OPENCORPORATES_API_KEY`). Disabled by default.

### Bulk ownership datasets (GLEIF & UK)
Beneficial-ownership data is loaded in bulk from current, authoritative sources —
**GLEIF** (Global LEI, corporate ownership worldwide, CC0) and the UK **Companies
House** register (people with significant control + the company register, Open
Government Licence). Controlled by `SCRAPER_BODS_GLEIF_ENABLED` /
`SCRAPER_BODS_UK_PSC_ENABLED`.

Unlike the per-company scrapers above, these are **bulk dataset imports**, not name
lookups — so they are *not* part of `run-all`. Because the source files are multi-GB,
they run from the **CLI** (`manage.py gleif-lei-cdf` / `gleif-rr` / `gleif-succession`
/ `gleif-repex` / `ch-psc` / `ch-company-data`) in a tmux session on the server, not
over HTTP. Both
sources still appear in `/scraper/sources` with independent on/off toggles, and
`/scraper/bods/status` reports their enabled state.

> These replaced the OpenOwnership **BODS** exports (GLEIF + UK PSC), which were
> frozen at 2025-03. The BODS importer and its `/scraper/bods/*/run` endpoints have
> been removed; only the CLI importers above remain.

**Daily GLEIF refresh (delta).** Re-running the full ~3.4M-record load every day is
wasteful, so once the full copy is loaded, `manage.py gleif-update` rides on top of
it: it fetches GLEIF's published **delta files** (only what changed since the last
publish — ~14k entities, ~2k relationships and ~3k reporting exceptions) and applies
them in seconds/minutes.
It is *retirement-aware* — a relationship that goes non-ACTIVE has its `OWNS` edge
**closed** (`until` set to the relationship's end date), and a dissolved LEI is
**marked** (`active=false`), never deleted (GLEIF never deletes; merges flow through
`SUCCEEDED_BY`). Writes are idempotent (re-applying a delta never duplicates), so no
`--bulk-load` and no whole-DB dedup. `~/scripts/cron-gleif-update.sh` (flock + log →
a `gleif-update` ScrapeRun) is the one crontab line for a daily run.

The delta rides **on top of** the full load, so `gleif-update` refuses to run until a
full load has baselined the graph (the full LEI-CDF import stamps a marker;
`wipe-source --source GLEIF` or dropping the database clears it, forcing a fresh full
load before deltas resume) — it never builds a partial
graph from deltas alone.

**A subset load does not count as that baseline.** A delta carries every record
GLEIF changed *worldwide*, so applying one whole to a curated test database
(`test-import.sh`, or any `--only-file` / `--limit` / `--jurisdiction` import) does
not refresh it — it imports the rest of the world into it: one night added 226,902
entity records and 18,720 edges to a 488-entity subset. The marker therefore
records a **scope**, and only `full` means "apply everything".

A subset still gets its delta, in **`--only-existing`** mode: records for companies
already in the database are applied, records for everything else are counted as
`not_here` and ignored. So a curated database stays current *and* keeps exercising
the delta path nightly, without growing. It is selected automatically from the
baseline scope; `--only-existing` / `--no-only-existing` forces either mode.

Both endpoints must be present for a relationship, and both ends for a succession
edge — the RR importer creates its endpoint nodes, so one relationship between two
unknown companies would otherwise drag both of them in. The trade-off is that a
genuinely new subsidiary of a tracked company is skipped too, so a subset drifts
slightly from GLEIF's truth; re-run `test-import.sh` against fresh golden copies to
resync.

The default `--interval auto` is **gap-aware**: it checkpoints the last GLEIF publish
it applied (an `ImportState` node) and, on each run, picks the smallest delta window
that still covers the gap since then — `LastDay` normally, escalating to `LastWeek` /
`LastMonth` if the box was off for a few days, so a missed run self-heals on the next
one. A gap wider than ~30 days can't be covered by a delta, so the run **fails loudly**
(telling you to full-reload) rather than silently under-applying. Pass an explicit
`--interval LastDay|LastWeek|LastMonth` to override.

**Bootstrap order:** run `full-import.sh` (loads the full copy + stamps the baseline
marker), then let the daily `gleif-update` cron take over — the first run cold-starts
`LastMonth` (reconciles up to a month), then settles into nightly `LastDay`.

UK PSC has no clean delta feed (Companies House publishes a daily *full* snapshot), so
its incremental refresh is a later, separate design.

---

## Merging entities

Duplicate companies are folded together by `deduplicate_entities` (shared LEI / Companies House id / CIK / Wikidata id) and, after every scrape, `deduplicate_entities_for` on just the entities that scrape touched — high-confidence groups only, the rest left for review.

The survivor is the highest-`name_credibility` node (then verified, then the lexically smallest id), so a register-sourced node with its stable `lei:` id normally wins over a scraped UUID. A merge carries three things across before deleting the loser:

- **its edges** — owners, subsidiaries and roles, skipping any the survivor already has;
- **its data** — identifiers (`wikidata_id`, `sec_cik`, …) and descriptive fields, filling gaps only; the survivor's own values always win, its name never changes, and the loser's name is added as an alias so the company stays findable under it;
- **a forwarding address** for its id (see below).

Carrying the data across is not cosmetic. Edges alone would drop the loser's `wikidata_id` — which is what marks a company "notable" in search ranking and the key a later Wikidata scrape resolves on — along with its description, revenue and headcount.

---

## Merged ids

A merge deletes one of the two nodes, but its id is **not** discarded: it lives on in shared links, client caches and — the case that actually bites — **federation peers' copies of our data**, where a peer that pulled the losing id and pulls again would find nothing and recreate the duplicate we just merged.

Every merge therefore writes a `MergedId` forwarding row (`old_id` → `new_id`). A by-id read that misses falls back to it and returns the survivor, whose own `id` is the canonical one, so a caller can see it and update what it stored. A **live** id is never redirected, and an id that was never merged still 404s.

It's a separate indexed vertex rather than an `also_known_ids` list on the survivor because the lookup has to be an indexed equality: `CONTAINS` over a list can't use an index and would scan the whole `Entity` type (millions of rows) on every miss. Chains are collapsed when written — merging A→B and later B→C rewrites A→C — so resolution is one hop and can never land on a node that was itself merged away.

---

## Duplicate persons

Different sources spell the same person differently (SEC's "Page Lawrence" vs
Wikidata's "Larry Page", nicknames, aliases), so scraping creates duplicate
`Person` nodes. `GET /persons/duplicates` scans for them (name/alias tokens,
birth date+place, surname+company); high-confidence groups are auto-merged after
each `run-all` scrape (`SCRAPER_AUTODEDUP_ENABLED`), the rest are resolved from
the web app's **Scraper tab → Review duplicate persons** panel (merge, keep
separate, or view the merge log).

Entities and ownership edges dedupe too: the same company under two GLEIF LEIs is
detected by name with a confidence tier (registered address / shared hard id), and
duplicate `OWNS` edges from multi-interest ownership records are collapsed — both via
`/scraper/duplicate-*` endpoints.

**Scraping a person.** The company scrape reads a company and finds its people; the person scrape goes the other way — the companies someone runs, founded or owns. Wikidata records those links only from the company side (P169 CEO, P112 founder, P488 chairperson, P3320 board member, P127 owner), so it is a reverse lookup, and the results need filtering: "founded by" also covers buildings, software, schools, a political party, and in Elon Musk's case a car and an aeroplane. A target counts as a company when its type is a recognised organisation **or** it carries a company signal — a legal form, an industry, an LEI or a stock listing. Nothing without one of those is written.

📄 **Deep dive:** [`docs/deduplication.md`](docs/deduplication.md) — person scan signals + confidence model + param-mediated merge, entity same-company detection with confidence tiers, and OWNS edge dedup.

---

## Scrape run log

Every scrape (`/scraper/run`, `/run-all`, `/sec-edgar/run`,
`/open-corporates/run`) records a `ScrapeRun` row: a `running` entry on start,
updated to `ok` (with node count) or `failed` (with the error) on finish. `GET
/scraper/runs` lists them newest-first, so the UI and other sessions can see
what's scraping now and which runs failed — across the panel *and* the bundled
`scrape_companies.sh` script.

The endpoint is **public**: what the platform ingests, against which company and
when, is the kind of thing an ownership-transparency project should be open
about, and the frontend shows the feed to every visitor. The `error` field is the
exception: it holds raw exception text, which can include internal URLs, database
errors, or a credential embedded in a failing request URL, so it is stripped for
anyone who isn't a `contributor` or `admin`. Everyone still sees that a run
failed — just not the stack's opinion about why.

The log is **bounded**: capped at 500 records, with the oldest pruned on every
write, so it can never grow the database unbounded. A `running` row older than 30
minutes is flagged `stale` (an interrupted run). Surfaced in the web app's
**Scraper tab → Recent activity** panel, which polls while a run is in progress.

---

## Usage measurement

Counting what the product is asked for, so the roadmap follows demand rather than
guesswork: which companies people search for, which searches find nothing (the
best possible list of registers to add next), which features get used, and how
the endpoints are behaving. Off by default — `ANALYTICS_ENABLED`.

The design constraint is that it must not become surveillance, and one detail
forced the whole shape: `SearchBar` calls `/search` every 300 ms while typing, so
counting requests would record `mi`, `mic`, `micr` — useless as demand data and
*more* intrusive than what we wanted, since partial typing exposes hesitation and
correction. So the **client reports once, when a search settles** (a result was
taken, nothing was found, or nothing was chosen), and the server keeps **totals,
never events**.

Three keyed counter types, in the database alongside `ImportState` and `GeoCache`:

| type | key | counts |
|---|---|---|
| `SearchDemand` | normalised query + ISO-2 country | searches, zero-result searches, searches where a result was taken |
| `UsageCounter` | an allow-listed event name (`export.csv`, `map.basis.hq`, `result.rank.3`) | occurrences |
| `EndpointStat` | `METHOD route_template status_class` + latency bucket | requests |

**No row can be tied to a person.** No user id, no account link, no session id, no
stored IP address (a salted hash lives in memory for rate limiting and is never
written), no device identifier, no user agent, and no per-event timestamp — only
first- and last-seen on an aggregate. There is no event log, so no individual's
activity can be reconstructed even by us. `EndpointStat` keys on the **route
template**, never the raw path, so `/entities/{id}` does not mint a row per
company.

`POST /v1/analytics/event` is public and anonymous, rate-limited like `/flags`,
and strictly allow-listed: an unknown event name is rejected, `query` is capped at
120 characters and `rank` at 0–49. An open-ended key column on a public endpoint
is unbounded row growth and a junk-injection vector. Reading it back —
`GET /v1/analytics/searches` and `/usage` — is admin-only and paged.

**The one honest caveat:** the `SearchDemand` key is free text, and somebody will
search for a person's name. That name is stored, unlinked to whoever searched for
it, readable only by an admin, and deleted once the query has been idle for twelve
months (`prune-analytics`, daily at 05:15 via `~/scripts/cron-prune-analytics.sh`).
The store is anonymous with respect to the *searcher*, not with respect to
everyone who might be named in a search box — which is exactly how the record of
processing and the published privacy notice put it. If the retention window here
changes, those change with it.

---

## Federation

Independent instances, run by different people, share ownership data as
**trusted peers** — each *publishes* its graph and *pulls* from peers it trusts.
A pull is **one-way and opt-in**: pulled nodes are reconciled on external ids and
run through the [duplicate scan](#duplicate-persons), and every imported fact is
attributed to a `Peer: <name>` Source, so you can trust or drop a peer without
touching your own data. Exports are **Ed25519-signed**, and a pull verifies the
peer's signature (a mismatch is refused).

Disabled by default. Enable with `FEDERATION_ENABLED=true`, generate a signing
key via `python manage.py gen-federation-key` (set it as `FEDERATION_SIGNING_KEY`),
then register peers and pull from the web app's **Scraper tab → Federation**
panel or the `/federation/*` API.

📄 **Deep dive:** [`docs/federation.md`](docs/federation.md) — the snapshot format, Ed25519 signing/verification, external-id reconciliation, the trust/threat model, why it's a native format rather than BODS, and setup commands.

---

## Import verification (planned)

Almost every node and edge comes from a scraper, and scrapers are sometimes
wrong. Phase A lets anyone (logged in or not) **⚑ report** a node or edge that
looks wrong and gives **moderators** a **queue of what's disputed** — without a
manual-entry UI.
Corrections live as a re-scrape-surviving overlay (like the dedup keep-separate
log), not as in-place edits that the next scrape would clobber.

📄 **Design (not yet implemented):** [`docs/verification.md`](docs/verification.md) — the `Flag` node and stable edge addressing, anonymous rate-limited reporting, the endpoints, the read-time "disputed" badge, GDPR intake, and the Phase-B non-goals (suppress/pin).

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ARCADEDB_URL` | required | ArcadeDB HTTP endpoint |
| `ARCADEDB_USERNAME` | required | Database username |
| `ARCADEDB_PASSWORD` | required | Database password |
| `ARCADEDB_DATABASE` | `owlgraph` | Database name |
| `SECRET_KEY` | insecure default | JWT signing key — **min 32 chars, always enforced; must also be overridden when `DEBUG=false`, or the app refuses to start** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access-token lifetime. Not revocable, so this is the exposure window for a stolen one |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | Absolute session lifetime. Rotation does not extend it |
| `REFRESH_COOKIE_NAME` | `owlgraph_refresh` | Name of the httpOnly session cookie |
| `REFRESH_COOKIE_DOMAIN` | `` (host-only) | Leave empty unless a host other than the API must receive the cookie |
| `REFRESH_COOKIE_SECURE` | `true` | HTTPS-only cookie. Set `false` only for a plain-http local backend |
| `CORS_ORIGINS` | `` (none) | Comma-separated list of allowed frontend origins |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | none | Provision this account as admin on startup (created if missing). When set, self-registration never grants admin — avoids the "first person to `/register` becomes admin" race on a fresh DB |
| `REQUIRE_EMAIL_VERIFICATION` | `true` | Block login until the account's email is verified |
| `EMAIL_BACKEND` | `` (auto) | `resend` \| `smtp` \| `console`; empty = auto (console unless `SMTP_HOST` set) |
| `RESEND_API_KEY` | — | Resend API key (for `EMAIL_BACKEND=resend`). Secret — env only |
| `SMTP_HOST` / `SMTP_PORT` | `` / `587` | SMTP server (e.g. `smtp.gmail.com`) — note Render blocks outbound SMTP |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | none | SMTP credentials — for Gmail, the account + an **App Password**. Secret — env only |
| `EMAIL_FROM` | = `SMTP_USERNAME` | `From` header on outgoing mail |
| `APP_BASE_URL` | `http://localhost:5173` | Frontend origin used to build verification / reset links in emails |
| `ANALYTICS_ENABLED` | `false` | Record aggregate usage counters — what is searched for, which features are used, endpoint health. No user, session, IP or per-event timestamp is stored; see *Usage measurement* below. Live on dev since 2026-08-04 |
| `SCRAPER_ENABLED` | `false` | Master scraper switch (required for any scrape) |
| `SCRAPER_WIKIDATA_ENABLED` | `true` | Wikidata source switch |
| `SCRAPER_SEC_EDGAR_ENABLED` | `false` | SEC EDGAR source switch |
| `SCRAPER_OPENCORPORATES_ENABLED` | `false` | OpenCorporates source switch |
| `SCRAPER_BODS_GLEIF_ENABLED` | `false` | GLEIF bulk-import switch (golden-copy CLI importers) |
| `SCRAPER_BODS_UK_PSC_ENABLED` | `false` | UK Companies House bulk-import switch (`ch-psc` / `ch-company-data`) |
| `SCRAPER_AUTODEDUP_ENABLED` | `true` | Auto-merge high-confidence duplicate persons after each `run-all` scrape |
| `FEDERATION_ENABLED` | `false` | Enable trusted-peer federation (publish/pull) |
| `FEDERATION_SIGNING_KEY` | — | Ed25519 private seed (base64) for signing exports; generate with `manage.py gen-federation-key`. Secret — env only |
| `OPENCORPORATES_API_KEY` | — | OpenCorporates API token (optional) |
| `GEOCODING_ENABLED` | `false` | Geocode addresses to coordinates via Nominatim |
| `GEOCODING_CONTACT` | — | Contact email added to the Nominatim User-Agent (required by their usage policy) |
| `GEOCODING_USER_AGENT` | `owlgraph-ownership-platform` | Base User-Agent for Nominatim requests |
| `NOMINATIM_URL` | public endpoint | Nominatim search URL (override to self-host) |
| `GEOCODING_MIN_INTERVAL` | `1.0` | Minimum seconds between geocoding requests |
| `DEBUG` | `false` | FastAPI debug mode |

---

## Deployment

Deployed on Render as a web service. Any push to **`develop`** triggers an automatic redeploy — Render tracks the integration branch, so what runs there is the dev environment (pointed at the dev database). `main` is the verified branch and currently deploys nowhere; production is planned on a separate, non-Render host. Required environment variables must be set in the Render dashboard: `ARCADEDB_URL`, `ARCADEDB_USERNAME`, `ARCADEDB_PASSWORD`, `SECRET_KEY`, `CORS_ORIGINS`. Set `ADMIN_EMAIL` + `ADMIN_PASSWORD` too so the admin is provisioned automatically on boot (see [Authentication](#authentication)).

**Domains.** The dev environment is reached at `dev.owlgraph.org` (frontend) and `api-dev.owlgraph.org` (API), both CNAMEd to the Render services. Keeping the two on one registrable domain is deliberate: they are **same-site**, which is what allows an `httpOnly; SameSite=Lax` auth cookie to pass between them. The `*.onrender.com` pair cannot do that — `onrender.com` is on the Public Suffix List, so each subdomain is a separate site and a cookie between them would be third-party, blocked by Safari and Chrome. Any future production pair should keep the same shape (`owlgraph.org` / `api.owlgraph.org`).

> **Env var changes apply on a *deploy*, not a restart.** Editing env vars in the dashboard triggers a deploy that applies them. But an env var set via the Render **API** does not auto-deploy, and a plain **"Restart Service"** restarts with the *already-deployed* config — so a value changed via the API only takes effect after a real deploy (**Manual Deploy → "Clear build cache & deploy"**, or a new push to `develop`).

### Schema & indexes

Lookup indexes (on `id`, `name`, `name_normalized`, `wikidata_id`, `sec_cik`, and a unique `User.email`) are created automatically on startup — the app runs an idempotent, best-effort bootstrap that is a no-op once they exist. To (re)create them explicitly, e.g. against a fresh database:

```bash
python3 manage.py init-schema
```

### CLI (`manage.py`)

| Command | Description |
|---|---|
| `init-schema` | Create vertex/edge types and lookup indexes (idempotent) |
| `seed` | Seed the built-in company list |
| `wipe-source` | Delete **one source's** data — its edges + the nodes only it created (nodes another source still references are kept); keeps user accounts, schema, and other sources. Finishes with `REBUILD INDEX *` to clear the stale index entries the batched deletes leave behind (else a later re-import can 500). There is **no** whole-DB wipe — drop the database for a fresh start. Requires `--source <name>` plus `ALLOW_DESTRUCTIVE_WIPE=true` **and** `--confirm-database <name>` matching the connected DB; add `--db-url http://localhost:2480` so the reindex isn't cut off by a proxy timeout, e.g. `ALLOW_DESTRUCTIVE_WIPE=true python manage.py wipe-source --source "UK PSC" --confirm-database owlgraph --db-url http://localhost:2480` |
| `geocode` | Backfill coordinates via Nominatim for **both** of a company's places: its headquarters (`hq_address` → `hq_lat/hq_lng`) and its registered office (`address` → `reg_lat/reg_lng`). They differ exactly where it is interesting — BARCLAYS CAPITAL (CAYMAN) is registered at its agent's door on Grand Cayman and run from London — and the map's Registered/Headquarters switch draws one or the other. `--target hq\|registered` runs a single pass; each is resumable on its own missing coordinate. |
| `normalize-countries` | Convert country values to canonical ISO-2 codes |
| `backfill-sec-headquarters` | Fill SEC filers' `hq_address`/`hq_city`/`hq_country` from EDGAR's **business address** — where a company is *run*. Deliberately separate from the country repair: a business address is poor evidence of a **domicile** (a foreign filer often files through a US office, which is why `sec_country` refuses to read one from it) and good evidence of a **headquarters**. The scraper had been fetching that address to read a single state code out of it and discarding the rest, leaving 40 of 43 SEC companies with no headquarters at all. |
| `gen-federation-key` | Generate an Ed25519 signing keypair for [federation](#federation) |
| `gleif-lei-cdf` / `gleif-rr` / `gleif-succession` | Import GLEIF golden-copy files (entities / direct+ultimate parents / mergers) — see *GLEIF sourcing* in [`docs/data-model.md`](docs/data-model.md) |
| `gleif-repex` | Import GLEIF **reporting exceptions** (`repex` = GLEIF's abbreviation). Every LEI holder must name its parent *or file a reason why not*, so "no parent recorded" and "a parent exists and was withheld" are different facts — `NATURAL_PERSONS`, `NON_PUBLIC`, `NO_LEI`, … See *Why a company reports no parent* in [`docs/data-model.md`](docs/data-model.md). Only ever updates entities already held; never creates one |
| `gleif-update` | Apply a GLEIF **delta** on top of the full load — the retirement-aware daily refresh (new/changed entities, merges, closed relationships). `--interval auto` (default) is gap-aware: it picks the smallest delta window covering any missed runs since the last one (fails loudly past ~30 days → full-reload). Override with `--interval LastDay\|LastWeek\|LastMonth` or pass `--lei-file`/`--rr-file`/`--repex-file`. Idempotent; runs against the live-indexed DB (no `--bulk-load`). Daily via `~/scripts/cron-gleif-update.sh` |
| `ch-psc` | Import a Companies House PSC snapshot (current UK beneficial ownership). Add `--bulk-load` on a full import to drop secondary indexes for the load and rebuild after (much faster; collapse duplicate edges afterwards with `POST /scraper/deduplicate-edges`). Company names come from a companion `ch-company-data` import |
| `ch-company-data` | Enrich UK companies with names/addresses/former-names from a Companies House BasicCompanyData snapshot (the full register). Enrichment only — updates companies already in the graph (from `ch-psc`), never creates isolated nodes |
| `backfill-search` | Populate the FULL_TEXT `search_text` column powering `/search`. Run once after a bulk import (the importers set it inline, but this covers pre-existing rows). |
| `rebuild-search` | REBUILD the FULL_TEXT `search_text` indexes so `/search` (`CONTAINSTEXT`) finds every row — needed after a non-bulk / `--only` import (the FULL_TEXT index isn't maintained incrementally). `--hard` first DROPs + re-CREATEs the indexes: use it to recover a **stuck/corrupted** index that a plain REBUILD reports "ok" on but never repopulates (e.g. after a bulk-load's REBUILD was cut off mid-flight by the nginx 60s proxy timeout). Run `--hard` against `--db-url http://localhost:2480` so it isn't cut off by that same proxy again. |
| `verify-users` | Mark existing accounts email-verified (login now requires it). Run once after enabling verification so pre-existing users aren't locked out; `--email <addr>` targets one account. |
| `set-password` | Set one account's password directly: `python manage.py set-password someone@example.com`. Prompts twice with hidden input (`--password` exists for scripting but puts the secret in shell history and `ps`). Applies the same policy as the API. The operator escape hatch for when neither in-app route works — the reset flow needs email, `/auth/change-password` needs the current password, and `ADMIN_PASSWORD` only ever seeds a *missing* account. |
| `prune-analytics` | Delete usage counters untouched inside the retention window (`--days`, default 365; `--dry-run` first). **Scheduled** — `~/scripts/cron-prune-analytics.sh` runs it daily at 05:15, because the privacy notice tells people those rows are deleted after twelve months and that is only true if something deletes them. Shortening `--days` means changing the published notice too. |
| `backup-database` | Take a consistent **online** backup — ArcadeDB's own `BACKUP DATABASE`, not a disk snapshot (pages and the WAL can be captured out of step). The **server** picks the path, so this only reports the filename; `~/scripts/backup-database.sh` wraps it with verification, rotation and an offsite copy. See [`docs/operations.md`](docs/operations.md) for the restore procedure. |
| `sec-holdings` | Ingest the >5% stakes one SEC filer discloses in others: `python manage.py sec-holdings 0002100119 [--limit N] [--succeeds OLD_CIK]`. The mirror of a normal scrape, which reads filings *about* a company — an asset manager has none of those. Keyed on CIK because the live filer often isn't the one you'd search by name. `--succeeds` records a handover between filer entities as a `SUCCEEDED_BY` edge. |

---

## Licence

### Source Code
The Owlgraph source code is licensed under the
[MIT Licence](LICENSE).

### Database
The Owlgraph ownership database is licensed under the
[Open Database Licence (ODbL) v1.0](DATA_LICENSE.md).

You are free to copy, distribute and use the data,
as long as you attribute Owlgraph and share any adapted
databases under ODbL. See [DATA_LICENSE.md](DATA_LICENSE.md)
for full details.

This dual licence model follows the same approach as
[OpenStreetMap](https://www.openstreetmap.org/copyright).

---

## Built With

This project was designed and built with the assistance of
[Claude](https://claude.ai) by Anthropic, using
[Claude Code](https://claude.ai/code) CLI for development.
