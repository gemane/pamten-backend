"""
Entity resolution by identifier — sequential single-field indexed lookups.

An OR across identifier fields (`WHERE e.wikidata_id=$w OR e.name=$n OR
e.name_normalized=$nn`) forces ArcadeDB to **full-scan** the Entity type: ~11s
on 3M entities, because its query planner does not index-union OR branches (even
when every branch is an indexed equality). Resolving each field in priority
order with its own indexed equality lookup is ~0.1s — the branches short-circuit
on the first hit.

Priority: authoritative external ids first (wikidata_id, sec_cik, lei_id,
companies_house_id), then the canonical normalized name, then the raw name.
"""

# All of these are indexed on Entity (see db/schema.py).
_RESOLVE_FIELDS: tuple[str, ...] = (
    "wikidata_id", "sec_cik", "lei_id", "companies_house_id",
    "name_normalized", "name",
)


def resolve_entity_id(session, *, label: str = "Entity", **ids) -> str | None:
    """Return the id of an existing `label` node matching any of the given
    identifiers, checked in priority order via single indexed lookups. Unknown
    or falsy identifier values are skipped. Returns None if nothing matches."""
    for field in _RESOLVE_FIELDS:
        value = ids.get(field)
        if not value:
            continue
        rec = session.run(
            f"MATCH (e:{label}) WHERE e.{field} = $v RETURN e.id AS id LIMIT 1",
            v=value,
        ).single()
        if rec:
            return rec["id"]
    return None
