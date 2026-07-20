# How to write a Pamten scraper plugin

This document covers the full pattern for adding a new data source to the
scraper pipeline, including the specific pitfalls encountered while building
the SEC EDGAR and OpenCorporates plugins.

---

## Data Licence

All data collected by Pamten scrapers and stored in the
Pamten database is published under ODbL v1.0.

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
HEADERS = {"User-Agent": "Pamten/1.0 contact@pamten.com"}
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
def run_scrape_mysource(company_name: str) -> dict:
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

Then add it to `run_scrape_all()`:

```python
if settings.SCRAPER_MYSOURCE_ENABLED and get_source_enabled("my_source"):
    try:
        results["my_source"] = run_scrape_mysource(query)
    except PermissionError as exc:
        results["my_source"] = {"status": "disabled", "detail": str(exc)}
    except Exception as exc:
        log.error("My Source scrape failed for %r: %s", query, exc)
        results["my_source"] = {"status": "error", "detail": str(exc)}
else:
    results["my_source"] = {"status": "disabled"}
```

---

## Step 5 — Add router endpoints

**File:** `backend/app/scraper/router.py`

```python
from app.scraper.runner import ..., run_scrape_mysource

@router.get("/my-source/status")
def my_source_status():
    return {
        "enabled":         settings.SCRAPER_ENABLED and settings.SCRAPER_MYSOURCE_ENABLED,
        "master_switch":   settings.SCRAPER_ENABLED,
        "my_source_switch": settings.SCRAPER_MYSOURCE_ENABLED,
    }

@router.post("/my-source/run")
def my_source_run(
    company: str = Query(..., min_length=2),
    _: dict = Depends(require_admin),
):
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403, detail="Scraper is disabled.")
    if not settings.SCRAPER_MYSOURCE_ENABLED:
        raise HTTPException(status_code=403, detail="My Source scraper is disabled.")
    try:
        return run_scrape_mysource(company)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {e}")
```

Update `/scraper/status` to include the new flag:

```python
return {
    ...
    "my_source_enabled": settings.SCRAPER_MYSOURCE_ENABLED,
}
```

---

## Step 6 — Deduplication

The same company appears under different names across sources:
- Wikidata: `"BlackRock"`
- SEC EDGAR: `"BlackRock, Inc."`
- OpenCorporates: `"BLACKROCK INC."`

Pamten resolves this with two properties on every Entity node:

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

## Importing BODS Data

BODS (Beneficial Ownership Data Standard) datasets are large
bulk imports, not real-time scrapers. They should be run
manually, not on a schedule.

### Recommended approach

For initial data population, download files locally first
to avoid re-downloading on retries:

```bash
# Download GLEIF (1.1 GB)
wget https://oo-bodsdata.s3.amazonaws.com/data/gleif_version_0_4/json.zip \
     -O /data/gleif.zip

# Download UK PSC (3.3 GB)
wget https://oo-bodsdata.s3.amazonaws.com/data/uk_version_0_4/json.zip \
     -O /data/uk_psc.zip
```

Then import with local file path:

```bash
# Test with limit first
curl -X POST "/scraper/bods/gleif/run?limit=1000&local_file=/data/gleif.zip"

# Import specific jurisdiction only
curl -X POST "/scraper/bods/gleif/run?filter_jurisdiction=DE&local_file=/data/gleif.zip"

# Full import (takes hours for large files)
curl -X POST "/scraper/bods/uk-psc/run?local_file=/data/uk_psc.zip"
```

For a **full load** run it from the CLI with `--bulk-load`, which drops the
secondary indexes on `Entity`/`Person` for the duration and rebuilds them at the
end. On 10M+ row types those indexes dominate per-write cost, so this is
substantially faster; each flush also retries with backoff so a transient proxy
timeout doesn't kill a multi-hour import. `id` indexes are kept (the load needs
them). Because `CREATE EDGE` isn't idempotent, collapse any duplicate ownership
edges afterwards with `POST /scraper/deduplicate-edges`:

```bash
python manage.py bods-uk-psc --file /data/uk_psc.zip --bulk-load
```

After a full import, populate the full-text search column so `/search` uses its
FULL_TEXT index instead of scanning every row (`toLower(name) CONTAINS` on
millions of entities takes ~12s; `CONTAINSTEXT` on the index is instant). The
BODS importer sets `search_text` inline, so this is only needed for rows loaded
by other sources or before this field existed:

```bash
python manage.py init-schema        # ensures the FULL_TEXT index exists
python manage.py backfill-search    # fills search_text for existing rows
```

### Licence

Both datasets are published under CC0 1.0 Universal.
No attribution required but Pamten credits them in NOTICE.

### Credibility scores

- GLEIF:  92 (authoritative LEI data, corporate ownership)
- UK PSC: 97 (official UK legal register, beneficial ownership)

---

## Checklist before deploying a new plugin

- [ ] `scrape_company()` returns `None` (not an exception) when not found
- [ ] `401` and other auth errors raise `PermissionError` with an actionable message
- [ ] HTTP 200 error payloads are checked and raise `RuntimeError`
- [ ] `time.sleep(REQUEST_DELAY)` called after every request
- [ ] `User-Agent` header set in every request
- [ ] API key read from `settings`, never hardcoded
- [ ] `SCRAPER_<SOURCE>_ENABLED` defaults to `False` in `config.py`
- [ ] Source added to `KNOWN_SOURCES` in `sources.py`
- [ ] `run_scrape_<source>()` checks all three flags (master, source env, Neo4j toggle)
- [ ] `run_scrape_all()` includes the new source after existing scrapers
- [ ] `/scraper/status` response includes the new flag
- [ ] `.env.example` documents the new variables
- [ ] Unit tests cover: 401 handling, not-found path, happy path, Person vs Entity split
