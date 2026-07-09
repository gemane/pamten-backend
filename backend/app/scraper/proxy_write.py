"""Proxy-statement writer: fetch a DEF 14A and write voting_power_pct onto
active OWNS edges. Extracted from the scraper router; behaviour unchanged.
"""
import re as _re
from app.db.arcadedb import run_query, run_command
from app.scraper.names import _person_name_variants, _entity_name_variants, _is_reordering


def write_proxy_ownership(company: str, entity_id: str | None = None) -> dict:
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
        hint = " (try passing entity_id directly)" if not entity_id else ""
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
