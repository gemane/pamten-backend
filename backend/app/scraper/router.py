from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from app.config import settings
from app.scraper.runner import run_scrape, run_scrape_sec_edgar, run_scrape_all, run_scrape_open_corporates
from app.auth.dependencies import require_admin
from app.database import db
from app.db.arcadedb import run_query, run_command

router = APIRouter(prefix="/scraper", tags=["Scraper"])


class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Company or brand name to search on Wikidata")
    depth: int = Field(2, ge=0, le=3, description="How many subsidiary levels to follow (0–3)")


# ── Master status ─────────────────────────────────────────────────────────────

@router.get("/status")
def scraper_status():
    """Check whether the master scraper switch is enabled."""
    return {
        "enabled":                    settings.SCRAPER_ENABLED,
        "sec_edgar_enabled":          settings.SCRAPER_SEC_EDGAR_ENABLED,
        "open_corporates_enabled":    settings.SCRAPER_OPENCORPORATES_ENABLED,
    }


# ── Wikidata endpoints ────────────────────────────────────────────────────────

@router.post("/run")
def scraper_run(body: ScrapeRequest, _: dict = Depends(require_admin)):
    """
    Trigger a Wikidata scrape for a company name.
    Requires SCRAPER_ENABLED=true in the environment.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true in the environment to enable.",
        )
    try:
        result = run_scrape(body.query, body.depth)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {e}")


# ── SEC EDGAR endpoints ───────────────────────────────────────────────────────

@router.get("/sec-edgar/status")
def sec_edgar_status():
    """Check whether SEC EDGAR scraping is enabled (both master and per-source flags)."""
    return {
        "enabled": settings.SCRAPER_ENABLED and settings.SCRAPER_SEC_EDGAR_ENABLED,
        "master_switch":     settings.SCRAPER_ENABLED,
        "sec_edgar_switch":  settings.SCRAPER_SEC_EDGAR_ENABLED,
    }


@router.post("/sec-edgar/run")
def sec_edgar_run(
    company: str = Query(..., min_length=2, description="Company name to look up on SEC EDGAR"),
    _: dict = Depends(require_admin),
):
    """
    Scrape SEC EDGAR for ownership filings and executive data for one company.
    Requires SCRAPER_ENABLED=true AND SCRAPER_SEC_EDGAR_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    if not settings.SCRAPER_SEC_EDGAR_ENABLED:
        raise HTTPException(status_code=403,
            detail="SEC EDGAR scraper is disabled. Set SCRAPER_SEC_EDGAR_ENABLED=true.")
    try:
        result = run_scrape_sec_edgar(company)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SEC EDGAR scrape failed: {e}")


# ── Run-all endpoint ──────────────────────────────────────────────────────────

@router.post("/run-all")
def scraper_run_all(
    company: str = Query(..., min_length=2, description="Company name to scrape across all enabled sources"),
    depth:   int = Query(2, ge=0, le=3,    description="Wikidata subsidiary depth (0–3)"),
    _: dict = Depends(require_admin),
):
    """
    Run all enabled scrapers (Wikidata + SEC EDGAR + OpenCorporates) for a company name.
    Disabled scrapers are skipped and reported with status 'disabled'.
    Requires SCRAPER_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    try:
        result = run_scrape_all(company, depth)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Run-all failed: {e}")


# ── OpenCorporates endpoints ──────────────────────────────────────────────────

@router.get("/open-corporates/status")
def open_corporates_status():
    """Check whether OpenCorporates scraping is enabled (both master and per-source flags)."""
    return {
        "enabled":                    settings.SCRAPER_ENABLED and settings.SCRAPER_OPENCORPORATES_ENABLED,
        "master_switch":              settings.SCRAPER_ENABLED,
        "open_corporates_switch":     settings.SCRAPER_OPENCORPORATES_ENABLED,
    }


@router.post("/open-corporates/run")
def open_corporates_run(
    company: str = Query(..., min_length=2, description="Company name to look up on OpenCorporates"),
    _: dict = Depends(require_admin),
):
    """
    Scrape OpenCorporates for company registration details and officers.
    Requires SCRAPER_ENABLED=true AND SCRAPER_OPENCORPORATES_ENABLED=true.
    """
    if not settings.SCRAPER_ENABLED:
        raise HTTPException(status_code=403,
            detail="Scraper is disabled. Set SCRAPER_ENABLED=true.")
    if not settings.SCRAPER_OPENCORPORATES_ENABLED:
        raise HTTPException(status_code=403,
            detail="OpenCorporates scraper is disabled. Set SCRAPER_OPENCORPORATES_ENABLED=true.")
    try:
        result = run_scrape_open_corporates(company)
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenCorporates scrape failed: {e}")


# ── Purge endpoint ────────────────────────────────────────────────────────────

@router.delete("/company")
def purge_company(
    name: str = Query(..., min_length=2, description="Exact company name to delete"),
    _: dict = Depends(require_admin),
):
    """
    Delete a company entity and all its relationships from the graph, then
    remove any nodes that are left with no remaining relationships (orphans).
    Admin only. Useful for cleaning up test scrapes.
    """
    with db.get_session() as session:
        # Check it exists first
        rec = session.run(
            "MATCH (e:Entity {name: $name}) RETURN e.id AS id LIMIT 1",
            name=name,
        ).single()
        if not rec:
            raise HTTPException(status_code=404, detail=f"Company '{name}' not found")

        # Detach-delete the entity and all its relationships
        session.run(
            "MATCH (e:Entity {name: $name}) DETACH DELETE e",
            name=name,
        )

        # Remove orphaned Person and Entity nodes (no remaining relationships)
        orphan_result = session.run(
            """
            MATCH (n)
            WHERE (n:Person OR n:Entity) AND NOT (n)--()
            WITH n, n.name AS orphan_name
            DETACH DELETE n
            RETURN count(*) AS removed, collect(orphan_name) AS names
            """
        ).single()
        orphans_removed = orphan_result["removed"] if orphan_result else 0
        orphan_names    = orphan_result["names"]   if orphan_result else []

    return {
        "status":          "deleted",
        "company":         name,
        "orphans_removed": orphans_removed,
        "orphans":         orphan_names,
    }


# ── Deduplication endpoint ─────────────────────────────────────────────────────

@router.post("/deduplicate-edges")
def deduplicate_owns_edges(_: dict = Depends(require_admin)):
    """
    For every (owner → target) pair that has more than one active OWNS edge,
    keep the most informative edge (highest stake_percent, then most recent
    file_date) and delete the rest.  Admin only.
    """
    # Find all pairs with duplicates
    pairs = run_query(
        """
        MATCH (a)-[r:OWNS]->(b)
        WHERE r.until IS NULL
        WITH a.id AS owner_id, b.id AS target_id, count(r) AS cnt
        WHERE cnt > 1
        RETURN owner_id, target_id, cnt
        """
    )

    total_deleted = 0
    cleaned = []

    for pair in pairs:
        oid = pair["owner_id"]
        nid = pair["target_id"]

        # Fetch all active edges for this pair with their properties
        edges = run_query(
            """
            MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
            WHERE r.until IS NULL
            RETURN r.stake_percent   AS stake,
                   r.file_date       AS file_date,
                   r.source_id       AS source_id,
                   r.ownership_type  AS ownership_type,
                   r.since           AS since
            """,
            {"oid": oid, "nid": nid},
        )

        # Sort: prefer edge with stake_percent, then most recent file_date
        def _sort_key(e):
            return (
                0 if e.get("stake") is not None else 1,
                e.get("file_date") or "",
            )

        edges_sorted = sorted(edges, key=_sort_key, reverse=True)
        best = edges_sorted[0]

        # Delete all active edges between this pair
        run_command(
            """
            MATCH (a {id: $oid})-[r:OWNS]->(b {id: $nid})
            WHERE r.until IS NULL
            DELETE r
            """,
            {"oid": oid, "nid": nid},
        )

        # Recreate the single best edge
        run_command(
            """
            MATCH (a {id: $oid}), (b {id: $nid})
            CREATE (a)-[:OWNS {
                stake_percent:  $stake,
                file_date:      $file_date,
                source_id:      $source_id,
                ownership_type: $ownership_type,
                since:          $since,
                until:          null
            }]->(b)
            """,
            {
                "oid":            oid,
                "nid":            nid,
                "stake":          best.get("stake"),
                "file_date":      best.get("file_date"),
                "source_id":      best.get("source_id"),
                "ownership_type": best.get("ownership_type"),
                "since":          best.get("since"),
            },
        )

        deleted_count = len(edges_sorted) - 1
        total_deleted += deleted_count
        cleaned.append({"owner_id": oid, "target_id": nid, "duplicates_removed": deleted_count})

    return {"duplicates_removed": total_deleted, "pairs_cleaned": len(pairs), "detail": cleaned}


# ── Person deduplication endpoint ──────────────────────────────────────────────

def _migrate_person_edges(dead_id: str, keep_id: str) -> int:
    """Move all OWNS and HAS_ROLE edges from dead_id → keep_id, return count migrated."""
    migrated = 0

    # OWNS edges the dead node owns
    owns_out = run_query(
        """
        MATCH (p:Person {id: $pid})-[r:OWNS]->(t)
        RETURN t.id AS tid, r.stake_percent AS stake, r.file_date AS file_date,
               r.source_id AS source_id, r.ownership_type AS ownership_type,
               r.since AS since, r.until AS until
        """,
        {"pid": dead_id},
    )
    for e in owns_out:
        tid = e["tid"]
        # Skip if keep already has an active OWNS edge to the same target
        existing = run_query(
            "MATCH (p:Person {id: $pid})-[r:OWNS]->(t {id: $tid}) WHERE r.until IS NULL RETURN r LIMIT 1",
            {"pid": keep_id, "tid": tid},
        )
        if not existing:
            run_command(
                """
                MATCH (p:Person {id: $pid}), (t {id: $tid})
                CREATE (p)-[:OWNS {
                    stake_percent: $stake, file_date: $file_date,
                    source_id: $source_id, ownership_type: $otype,
                    since: $since, until: $until
                }]->(t)
                """,
                {"pid": keep_id, "tid": tid, "stake": e.get("stake"),
                 "file_date": e.get("file_date"), "source_id": e.get("source_id"),
                 "otype": e.get("ownership_type"), "since": e.get("since"),
                 "until": e.get("until")},
            )
            migrated += 1

    # HAS_ROLE edges
    roles = run_query(
        """
        MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(t)
        RETURN t.id AS tid, r.role AS role, r.since AS since, r.until AS until,
               r.source_id AS source_id
        """,
        {"pid": dead_id},
    )
    for e in roles:
        tid = e["tid"]
        existing = run_query(
            """
            MATCH (p:Person {id: $pid})-[r:HAS_ROLE]->(t {id: $tid})
            WHERE r.role = $role AND r.until IS NULL RETURN r LIMIT 1
            """,
            {"pid": keep_id, "tid": tid, "role": e.get("role")},
        )
        if not existing:
            run_command(
                """
                MATCH (p:Person {id: $pid}), (t {id: $tid})
                CREATE (p)-[:HAS_ROLE {
                    role: $role, since: $since, until: $until, source_id: $source_id
                }]->(t)
                """,
                {"pid": keep_id, "tid": tid, "role": e.get("role"),
                 "since": e.get("since"), "until": e.get("until"),
                 "source_id": e.get("source_id")},
            )
            migrated += 1

    return migrated


@router.post("/deduplicate-persons")
def deduplicate_person_nodes(_: dict = Depends(require_admin)):
    """
    Merge Person node pairs whose 2-word names are each other's reversal
    (e.g. 'Brin Sergey' ↔ 'Sergey Brin').  Keeps the richer node
    (prefer wikidata_id, then more edges, then alphabetically first name),
    migrates all edges from the dead node, then deletes it.  Admin only.
    """
    # Fetch all Person nodes with a 2-word full_name
    persons = run_query(
        "MATCH (p:Person) RETURN p.id AS id, p.full_name AS name, p.wikidata_id AS wid"
    )

    # Build a lookup: normalised name → node
    by_name: dict[str, dict] = {}
    for p in persons:
        name = (p.get("name") or "").strip()
        if name:
            by_name[name.lower()] = p

    merged: list[dict] = []
    visited: set[str] = set()

    for p in persons:
        name = (p.get("name") or "").strip()
        parts = name.split()
        if len(parts) != 2:
            continue
        pid = p["id"]
        if pid in visited:
            continue

        reversed_name = f"{parts[1]} {parts[0]}"
        other = by_name.get(reversed_name.lower())
        if not other or other["id"] == pid or other["id"] in visited:
            continue

        # Decide which to keep: prefer wikidata_id, then pick the one with
        # more natural "First Last" order (first word title-cased, second too)
        p_has_wiki   = bool(p.get("wid"))
        oth_has_wiki = bool(other.get("wid"))

        if p_has_wiki and not oth_has_wiki:
            keep, dead = p, other
        elif oth_has_wiki and not p_has_wiki:
            keep, dead = other, p
        else:
            # Both or neither have wikidata — keep the more "natural" name
            # (prefer First Last over Last First: first word should be shorter
            # for EDGAR LAST FIRST format, but simplest heuristic is alphabetical)
            keep, dead = (p, other) if p["name"] < other["name"] else (other, p)

        migrated = _migrate_person_edges(dead["id"], keep["id"])

        # Delete the dead node
        run_command("MATCH (p:Person {id: $pid}) DETACH DELETE p", {"pid": dead["id"]})

        visited.add(pid)
        visited.add(other["id"])
        merged.append({
            "kept":     keep["name"],
            "deleted":  dead["name"],
            "edges_migrated": migrated,
        })

    return {"pairs_merged": len(merged), "detail": merged}


# ── Proxy statement endpoints ───────────────────────────────────────────────────

@router.post("/proxy-statement/run")
def proxy_statement_run(
    company: str = Query(..., min_length=2,
                         description="Company name to search for on EDGAR"),
    _: dict = Depends(require_admin),
):
    """
    Parse the most recent DEF 14A proxy statement for a company and return
    per-person voting power percentages from the beneficial ownership table.
    Read-only — does not write to the database.
    """
    from app.scraper.proxy_statement import fetch_proxy_ownership
    return fetch_proxy_ownership(company)


import re as _re

# Common nickname → formal first-name mappings
_NICKNAMES: dict[str, str] = {
    "larry":  "lawrence",
    "bill":   "william",
    "bob":    "robert",
    "dick":   "richard",
    "chuck":  "charles",
    "jim":    "james",
    "mike":   "michael",
    "ted":    "edward",
    "tom":    "thomas",
    "ken":    "kenneth",
    "jeff":   "jeffrey",
    "steve":  "steven",
    "dave":   "david",
    "andy":   "andrew",
    "tony":   "anthony",
    "joe":    "joseph",
    "jack":   "john",
    "alex":   "alexander",
    "liz":    "elizabeth",
    "beth":   "elizabeth",
    "kate":   "katherine",
    "sue":    "susan",
    "jen":    "jennifer",
    "sam":    "samuel",
    "matt":   "matthew",
    "dan":    "daniel",
    "tim":    "timothy",
    "pat":    "patricia",
    "chris":  "christopher",
    "nick":   "nicholas",
}


def _name_words(name: str) -> list[str]:
    """Strip punctuation and split into words."""
    return name.replace(".", "").replace(",", "").strip().split()


def _person_name_variants(name: str) -> list[str]:
    """
    Generate name candidates to match SEC reversed-name format and nicknames.

    SEC filings store names as "Last First [Mid]" (no periods).
    Proxy statements use "First [Mid.] Last".

    Examples:
      "Sergey Brin"       → [..., "Brin Sergey"]
      "Warren E. Buffett" → [..., "Buffett Warren E", "Buffett Warren"]
      "Larry Page"        → [..., "Page Larry", "Lawrence Page", "Page Lawrence"]
    """
    variants: list[str] = [name]
    clean = name.replace(".", "").replace(",", "").strip()
    words = clean.split()

    if len(words) == 2:
        variants.append(f"{words[1]} {words[0]}")
    elif len(words) == 3:
        variants.append(f"{words[2]} {words[0]} {words[1]}")
        variants.append(f"{words[2]} {words[0]}")
        variants.append(f"{words[0]} {words[2]}")

    if clean != name:
        variants.append(clean)

    # Nickname expansion on the first word
    if words:
        nick = words[0].lower()
        formal = _NICKNAMES.get(nick)
        if formal:
            formal_cap = formal.capitalize()
            exp_words = [formal_cap] + words[1:]
            variants.append(" ".join(exp_words))
            if len(exp_words) == 2:
                variants.append(f"{exp_words[1]} {exp_words[0]}")
            elif len(exp_words) == 3:
                variants.append(f"{exp_words[2]} {exp_words[0]} {exp_words[1]}")
                variants.append(f"{exp_words[2]} {exp_words[0]}")

    return list(dict.fromkeys(variants))


def _entity_name_variants(name: str) -> list[str]:
    """
    Generate entity name candidates that differ only in articles/punctuation.

    Handles cases like:
      "The Vanguard Group, Inc." → also tries "Vanguard Group, Inc.", "Vanguard Group Inc"
      "BlackRock, Inc."          → also tries "BlackRock Inc", "Blackrock Inc."
    """
    variants: list[str] = [name]
    # Strip leading "The "
    no_the = _re.sub(r"^the\s+", "", name.strip(), flags=_re.IGNORECASE)
    if no_the != name:
        variants.append(no_the)
    # Strip commas and periods
    for base in [name, no_the]:
        no_punct = base.replace(",", "").replace(".", "")
        variants.append(no_punct)
    return list(dict.fromkeys(variants))


def _is_reordering(proxy_name: str, db_name: str) -> bool:
    """True when proxy and DB name contain exactly the same words in a different order."""
    pw = sorted(_name_words(proxy_name.lower()))
    dw = sorted(_name_words(db_name.lower()))
    return pw == dw and proxy_name.replace(".", "").replace(",", "").strip() != \
           db_name.replace(".", "").replace(",", "").strip()


@router.post("/proxy-statement/write")
def proxy_statement_write(
    company: str = Query(..., min_length=2,
                         description="Company name to search for on EDGAR"),
    entity_id: str | None = Query(
        None,
        description="DB entity ID of the target company (overrides name lookup). "
                    "Use this when the EDGAR name differs from the DB name, "
                    "e.g. company=Alphabet&entity_id=<google-uuid>",
    ),
    _: dict = Depends(require_admin),
):
    """
    Fetch the most recent DEF 14A proxy statement and write voting_power_pct
    onto active OWNS edges in the database.

    Owner names are matched by: exact full_name / name, normalised name,
    SEC reversed-name variants, nickname expansion, and entity article/
    punctuation variants.  When a match is a word-reordering (e.g. 'Brin
    Sergey' in DB matched by 'Sergey Brin' from proxy), the DB node name
    is updated to the proxy's form in-place.
    Edges that cannot be matched are reported in 'not_found_in_db'.
    """
    from app.scraper.proxy_statement import fetch_proxy_ownership
    from app.scraper.mapper import normalize_entity_name

    proxy = fetch_proxy_ownership(company)
    if proxy.get("error") and not proxy.get("owners"):
        return proxy

    # ── Find the company node ──────────────────────────────────────────────────
    if entity_id:
        company_rows = run_query(
            "MATCH (c {id: $id}) RETURN c.id AS id, c.name AS name LIMIT 1",
            {"id": entity_id},
        )
    else:
        company_norm = normalize_entity_name(company)
        company_rows = run_query(
            """MATCH (c:Company)
               WHERE c.name_normalized = $norm OR c.name = $name
               RETURN c.id AS id, c.name AS name
               LIMIT 5""",
            {"norm": company_norm, "name": company},
        )
    if not company_rows:
        hint = f" (try passing entity_id directly)" if not entity_id else ""
        return {**proxy, "db_error": f"Company not found in DB: '{company}'{hint}"}

    company_id   = company_rows[0]["id"]
    company_name = company_rows[0]["name"]

    updated:   list[dict] = []
    not_found: list[str]  = []
    skipped:   list[str]  = []

    for owner in proxy.get("owners", []):
        name = owner["name"]
        pct  = owner.get("voting_power_pct")
        if pct is None:
            skipped.append(name)
            continue

        # Combine person (reordering + nickname) and entity (article/punct) variants
        variants = list(dict.fromkeys(
            _person_name_variants(name) + _entity_name_variants(name)
        ))
        # Normalised form: also strip leading "The" before normalising
        name_no_the = _re.sub(r"^the\s+", "", name.strip(), flags=_re.IGNORECASE)
        name_norm     = normalize_entity_name(name)
        name_norm_the = normalize_entity_name(name_no_the)

        # ── Find active OWNS edge owner → company ──────────────────────────
        match_rows = run_query(
            """MATCH (n)-[r:OWNS]->(c {id: $cid})
               WHERE r.until IS NULL
                 AND (n.full_name IN $variants OR n.name IN $variants
                      OR n.name_normalized IN $norms)
               RETURN n.id AS oid,
                      coalesce(n.full_name, n.name) AS matched_name,
                      r.stake_percent   AS stake,
                      r.file_date       AS file_date,
                      r.source_id       AS source_id,
                      r.ownership_type  AS ownership_type,
                      r.since           AS since
               LIMIT 1""",
            {"cid": company_id, "variants": variants,
             "norms": list(dict.fromkeys([name_norm, name_norm_the]))},
        )
        if not match_rows:
            not_found.append(name)
            continue

        row          = match_rows[0]
        oid          = row["oid"]
        matched_name = row["matched_name"]

        # ── Auto-correct reversed DB names to proxy form ──────────────────
        # e.g. "Brin Sergey" → "Sergey Brin", "Buffett Warren E" → "Warren E. Buffett"
        name_corrected = False
        if matched_name and _is_reordering(name, matched_name):
            run_command(
                """MATCH (n {id: $oid})
                   SET n.full_name = $proxy_name""",
                {"oid": oid, "proxy_name": name},
            )
            name_corrected = True

        # ── Delete old edge and recreate with voting_power_pct ────────────
        run_command(
            """MATCH (n {id: $oid})-[r:OWNS]->(c {id: $cid})
               WHERE r.until IS NULL
               DELETE r""",
            {"oid": oid, "cid": company_id},
        )
        run_command(
            """MATCH (n {id: $oid}), (c {id: $cid})
               CREATE (n)-[:OWNS {
                   stake_percent:     $stake,
                   file_date:         $file_date,
                   source_id:         $source_id,
                   ownership_type:    $ownership_type,
                   since:             $since,
                   until:             null,
                   voting_power_pct:  $pct
               }]->(c)""",
            {
                "oid":            oid,
                "cid":            company_id,
                "stake":          row.get("stake"),
                "file_date":      row.get("file_date"),
                "source_id":      row.get("source_id"),
                "ownership_type": row.get("ownership_type"),
                "since":          row.get("since"),
                "pct":            pct,
            },
        )
        entry: dict = {
            "proxy_name":       name,
            "db_name":          matched_name,
            "voting_power_pct": pct,
        }
        if name_corrected:
            entry["name_corrected"] = True
        updated.append(entry)

    return {
        "company":              company_name,
        "filing_date":          proxy.get("filing_date"),
        "share_class_structure": proxy.get("share_class_structure"),
        "updated":              updated,
        "not_found_in_db":      not_found,
        "skipped_no_pct":       skipped,
    }
