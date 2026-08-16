# How to write a Owlgraph scraper plugin

This document covers the full pattern for adding a new data source to the
scraper pipeline, including the specific pitfalls encountered while building
the SEC EDGAR and OpenCorporates plugins.

---

## Data Licence

All data collected by Owlgraph scrapers and stored in the
Owlgraph database is published under ODbL v1.0.

When writing a new scraper plugin, ensure:
1. The source data licence is compatible with ODbL
   - CC0: ✅ compatible
   - Public domain: ✅ compatible
   - Open Government Licence: ✅ compatible
   - Creative Commons Attribution (CC-BY): ✅ compatible
   - CC-BY-SA: ✅ compatible (share-alike aligns with ODbL)
   - Proprietary / All rights reserved: ❌ not compatible
   - OpenCorporates free tier: ❌ requires separate agreement

2. Add the source licence to the NOTICE file
3. Set an appropriate credibility_score for the source
4. Document the source licence in the scraper file header

---

## Architecture overview

Each scraper plugin consists of three layers:

```
external API module          runner.py                  router.py
(e.g. sec_edgar.py)    →    (Neo4j writes)        →    (HTTP endpoints)
scrape_company()            run_scrape_<source>()       POST /scraper/<source>/run
```

The external API module knows nothing about Neo4j. The runner knows nothing
about HTTP. The router knows nothing about either scraper or database details.

---

## Step 1 — Create the API module

**File:** `backend/app/scraper/<source_name>.py`

Model it on `sec_edgar.py` or `open_corporates.py`. The public entry point
must be:

```python
def scrape_company(company_name: str) -> dict | None:
    ...
```

Return `None` if the company cannot be found. Never raise on not-found —
only raise on genuine errors (network failure, auth error).

### Rate limiting

Always sleep between requests:

```python
REQUEST_DELAY = 0.2   # seconds
time.sleep(REQUEST_DELAY)
```

SEC EDGAR allows 10 req/s → 0.12s delay.
OpenCorporates allows 5 req/s → 0.2s delay.
When in doubt, 0.2s is safe for any public API.

### Required User-Agent

Some APIs (SEC EDGAR, OpenCorporates) block requests without a User-Agent
that identifies your application:

```python
HEADERS = {"User-Agent": "Owlgraph/1.0 contact@owlgraph.org"}
```

### Error handling in HTTP helpers

- `401 Unauthorized` → raise `PermissionError` with an actionable message.
  Do **not** catch it silently: it turns into a misleading `no_results`.
- `404 Not Found` → return `None` or `[]` from the calling function.
- `5xx / network error` → catch `httpx.HTTPError`, log it, return `None` / `[]`.
- HTTP 200 with an error payload (some APIs do this) → check `"error" in data`
  and raise `RuntimeError`.

```python
def _get(path: str) -> dict:
    r = httpx.get(BASE_URL + path, headers=HEADERS, timeout=20)
    if r.status_code == 401:
        raise PermissionError("API requires a token. Set MY_API_KEY env var.")
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    payload = r.json()
    if "error" in payload:
        raise RuntimeError(f"API error: {payload['error']}")
    return payload
```

### Optional API keys

Read keys from settings, never hardcode them. Default to empty string so the
free-tier path works without any configuration:

```python
def _api_key() -> str:
    from app.config import settings
    return settings.MY_SOURCE_API_KEY or ""
```

### Company name lookup ambiguity

Full-text search APIs return too many results for common words.

- SEC EDGAR: use `company_tickers.json` as primary lookup. It maps every
  listed company to its exact CIK unambiguously. Only fall back to full-text
  search for unlisted/private companies. Searching "Apple" in full-text will
  return Apple Hospitality REIT before Apple Inc.

- OpenCorporates: the `/companies/search` endpoint returns the highest-scoring
  result first, which is usually correct, but test with common names.

### EDGAR-specific: CIK vs filing agent CIK

Form 3/4 filings are sometimes filed through a filing agent (e.g. Toppan
Merrill, CIK 0001104659). The accession number starts with the agent's CIK,
but EDGAR indexes the filing under the **issuer's CIK** in Archives.

Always use the company's own CIK to build Archives URLs:

```python
# WRONG: uses filer/agent CIK from the accession number
filer_cik = accession.replace("-", "")[:10]
url = f"{ARCHIVES_URL}/{filer_cik}/{accession}/{doc}"

# RIGHT: uses the issuer's CIK (the company you're scraping)
issuer_cik = str(int(company_cik))   # strip leading zeros
url = f"{ARCHIVES_URL}/{issuer_cik}/{accession}/{doc}"
```

### EDGAR-specific: XSLT prefix in primaryDocument

The `primaryDocument` field in the submissions API sometimes contains an XSLT
stylesheet prefix: `xslF345X06/form4.xml`. Fetching this URL returns an
HTML-rendered view, not raw XML.

Always strip any leading directory component:

```python
primary_doc = raw.split("/")[-1] if "/" in raw else raw
```

---

## Step 2 — Add the source toggle

**File:** `backend/app/scraper/sources.py`

Add one entry to `KNOWN_SOURCES`:

```python
KNOWN_SOURCES = {
    ...
    "my_source": "My Source — short description of what it provides",
}
```

The key is the toggle name used by `get_source_enabled("my_source")`. It is
also the URL path component: `PATCH /scraper/sources/my_source/toggle`.

---

## Step 3 — Add config flags

**File:** `backend/app/config.py`

```python
SCRAPER_MYSOURCE_ENABLED: bool = False
MY_SOURCE_API_KEY: str = ""
```

Default to `False` — new scrapers are opt-in.

**File:** `backend/.env.example`

```env
SCRAPER_MYSOURCE_ENABLED=false
MY_SOURCE_API_KEY=
```

---

## Step 4 — Add runner functions

**File:** `backend/app/scraper/runner.py`

Add constants at the top:

```python
MYSOURCE_SOURCE_NAME  = "My Source"
MYSOURCE_SOURCE_URL   = "https://mysource.com"
MYSOURCE_CREDIBILITY  = 85   # see credibility table below
```

### Credibility scores

| Source         | Score | Rationale                         |
|----------------|-------|-----------------------------------|
| SEC EDGAR      | 98    | Legally mandated, audited filings |
| OpenCorporates | 85    | Official registers, aggregated    |
| Wikidata       | 80    | Community-maintained              |

Higher score wins the `name` field when the same entity is seen from multiple
sources. Assign your source a score based on how authoritative it is.

### Required functions

**`_ensure_<source>_source() -> str`**

```python
def _ensure_mysource_source() -> str:
    with db.get_session() as session:
        rec = session.run(
            "MATCH (s:Source {name: $name}) RETURN s.id AS id",
            name=MYSOURCE_SOURCE_NAME,
        ).single()
        if rec:
            return rec["id"]
        source_id = str(uuid.uuid4())
        session.run(
            "CREATE (s:Source {id: $id, name: $name, url: $url, "
            "credibility_score: $score, type: 'register'})",
            id=source_id, name=MYSOURCE_SOURCE_NAME,
            url=MYSOURCE_SOURCE_URL, score=MYSOURCE_CREDIBILITY,
        )
        return source_id
```

**`run_scrape_<source>(company_name: str) -> dict`**

```python
def run_scrape_mysource(company_name: str, country: str | None = None) -> dict:
    if not settings.SCRAPER_ENABLED:
        raise PermissionError("Scraper is disabled. Set SCRAPER_ENABLED=true.")
    if not settings.SCRAPER_MYSOURCE_ENABLED:
        raise PermissionError("My Source scraper is disabled. Set SCRAPER_MYSOURCE_ENABLED=true.")
    if not get_source_enabled("my_source"):
        raise PermissionError("My Source is disabled. Enable it in the Scraper panel.")

    from app.scraper.my_source import scrape_company   # import inside function
    data = scrape_company(company_name)
    if not data:
        return {"status": "no_results", "company": company_name, "total": 0, "scraped": []}

    source_id = _ensure_mysource_source()
    scraped: list[dict] = []

    target_id = _upsert_entity_by_name(name=data["name"], entity_type="company")
    scraped.append({"type": "entity", "name": data["name"], "role": "target"})

    for officer in data.get("officers", []):
        name = officer["name"].strip()
        if not name:
            continue
        if is_person_name(name):
            person_id = _upsert_person_by_name(name)
            # create HAS_ROLE edge...
            scraped.append({"type": "person", "name": name, "role": officer["role"]})
        else:
            _upsert_entity_by_name(name=name, entity_type="company")
            scraped.append({"type": "entity", "name": name, "role": officer["role"]})

    return {"status": "ok", "company": company_name, "total": len(scraped), "scraped": scraped}
```

**Import inside the function.** This avoids circular imports and keeps
cold-start fast (the module is only loaded when the scraper actually runs).

Then **register** it so `run_scrape_all` picks it up — no edits to the orchestrator,
which iterates the registry (`app/scraper/scraper_registry.py`):

```python
from app.scraper.scraper_registry import ScraperSpec, register

register(ScraperSpec(
    "my_source",
    # (query, depth, country) — ignore depth if your source doesn't traverse.
    lambda q, d, c=None: run_scrape_mysource(q, c),
    lambda: settings.SCRAPER_MYSOURCE_ENABLED and get_source_enabled("my_source"),
))
```

### The country argument

`country` is an ISO-2 the user picked in the search box, or `None`. Asked for
"Alphabet", every source left to itself answers with Alphabet Inc of Mountain
View, because it is the most famous company by that name. The country is the
only thing standing between a German query and an American import.

**Ask the source, if it can be asked.** A filter applied to the answer cannot
find what the question never reached: Alphabet Fuhrparkmanagement, the German
company called Alphabet, is nowhere near the global top hits and no amount of
post-filtering will ever surface it. Wikidata's search index takes a statement
filter, and OpenCorporates takes a `jurisdiction_code`, so both put the country
*in the query*:

```python
data = search_their_api(name, country=country)     # the source does the filtering
```

**Check the match only when you cannot ask.** SEC EDGAR is the example: its
search-side `State=` filter matches the *business address*, which for a foreign
filer is usually its US filing office — Deutsche Bank AG lists New York — so
filtering the search by it would hide German companies from a German search.
There, EDGAR's single match is judged on what it states about incorporation:

```python
from app.scraper.country_match import matches_requested, country_mismatch

found = filer_country(match)               # whatever your source calls it
if not matches_requested(found, country):
    return country_mismatch(company_name, found, country)
```

Do the check **before the first write**, so a rejection leaves nothing behind.

**A match that states no country is rejected.** Asked for a company in Germany,
"we do not know where this is" is not an answer — and it is what the source-side
filters do anyway (an item with no `P17` is not in the index being searched), so
the checked sources have to agree or "found in Germany" means two things. The
cost is real: Deutsche Bank leaves `stateOfIncorporation` empty, so a German
search will not find it through EDGAR.

`register()` rejects a `run` that cannot take all three arguments. That check
exists because the dispatchers catch every exception per source — a two-argument
`run` would otherwise raise, be logged, and leave the scrape reporting success
having run nothing.

`run_scrape_all` applies the enabled/disabled/error wrapping uniformly to every
registered scraper, so a registered spec is all the wiring `run-all` needs.

---

## Step 5 — Router endpoints (nothing to write)

Once your scraper is registered (Step 4), it's **automatically** reachable through
the generic registry-driven endpoints — no per-scraper route code:

- `POST /scraper/source/{name}/run?company=…&depth=…&country=…` — run it (`country` is the ISO-2 filter above, so a source can be exercised per-country straight from the API)
- `GET  /scraper/source/{name}/status` — its enabled state
- `GET  /scraper/registry` — lists every registered scraper + enabled state

e.g. `POST /scraper/source/my_source/run?company=Acme`. These dispatch via the
registry and apply the master-switch / enabled / `PermissionError` / error handling
uniformly, so there's nothing to add in `router.py`.

(The built-ins additionally keep their older named endpoints — `/scraper/run`,
`/scraper/sec-edgar/run`, `/scraper/open-corporates/run` — for the current frontend;
new scrapers just use the generic path above.)

---

## Step 6 — Deduplication

The same company appears under different names across sources:
- Wikidata: `"BlackRock"`
- SEC EDGAR: `"BlackRock, Inc."`
- OpenCorporates: `"BLACKROCK INC."`

Owlgraph resolves this with two properties on every Entity node:

**`name_normalized`** — produced by `normalize_entity_name()` from `mapper.py`.
Strips legal suffixes (Inc, Corp, Ltd, …), commas, periods, and lowercases.
All three names above normalize to `"blackrock"`.

**`name_credibility`** — the score of the source that last set the name.
When upserting, only update `name` if the incoming credibility ≥ stored:

```python
SET e.name = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred
                  THEN $name ELSE e.name END,
    e.name_credibility = CASE WHEN COALESCE(e.name_credibility, 0) <= $cred
                              THEN $cred ELSE e.name_credibility END
```

Pass `name=name` in the params dict — failing to include it causes
`Neo.ClientError.Statement.ParameterMissing` (this happened with Wikidata
when the credibility CASE was added but `name` was not added to the params).

Use `_upsert_entity_by_name()` for name-only sources (no Wikidata QID or
fixed identifier). It already implements the full match-by-CIK-or-name-or-normalized
logic and the credibility-based name update.

---

## Step 7 — Person vs Entity classification

Officers fetched from registers can be natural persons or corporate nominees.
Use `is_person_name()` from `mapper.py` to decide:

```python
if is_person_name(name):
    person_id = _upsert_person_by_name(name)
    # → Person node + HAS_ROLE edge
else:
    entity_id = _upsert_entity_by_name(name=name, entity_type="company")
    # → Entity node
```

`is_person_name` returns `True` for 2–4 capitalised words with no digits and
no legal suffixes. It catches most corporate nominees (`"Computershare Trust Co."`)
but is a heuristic — it will occasionally misclassify unusual names.

---

## Importing bulk ownership datasets

The bulk ownership datasets (GLEIF golden copy + Companies House snapshots) are
large batch imports, not real-time scrapers, and are run **manually from the CLI**
in a tmux session on the server — not over HTTP and not on a schedule. (They
replaced the OpenOwnership BODS exports, which were frozen at 2025-03; the BODS
importer and its `/scraper/bods/*/run` endpoints have been removed.)

### GLEIF (worldwide corporate ownership)

Imported from the current GLEIF golden copy (download the LEI-CDF and RR-CDF
files first):

```bash
python manage.py gleif-lei-cdf --file /data/lei-cdf/gleif-lei2.json.zip --bulk-load  # entities
python manage.py gleif-rr      --file /data/rr-cdf/gleif-rr.json.zip                 # relationships
python manage.py gleif-succession --file /data/lei-cdf/gleif-lei2.json.zip           # mergers
```

The entity import stores, besides name/country, the fields gleif.org shows in its
detail view: **legal form** (ISO 20275 ELF), **Registered At** (registration authority
+ number), and a display **address** (the registered `LegalAddress`, all lines). It also
sets **`hq_city`/`hq_country` from the `HeadquartersAddress`** — the real operating
location, shown at the top of the node — which for many entities differs from both the
jurisdiction and the (often registered-agent) legal address (e.g. MercadoLibre, Inc. is
US-DE domiciled with a Delaware C/O legal address but HQ in Montevideo, UY). The LEI-CDF file only carries *codes*
for legal form (e.g. `H0PO`) and authority (e.g. `RA000585`); these are resolved to
names ("Private Limited Company", "Companies Register") via GLEIF's ELF + RA reference
code lists, bundled as `app/scraper/data/gleif_{elf,ra}.json` (see
`app/scraper/gleif_reference.py`). Refresh those two JSONs from
<https://www.gleif.org/en/about-lei/code-lists> when GLEIF publishes a new list
version. These fields populate on (re)import — existing rows only gain them after a
re-run of `gleif-lei-cdf`.

### UK (Companies House)

Two companion imports — beneficial ownership (PSC) then company names (register):

```bash
python manage.py ch-psc          --file /data/companies-house-psc/psc-snapshot.zip --bulk-load
python manage.py ch-company-data --file /data/companies-house-basic/basic-company-data.zip --bulk-load
```

`ch-psc` creates controlled companies keyed on their number (`gb-coh:{number}`);
`ch-company-data` fills in their names/addresses/former-names by enriching those
existing nodes (it never creates isolated companies for the ~5.6M-row register).

### Curated test subset (`--only` / `--only-file`)

A full load is slow to iterate on. To load just a **handful of test companies straight
from the same golden-copy files** — for checking the pipeline and the node UI, and as
repeatable test cases for the production import — pass an allow-list:

```bash
# GLEIF: a big/medium/small spread (bundled fixture)
python manage.py gleif-lei-cdf --file …/gleif-lei2.json.zip \
    --only-file app/scraper/data/test_leis.txt
# UK: PSC ownership then names, same curated companies
python manage.py ch-psc          --file …/psc-snapshot.zip       --only-file app/scraper/data/test_companies.txt
python manage.py ch-company-data --file …/basic-company-data.zip --only-file app/scraper/data/test_companies.txt
```

`--only LEI1,LEI2` takes an inline comma list; `--only-file` reads ids one per line
(`#` comments allowed). Reading **stops early** once every listed id is found, and the
PSC scan rejects non-matching lines before parsing, so a subset loads in seconds–a
minute rather than the full pass. **Don't** combine with `--bulk-load` — for a few
records you want the indexes to stay live (and to skip the whole-DB rebuild). The
wrapper scripts `lei-cdf-import-test.sh` / `psc-import-test.sh` do exactly this; the
bundled `data/test_{leis,companies}.txt` are the curated cases (edit to taste). The
full loads remain `lei-cdf-import.sh` etc.

`gleif-rr --only-file <seeds> --emit-leis <path>` imports the **corporate family** of
the seed LEIs — every consolidation edge among the seeds, their ancestors and all their
descendants (the RR file is small, so it loads the edges and walks the tree in memory)
— and writes the family's LEIs to `<path>`. Feed that to a second `gleif-lei-cdf
--only-file <path>` to name the subsidiaries/parents (RR edges alone leave them as
nameless LEI stubs). `lei-cdf-import-test.sh` chains exactly this: seeds → family edges
→ name the family → rebuild search.

**After a non-bulk / `--only` import, run `python manage.py rebuild-search`.** The
FULL_TEXT index isn't maintained incrementally, so freshly imported companies have
their `search_text` set but aren't findable via `/search` (`CONTAINSTEXT`) until the
index is rebuilt — instant on a small test-only DB, minutes on the full ~4M graph
(the wrapper scripts already do this).

### `--bulk-load`

For a **full load** pass `--bulk-load`, which drops the secondary indexes on
`Entity`/`Person` for the duration and rebuilds them at the end. On 10M+ row types
those indexes dominate per-write cost, so this is substantially faster; each flush
also retries with backoff so a transient proxy timeout doesn't kill a multi-hour
import. `id` indexes are kept (the load needs them). Because `CREATE EDGE` isn't
idempotent, collapse any duplicate ownership edges afterwards with
`POST /scraper/deduplicate-edges`.

### Speed behind a proxy (`--db-url`, `--batch-size`)

The **dominant cost of a slow import is a proxy read timeout**, not the DB. dev-db
sits behind an nginx with a **60s** timeout; when a flush of `--batch-size` records
crosses 60s, nginx returns a 504, the importer waits the full 60s, then backs off
and retries — so every slow flush burns a minute-plus. Measured on the CH PSC
snapshot (~35M nodes) this turned the run into ~17h. Two knobs (`ch-psc` /
`ch-company-data`):

- **`--db-url http://<arcadedb-host>:2480`** — point the import **straight at
  ArcadeDB**, bypassing the proxy and its timeout entirely. Biggest win when the
  DB port is reachable from the import host; also drops per-round-trip latency.
- **`--batch-size N`** (default 400) — **lower it** (e.g. 100) when stuck behind
  the proxy so each flush finishes well under the timeout; **raise it** on a direct
  connection to cut round-trips.

Alternatively, raise the proxy's `proxy_read_timeout` server-side. Either removes
the 504-retry churn.

> **Temp space:** `gleif-succession` spills its id map to SQLite under the system
> temp dir. If `/tmp` is a small **tmpfs** (RAM), a full run can fill it and SQLite
> fails with *"database or disk is full"*. Set `SCRAPER_TMP_DIR` to a path on a real
> disk with tens of GB free: `SCRAPER_TMP_DIR=/data/tmp python manage.py gleif-succession …`.

`/search` relies on a FULL_TEXT index (`CONTAINSTEXT`, instant) instead of scanning
every row (`toLower(name) CONTAINS` on millions of entities takes ~12s). The importers
set `search_text` inline, and a **`--bulk-load` run drops the FULL_TEXT indexes for the
load and `REBUILD`s them afterwards automatically** — maintaining a Lucene index
per-insert across millions of rows is slow and, if a load is interrupted, leaves it
incomplete (`CONTAINSTEXT` then silently returns nothing). So no manual reindex is
needed after a bulk load.

Only needed for rows loaded by other means, or before `search_text` existed:

```bash
python manage.py init-schema        # ensures the FULL_TEXT index exists
python manage.py backfill-search    # fills search_text for existing rows
```

> If `/search` ever comes back empty after a load, the FULL_TEXT index is stale —
> rebuild it directly (the brackets need backticks): ``REBUILD INDEX `Entity[search_text]` ``.

### Licence

Both datasets are published under CC0 1.0 Universal.
No attribution required but Owlgraph credits them in NOTICE.

### Credibility scores

- GLEIF:  92 (authoritative LEI data, corporate ownership)
- UK PSC: 97 (official UK legal register, beneficial ownership)

---

## Checklist before deploying a new plugin

The mechanical one is below. For what a source should *contribute* — which facts to take,
how its records find the companies already in the graph, and the traps this project has
already fallen into — see **[`new-source-checklist.md`](new-source-checklist.md)**.

- [ ] `scrape_company()` returns `None` (not an exception) when not found
- [ ] `401` and other auth errors raise `PermissionError` with an actionable message
- [ ] HTTP 200 error payloads are checked and raise `RuntimeError`
- [ ] `time.sleep(REQUEST_DELAY)` called after every request
- [ ] `User-Agent` header set in every request
- [ ] API key read from `settings`, never hardcoded
- [ ] `SCRAPER_<SOURCE>_ENABLED` defaults to `False` in `config.py`
- [ ] Source added to `KNOWN_SOURCES` in `sources.py`
- [ ] `run_scrape_<source>()` checks all three flags (master, source env, DB source toggle)
- [ ] `run_scrape_all()` includes the new source after existing scrapers
- [ ] `/scraper/status` response includes the new flag
- [ ] `.env.example` documents the new variables
- [ ] Unit tests cover: 401 handling, not-found path, happy path, Person vs Entity split
- [ ] Anything that writes has an integration test against a real ArcadeDB
