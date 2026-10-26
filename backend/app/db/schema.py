"""
Schema bootstrap: create the vertex types, properties, and indexes the app
relies on for its lookups.

Without these indexes the name-based upserts in the scraper (e.g.
`MATCH (e:Entity) WHERE e.wikidata_id = ... OR e.name_normalized = ...`)
do a full scan of every node, which makes bulk imports O(n^2). The unique
index on User.email also enforces the account-uniqueness the app assumes.

Design notes
------------
- Idempotent: vertex types and indexes use `IF NOT EXISTS`; property
  statements are guarded by the per-statement try/except (ArcadeDB does
  not support `IF NOT EXISTS` on `CREATE PROPERTY`). The rules
  (`constraints()`: mandatory ids, vocabularies, ISO-2 countries, 0–100
  percentages, a flag's default) are `ALTER PROPERTY` statements issued
  only where the catalog does not already hold them; the server refuses
  one while a stored row breaks it, which the log then shows.
- Fault-tolerant: each statement is guarded individually, so a single
  failure never aborts the rest, and an unreachable DB is skipped with one
  warning rather than crashing the caller. This lets it run best-effort on
  startup while remaining useful as an explicit `manage.py init-schema`.
"""
import logging

from app import addresses
from app.db.arcadedb import run_sql, run_sqlscript

log = logging.getLogger(__name__)

# (vertex type, property, uniqueness) — the properties queries filter/join on.
_INDEXES: list[tuple[str, str, str]] = [
    ("Entity",   "id",                  "UNIQUE"),
    ("Entity",   "name",               "NOTUNIQUE"),
    ("Entity",   "name_normalized",    "NOTUNIQUE"),
    ("Entity",   "wikidata_id",        "NOTUNIQUE"),
    ("Entity",   "sec_cik",            "NOTUNIQUE"),
    ("Entity",   "lei_id",             "NOTUNIQUE"),
    ("Entity",   "companies_house_id", "NOTUNIQUE"),
    ("Entity",   "register_id",        "NOTUNIQUE"),  # "{RA code}:{number}" hard id
    ("Entity",   "cusip",              "NOTUNIQUE"),  # 13F info-table join key (NOT a dedup id)
    ("Entity",   "address_normalized", "NOTUNIQUE"),  # dup-detection corroborator (was registered_address until 2026-10-25)
    # Filtered on every map drill-down (/entities/by-country/{country}) and by the
    # country-scoped search; without an index each one scans every Entity.
    ("Entity",   "country",            "NOTUNIQUE"),
    ("Entity",   "type",               "NOTUNIQUE"),
    ("Person",   "id",              "UNIQUE"),
    # A 13D filing group: its own vertex type since 2026-10-26 (it was an
    # Entity of type `voting_group`, which every query that assumes an entity
    # is a legal person had to filter out — eight did, the rest forgot)
    ("VotingGroup", "id",            "UNIQUE"),
    ("Person",   "full_name",       "NOTUNIQUE"),
    ("Person",   "wikidata_id",     "NOTUNIQUE"),
    ("Person",   "sec_cik",         "NOTUNIQUE"),
    # Element-wise over the LIST: `$name IN p.aliases` is how the scoped
    # post-scrape dedup finds a node recorded under another of its names.
    # Typed LIST below — declared STRING, ArcadeDB rejects the comparison with
    # "IN requires a list on the right side, got String". `alias` until
    # 2026-10-25 — one name for the one list on both node types now.
    ("Person",   "aliases",         "NOTUNIQUE"),
    ("Source",   "id",              "UNIQUE"),
    ("User",     "id",              "UNIQUE"),
    ("User",     "email",           "UNIQUE"),
    ("MergeLog", "id",              "UNIQUE"),
    ("MergeLog", "keep_id",         "NOTUNIQUE"),
    # Forwarding addresses for ids a merge folded away, so shared links, cached
    # client ids and federation peers don't dangle. old_id is UNIQUE — an id can
    # only have been merged into one survivor — and the lookup must be indexed,
    # since it runs on every by-id miss.
    ("MergedId", "old_id",          "UNIQUE"),
    ("MergedId", "new_id",          "NOTUNIQUE"),
    ("Peer",     "id",              "UNIQUE"),
    ("Peer",     "base_url",        "NOTUNIQUE"),
    ("ScrapeRun", "id",             "UNIQUE"),
    ("ScrapeRun", "started_at",     "NOTUNIQUE"),
    ("ScrapeRun", "entity_id",      "NOTUNIQUE"),
    # Verification flags (user reports that a node/edge looks wrong).
    ("Flag",      "id",             "UNIQUE"),
    ("Flag",      "status",         "NOTUNIQUE"),
    ("Flag",      "target_kind",    "NOTUNIQUE"),
    ("Flag",      "node_id",        "NOTUNIQUE"),
    ("Flag",      "from_id",        "NOTUNIQUE"),
    ("Flag",      "to_id",          "NOTUNIQUE"),
    # Suppressions — a moderator override hiding a wrong edge (Phase-B resolution).
    ("Suppression", "id",           "UNIQUE"),
    ("Suppression", "from_id",      "NOTUNIQUE"),
    ("Suppression", "to_id",        "NOTUNIQUE"),
    ("Suppression", "node_id",      "NOTUNIQUE"),
    # Pins — a moderator-corrected OWNS value that overrides the scraped one.
    ("Pin",         "id",           "UNIQUE"),
    ("Pin",         "from_id",      "NOTUNIQUE"),
    ("Pin",         "to_id",        "NOTUNIQUE"),
    # Import checkpoints — e.g. the last GLEIF publish a delta update applied, so
    # the daily refresh can pick a catch-up interval that covers any missed runs.
    ("ImportState", "key",          "UNIQUE"),
    # Fruitless on-demand searches, so the same one is not run at every source
    # again the moment a user clicks twice. Keyed by normalised query + country,
    # which is exactly what makes two searches "the same search".
    ("ScrapeMiss",  "key",          "UNIQUE"),
    # Usage measurement — aggregate counters only, never linked to a user, a
    # session or an IP (see app/analytics.py for why that is the whole design).
    ("SearchDemand", "key",          "UNIQUE"),
    ("UsageCounter", "key",          "UNIQUE"),
    ("EndpointStat", "key",          "UNIQUE"),
    # The same counters per calendar week (key = lifetime key + "|2026-W38"),
    # so the weekly digest can say how many — still totals, nothing per event.
    ("SearchWeek",   "key",          "UNIQUE"),
    ("SearchWeek",   "week",         "NOTUNIQUE"),
    ("UsageWeek",    "key",          "UNIQUE"),
    ("UsageWeek",    "week",         "NOTUNIQUE"),
    # One stored digest per week, so the next one can report the change.
    ("WeeklyReport", "week",         "UNIQUE"),
    # Address -> coordinate cache, so a shared registered-agent building is
    # geocoded once rather than once per company registered there (24 companies
    # share one Wilmington address in the dev graph alone). UNIQUE on the cleaned
    # query string, which is also the only way it is ever looked up.
    ("GeoCache",    "query",        "UNIQUE"),
    # Runtime settings a deploy should not be needed to change — currently the
    # mobile minimum-supported-version policy (see routers/app_version.py). Keyed
    # so a lookup is indexed; the value is JSON so a policy updates atomically.
    ("AppSetting",  "key",          "UNIQUE"),
    # Rate-limit buckets: sliding-window attempt timestamps keyed by bucket id
    # (e.g. "login:<ip>:<email>", "email:<email>", "mfa:<user_id>").
    ("RateLimit",   "key",          "UNIQUE"),
    # Per-source assertions behind an edge (see app/claims.py). claim_key is
    # UNIQUE — one row per (kind, from, to, source) — which is what makes a
    # re-import update a source's claim instead of stacking another copy.
    # from_id/to_id are the lookup path when rendering an entity's provenance.
    ("Claim",       "claim_key",    "UNIQUE"),
    ("Claim",       "from_id",      "NOTUNIQUE"),
    ("Claim",       "to_id",        "NOTUNIQUE"),
    ("Claim",       "source_id",    "NOTUNIQUE"),
    # The dated stake statements a claim overwrote (see app/stake_history.py):
    # read by holder (to_id) for a company's timeline and time travel, by
    # holding (from_id) for what it owns; a merge re-points both columns.
    ("StakeHistory", "from_id",     "NOTUNIQUE"),
    ("StakeHistory", "to_id",       "NOTUNIQUE"),
    # A source's statement of what a company does (app.industry): one row per
    # (entity, scheme, position), keyed by their hash; read per entity.
    ("IndustryCode", "key",         "UNIQUE"),
    ("IndustryCode", "entity_id",   "NOTUNIQUE"),
    # The NACE node the panel draws the sector tree from — indexed so "every
    # company in 23.32" is a lookup, not a scan.
    ("Entity",       "nace_code",   "NOTUNIQUE"),
    # Refresh tokens (see app/auth/refresh.py). token_hash is UNIQUE because it
    # identifies the row on every refresh and two tokens must never collide;
    # user_id and family_id are the revoke-all and replay-response paths.
    ("RefreshToken", "token_hash",  "UNIQUE"),
    ("RefreshToken", "user_id",     "NOTUNIQUE"),
    ("RefreshToken", "family_id",   "NOTUNIQUE"),
]

# Edge types the app creates via Cypher and needs to exist up front (init-schema
# and the startup bootstrap create them).
_EDGE_TYPES: list[str] = [
    "OWNS", "HAS_ROLE", "RELATED_TO", "MEMBER_OF", "DUAL_LISTED_WITH", "SUCCEEDED_BY",
    "NOT_DUPLICATE",
]

# Full-text indexes powering /search. A FULL_TEXT index (tokenized, queried with
# the `CONTAINSTEXT` operator) turns name search from an un-indexable
# `toLower(name) CONTAINS` full scan — ~12s on 3M entities — into an index
# lookup. It lives on a dedicated `search_text` column (name [+ description])
# because FULL_TEXT can't share a property with the existing LSM indexes the
# scrapers use for exact-match resolution. Populate search_text with
# `manage.py backfill-search` (and the BODS importer sets it inline).
_FULLTEXT_INDEXES: list[tuple[str, str]] = [
    ("Entity", "search_text"),
    ("Person", "search_text"),
    ("VotingGroup", "search_text"),     # "anheuser voting" finds the bloc
]


# Indexes on an EDGE property. Separate from `_INDEXES` because that list's first
# element drives `CREATE VERTEX TYPE`, and OWNS is an edge — appending it there
# would try to create a vertex type of the same name.
#
# `psc_self_link` is the Companies House PSC appointment link. The incremental
# refresh finds the edge a changed snapshot record belongs to by matching on it, in
# batches of ~1000 via `WHERE psc_self_link IN :links`; unindexed that is a full
# scan of every OWNS edge per batch.
_EDGE_INDEXES: list[tuple[str, str, str]] = [
    ("OWNS", "psc_self_link", "NOTUNIQUE"),
]

#: Indexed properties that are NOT strings. Declaring `aliases` STRING makes
#: ArcadeDB reject `$name IN p.aliases` ("IN requires a list on the right side,
#: got String") — the property type is part of the contract, not decoration.
_PROPERTY_TYPES: dict[tuple[str, str], str] = {
    ("Person", "aliases"): "LIST",
}

#: Declared types for the properties that are not strings, indexed or not —
#: every number, flag and list a writer sets (2026-10-24). ArcadeDB stores a
#: schemaless value in whatever type the writer's JSON happened to carry:
#: `stake_percent` sat as DOUBLE on 974 edges and INTEGER on 20, `shares` as
#: DOUBLE, INTEGER and LONG side by side. A declared type makes the server
#: coerce every write (an int into a DOUBLE property is stored 20.0; a
#: numeric string into an INTEGER is parsed; a value that cannot convert is
#: refused, which is what we want of "abc" for a year). Declaring a type
#: never touches the rows already stored — `manage.py heal-property-types`
#: rewrites those. Dates stay STRING on purpose: the writers keep a source's
#: precision ("2019-00-00" is a Wikidata year), which no DATE type can hold.
_TYPED_PROPERTIES: list[tuple[str, str, str]] = [
    # Entity — numbers
    ("Entity", "founded",          "INTEGER"),   # the year the business began
    ("Entity", "employees",        "INTEGER"),
    ("Entity", "employees_as_of",  "INTEGER"),
    ("Entity", "name_credibility", "INTEGER"),
    ("Entity", "scrape_depth",     "INTEGER"),
    ("Entity", "revenue",          "DOUBLE"),
    # the pins of both places (app.addresses)
    *[("Entity", f, "DOUBLE") for f in addresses.coordinate_fields()],
    # Entity — flags
    ("Entity", "is_nominee",        "BOOLEAN"),
    ("Entity", "active",            "BOOLEAN"),
    ("Entity", "on_demand_scraped", "BOOLEAN"),
    # Entity — lists
    ("Entity", "aliases",              "LIST"),
    ("Entity", "countries",            "LIST"),
    ("Entity", "hq_locations",         "LIST"),
    ("Entity", "former_names",         "LIST"),
    ("Entity", "other_names",          "LIST"),   # GLEIF's other names, folded into search_text
    ("Entity", "uk_sic_codes",         "LIST"),
    ("Entity", "cusips",               "LIST"),   # one issuer, several 13F CUSIPs
    ("Entity", "former_register_ids",  "LIST"),   # register pairs it used to carry
    ("Entity", "source_statement_ids", "LIST"),
    # Person
    ("Person", "nationalities", "LIST"),
    # OWNS — the stake and the counts behind it
    ("OWNS", "stake_percent",      "DOUBLE"),
    ("OWNS", "voting_power_pct",   "DOUBLE"),
    # Share counts are DOUBLE, not LONG: a Form 4 reports fractional shares
    # (dividend reinvestment, plan units — 562,701.427 sat on a dev edge and
    # ArcadeDB refused the LONG declaration over it, the first deploy of the
    # types), and a double holds every whole count a filing states exactly
    # (16 billion Apple shares is far below 2^53).
    ("OWNS", "shares",             "DOUBLE"),
    ("OWNS", "shares_outstanding", "DOUBLE"),
    ("OWNS", "voting_shares",      "DOUBLE"),
    ("OWNS", "value_usd",          "DOUBLE"),
    ("OWNS", "credibility_score",  "INTEGER"),
    ("OWNS", "stale",              "BOOLEAN"),
    ("OWNS", "shortcut",           "BOOLEAN"),
    ("OWNS", "also_ultimate",      "BOOLEAN"),
    ("OWNS", "interest_types",     "LIST"),
    ("HAS_ROLE",     "credibility_score", "INTEGER"),
    ("SUCCEEDED_BY", "credibility_score", "INTEGER"),
    # Claim — the same facts a source asserts, typed alike
    ("Claim", "stake_percent",      "DOUBLE"),
    ("Claim", "voting_power_pct",   "DOUBLE"),
    ("Claim", "shares",             "DOUBLE"),
    ("Claim", "shares_outstanding", "DOUBLE"),
    ("Claim", "voting_shares",      "DOUBLE"),
    ("Claim", "credibility_score",  "INTEGER"),
    ("StakeHistory", "stake_percent",    "DOUBLE"),
    ("StakeHistory", "previous_percent", "DOUBLE"),
    ("IndustryCode", "position", "INTEGER"),
    ("Source", "credibility_score", "INTEGER"),
    ("Source", "enabled",           "BOOLEAN"),
    ("Source", "verified",          "BOOLEAN"),   # a federation peer's key check
    # the rest of the graph and the ops tables (2026-10-25: a survey of dev
    # found these stored untyped — ints, floats and flags the writers' JSON
    # happened to carry; a declaration pins them)
    ("VotingGroup",  "member_keys",       "LIST"),      # the group's roster (cik|name per party)
    ("Person",       "scrape_depth",      "INTEGER"),
    ("Person",       "on_demand_scraped", "BOOLEAN"),
    ("Pin",          "stake_percent",     "DOUBLE"),
    ("MergeLog",     "count",             "INTEGER"),
    ("ScrapeRun",    "total",             "INTEGER"),
    ("GeoCache",     "lat",               "DOUBLE"),
    ("GeoCache",     "lng",               "DOUBLE"),
    ("SearchDemand", "searches",          "INTEGER"),
    ("SearchDemand", "selected",          "INTEGER"),
    ("SearchDemand", "zero_results",      "INTEGER"),
    ("SearchWeek",   "searches",          "INTEGER"),
    ("SearchWeek",   "selected",          "INTEGER"),
    ("SearchWeek",   "zero_results",      "INTEGER"),
    ("UsageCounter", "count",             "INTEGER"),
    ("UsageWeek",    "count",             "INTEGER"),
    ("EndpointStat", "count",             "INTEGER"),
    ("ImportState",  "edge_key_version",  "INTEGER"),
    ("RefreshToken", "expires_at",        "DOUBLE"),    # epoch seconds
    ("RefreshToken", "issued_at",         "DOUBLE"),
    ("RefreshToken", "revoked_at",        "DOUBLE"),
    ("User",         "email_verified",    "BOOLEAN"),
    ("User",         "mfa_enabled",       "BOOLEAN"),
    ("User",         "recovery_code_hashes", "LIST"),
    ("RateLimit",    "timestamps",        "LIST"),
    ("Peer",         "enabled",           "BOOLEAN"),
    ("Peer",         "credibility_score", "INTEGER"),
]


def typed_properties() -> list[tuple[str, str, str]]:
    """Every declared non-string property as (type, property, ArcadeDB type) —
    the indexed ones from `_PROPERTY_TYPES` and the rest from
    `_TYPED_PROPERTIES`; what the heal converts stored rows to."""
    out = [(t, p, k) for (t, p), k in _PROPERTY_TYPES.items()]
    out += [row for row in _TYPED_PROPERTIES if (row[0], row[1]) not in _PROPERTY_TYPES]
    return out


#: Every STRING property a writer sets, by type — the ledger (2026-10-25).
#: Numbers, flags and lists are typed above; the indexed and ruled strings
#: declare themselves, and are listed here anyway so one list reads as the
#: model (`string_properties()` filters what another list already declares).
#: ArcadeDB accepts a property nobody declared — there is no strict mode — so
#: this is not a gate but a ledger: the catalog then IS the model, and
#: `undeclared_properties()` (`manage.py schema-audit`) names a stored
#: property the ledger does not know, which is the shape a typo'd writer
#: takes. A conformance test holds the writers' own property lists against it.
_STRING_PROPERTIES: dict[str, tuple[str, ...]] = {
    "Entity": (
        "id", "name", "name_normalized", "type", "country", "description",
        # both places: readable address, matching key, parts, geocode precision
        *[f for f in addresses.all_fields() if f not in addresses.coordinate_fields()],
        "legal_form", "legal_form_code", "registration_authority", "registration_authority_code",
        "registration_number", "register_id", "country_registered", "jurisdiction_code",
        "cusip", "lei_id", "sec_cik", "companies_house_id", "wikidata_id", "nace_code",
        "website", "logo_url", "search_text", "source_id", "source_url",
        "validation_sources", "gleif_registration_status", "lei_registration_date",
        "incorporated_on", "dissolved_on", "company_status",
        "created_at", "last_scraped_at", "sec_13f_scraped_at",
        "sec_ex21_ingested", "sec_acquisitions_ingested", "sec_formd_ingested",
        "no_direct_parent_reason", "no_ultimate_parent_reason",
    ),
    "Person": (
        "id", "full_name", "first_name", "last_name", "birth_date", "death_date", "birth_place",
        "description", "wikipedia_url", "image_url", "source_id", "wikidata_id", "sec_cik",
        "search_text", "created_at", "last_scraped_at",
    ),
    "Source": ("id", "name", "url", "type", "kind", "description", "registry_key", "data_mode", "key_id"),
    "Claim": (
        "claim_key", "kind", "from_id", "to_id", "source_id", "ownership_type", "role", "since",
        "until", "source_url", "source_date", "share_class", "denominator_date", "event_date",
        "filing_type", "since_basis", "since_source_url", "structure_basis", "read_from",
        "validation_sources", "validation_documents", "first_seen_at", "last_seen_at",
        "until_source_url",
    ),
    "OWNS": (
        "ownership_type", "since", "until", "until_reason", "source_id", "source_url",
        "source_date", "last_scraped_at", "direct_or_indirect", "psc_self_link", "share_class",
        "denominator_date", "event_date", "stale_since", "stale_reason", "stale_source_url",
        "ultimate_since", "ultimate_until", "validation_sources",
        "validation_documents", "filing_type", "since_basis", "since_source_url",
        "since_not_before", "structure_basis", "read_from",
    ),
    "HAS_ROLE": ("role", "since", "until", "source_id", "source_url", "source_date", "last_scraped_at", "read_from",
                 "until_source_url"),
    # `source_url` / `source_date`: the 13F affiliate writer stamps them
    # (`runner._upsert_affiliate`); the ledger lacked them until 2026-10-26
    "RELATED_TO": ("relation", "source_id", "source_url", "source_date", "last_scraped_at"),
    # A party of a 13D filing group pointing at the group (2026-10-26; was
    # RELATED_TO {relation: group_member})
    "MEMBER_OF": ("source_id", "last_scraped_at"),
    # A 13D filing group. `type` is always `voting_group`: the API's node
    # kind, which the client reads off every node it draws — the vertex type
    # says the same thing to the database
    "VotingGroup": ("id", "name", "name_normalized", "search_text", "description", "type",
                    "source_id", "created_at", "last_scraped_at"),
    "SUCCEEDED_BY": ("since", "source_id", "source_url", "source_date", "last_scraped_at"),
    "DUAL_LISTED_WITH": ("source_id", "source_url", "source_date", "last_scraped_at"),
    "NOT_DUPLICATE": ("at",),
    "StakeHistory": ("from_id", "to_id", "source_id", "as_of", "filing_type", "source_url", "recorded_at"),
    "IndustryCode": ("key", "entity_id", "scheme", "code", "label", "nace_code", "source_id",
                     "source_url", "source_date", "first_seen_at", "last_seen_at"),
    "MergedId": ("old_id", "new_id", "id", "kind", "at"),
    "MergeLog": ("id", "keep_id", "kind", "keep_name", "dup_id", "dup_name", "at"),
    "Flag": ("id", "target_kind", "category", "note", "status", "reporter_kind", "reporter_id",
             "reporter_fp", "from_id", "to_id", "role", "node_id", "created_at", "updated_at"),
    "Suppression": ("id", "target_kind", "node_id", "from_id", "to_id", "role", "flag_id", "created_at"),
    "Pin": ("id", "target_kind", "from_id", "to_id", "ownership_type", "flag_id", "created_at", "updated_at"),
    "ScrapeRun": ("id", "source", "target", "status", "started_at", "finished_at", "error", "reason", "entity_id"),
    "ScrapeMiss": ("key", "missed_at"),
    "Peer": ("id", "name", "base_url", "auth_token", "public_key"),
    "User": ("id", "email", "password_hash", "role", "language", "created_at", "totp_secret", "mfa_pending_secret"),
    "GeoCache": ("query", "precision", "checked_at"),
    "SearchDemand": ("key", "query", "country", "first_seen", "last_seen"),
    "SearchWeek": ("key", "week", "query", "country"),
    "UsageCounter": ("key", "first_seen", "last_seen"),
    "UsageWeek": ("key", "week", "event"),
    "EndpointStat": ("key", "last_seen"),
    "ImportState": ("key", "scope", "last_publish_date", "last_run_at", "snapshot_date", "holder", "acquired_at"),
    "RefreshToken": ("id", "token_hash", "user_id", "family_id", "replaced_by"),
    "AppSetting": ("key", "value", "updated_at"),
    "RateLimit": ("key",),
    "WeeklyReport": ("week", "generated_at", "json"),
}


def _declared_elsewhere() -> set[tuple[str, str]]:
    return ({(t, p) for t, p, _ in _TYPED_PROPERTIES} | set(_PROPERTY_TYPES)
            | {(t, p) for t, p, _ in _INDEXES} | {(t, p) for t, p, _ in _EDGE_INDEXES}
            | set(_FULLTEXT_INDEXES) | set(_RULED_STRINGS))


def string_properties() -> list[tuple[str, str]]:
    """The ledger's strings that no other list declares — what the bootstrap
    issues a `CREATE PROPERTY … STRING` for."""
    elsewhere = _declared_elsewhere()
    return [(t, p) for t, props in _STRING_PROPERTIES.items() for p in props if (t, p) not in elsewhere]


def all_properties() -> set[tuple[str, str]]:
    """Every (type, property) the schema declares, by any list."""
    return _declared_elsewhere() | {(t, p) for t, props in _STRING_PROPERTIES.items() for p in props}


def undeclared_properties(sample: int = 5000) -> dict[str, dict[str, dict]]:
    """{type: {property: {"rows": n, "null_only": bool}}} for what stored rows
    carry and the catalog does not declare — the audit behind `manage.py
    schema-audit`. `null_only` marks a key no row gives a value (the leftover
    of a renamed field — dev kept 97 null `founded_date` keys after the rename
    to `incorporated_on`), which is removed rather than declared. Reads up to
    `sample` rows of every type the catalog lists; {} when the catalog cannot
    be read."""
    try:
        existing = _existing_schema()
    except Exception:  # noqa: BLE001 — an unreadable catalog audits nothing
        return {}
    if existing is None:
        return {}
    types, _, declared, _ = existing
    out: dict[str, dict[str, dict]] = {}
    for vtype in sorted(t for t in types if t):
        have = {k.split(".", 1)[1] for k in declared if k.startswith(f"{vtype}.")}
        try:
            rows = run_sql(f"SELECT FROM {vtype} LIMIT {int(sample)}")
        except Exception:  # noqa: BLE001 — a type that cannot be read is not drift
            continue
        found: dict[str, dict] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                if k.startswith("@") or k in have:
                    continue
                entry = found.setdefault(k, {"rows": 0, "null_only": True})
                entry["rows"] += 1
                if v is not None:
                    entry["null_only"] = False
        if found:
            out[vtype] = dict(sorted(found.items()))
    return out


#: Properties that carry a rule below but no type declaration elsewhere (a
#: rule needs a declared property; these are strings).
_RULED_STRINGS: list[tuple[str, str]] = [
    ("Entity", "hq_country"), ("VotingGroup", "type"),
    ("OWNS", "ownership_type"), ("OWNS", "read_from"),
    ("Claim", "kind"), ("Claim", "read_from"), ("Claim", "until_read_from"),
    ("HAS_ROLE", "read_from"), ("HAS_ROLE", "until_reason"),
]

#: The dates: ISO `YYYY-MM-DD`, as every writer normalises a source's date, at
#: the precision the source states — a Wikidata year is `2019-00-00` or
#: `2019`, a PSC birth month `1965-04` — so the rule checks the SHAPE and never
#: the calendar (that stays with the writers). What it refuses is a date in
#: another notation ("4 March 2019", "03/04/2019") or a timestamp in a date
#: field — either of which would sort wrongly against every other row, since
#: the readers compare dates as text. No backslash in the pattern: it travels
#: inside an SQL string literal.
_DATE_PATTERN = "^[0-9]{4}(-[0-9]{2}){0,2}$"
_DATES: list[tuple[str, str]] = [
    ("Entity", "incorporated_on"), ("Entity", "dissolved_on"), ("Entity", "lei_registration_date"),
    ("Person", "birth_date"), ("Person", "death_date"),
    ("OWNS", "since"), ("OWNS", "until"), ("OWNS", "since_not_before"),
    ("OWNS", "ultimate_since"), ("OWNS", "ultimate_until"),
    ("OWNS", "source_date"), ("OWNS", "event_date"), ("OWNS", "denominator_date"),
    ("OWNS", "stale_since"),
    ("HAS_ROLE", "since"), ("HAS_ROLE", "until"), ("HAS_ROLE", "source_date"),
    ("Claim", "since"), ("Claim", "until"), ("Claim", "source_date"),
    ("Claim", "event_date"), ("Claim", "denominator_date"),
    ("StakeHistory", "as_of"),
    ("IndustryCode", "source_date"),
]
#: The timestamps: ISO 8601 with a time AND an offset — what
#: `datetime.now(timezone.utc).isoformat()` writes (`+00:00`; a few hundred
#: older rows carry `Z`, which is the same instant). Seconds are optional
#: because ArcadeDB parses a Cypher value shaped `…T00:00:00Z` into a datetime
#: and stores it back as `…T00:00Z` — the server's own spelling must pass its
#: own rule. A naive timestamp is refused: without its offset it is not a
#: point in time.
_TIMESTAMP_PATTERN = ("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?"
                      "(Z|[+-][0-9]{2}:[0-9]{2})$")
_TIMESTAMPS: list[tuple[str, str]] = [
    ("Entity", "created_at"), ("Entity", "last_scraped_at"), ("Entity", "sec_13f_scraped_at"),
    ("Person", "created_at"),
    ("VotingGroup", "created_at"), ("VotingGroup", "last_scraped_at"),
    ("OWNS", "last_scraped_at"), ("HAS_ROLE", "last_scraped_at"), ("RELATED_TO", "last_scraped_at"),
    ("MEMBER_OF", "last_scraped_at"),
    ("Claim", "first_seen_at"), ("Claim", "last_seen_at"),
    ("StakeHistory", "recorded_at"),
    ("IndustryCode", "first_seen_at"), ("IndustryCode", "last_seen_at"),
]
_RULED_STRINGS += _DATES + _TIMESTAMPS

#: catalog key per ALTER PROPERTY attribute, as `schema:types` reports it
_RULE_KEYS = {"MANDATORY": "mandatory", "NOTNULL": "notNull", "REGEXP": "regexp",
              "MIN": "min", "MAX": "max", "DEFAULT": "default"}

_ISO2 = "^[A-Z]{2}$"

#: The flags that are `true` or `false` and never null — "not asserted" is
#: false. Declared with a DEFAULT (`constraints()`) and healed on an older
#: database (`field_twins.heal_flags`).
FLAGS: tuple[tuple[str, str], ...] = (
    ("OWNS", "stale"), ("OWNS", "shortcut"), ("OWNS", "also_ultimate"),
    ("Entity", "is_nominee"), ("Entity", "on_demand_scraped"), ("Person", "on_demand_scraped"))


def _vocab(values) -> str:
    return "^(" + "|".join(str(getattr(v, "value", v)) for v in values) + ")$"


def constraints() -> list[tuple[str, str, str, str]]:
    """The rules the server enforces beyond a property's type, as
    (type, property, ATTRIBUTE, value) for `ALTER PROPERTY`: the ids and claim
    keys a row cannot exist without (a claim's source among them), the
    vocabularies (built from the enums the code already validates against, so
    a new value needs no second list), the ISO-2 shape of a country, the shape
    of every date and timestamp, the 0–100 range of a percentage, and the
    flags' default. ArcadeDB refuses a write that breaks one with a clear
    message, and refuses to declare a rule while any stored row breaks it —
    so a heal runs before its rule, never after (2026-10-25). A rule leaves a
    null alone (`notnull` is the one that does not)."""
    from app.claims import KIND_OWNS, KIND_ROLE, KIND_SUCCESSION
    from app.models.entity import EntityType
    from app.models.relationship import OwnershipType
    from app.scraper.edge_schema import READ_GRADES
    grades = _vocab(READ_GRADES)
    pct = [("MIN", "0"), ("MAX", "100")]
    rules: list[tuple[str, str, str, str]] = []
    # `Claim.source_id` too (since 2026-10-25): a claim IS what one source
    # says, `record_claim` has always dropped one without a source, and no
    # database held one — the rule makes the writer's refusal the server's.
    for vtype, prop in (("Entity", "id"), ("Person", "id"), ("VotingGroup", "id"), ("Source", "id"),
                        ("Claim", "claim_key"), ("Claim", "kind"), ("Claim", "from_id"),
                        ("Claim", "to_id"), ("Claim", "source_id")):
        rules += [(vtype, prop, "MANDATORY", "true"), (vtype, prop, "NOTNULL", "true")]
    rules += [(vtype, prop, "REGEXP", _DATE_PATTERN) for vtype, prop in _DATES]
    rules += [(vtype, prop, "REGEXP", _TIMESTAMP_PATTERN) for vtype, prop in _TIMESTAMPS]
    rules += [
        ("Claim", "kind", "REGEXP", _vocab((KIND_OWNS, KIND_ROLE, KIND_SUCCESSION))),
        ("Entity", "type", "REGEXP", _vocab(EntityType)),
        ("VotingGroup", "type", "REGEXP", "^voting_group$"),
        ("Entity", "country", "REGEXP", _ISO2),
        ("Entity", "hq_country", "REGEXP", _ISO2),
        ("OWNS", "ownership_type", "REGEXP", _vocab(OwnershipType)),
        ("OWNS", "read_from", "REGEXP", grades),
        ("Claim", "read_from", "REGEXP", grades),
        ("HAS_ROLE", "read_from", "REGEXP", grades),
        ("Claim", "until_read_from", "REGEXP", grades),
        # the two reasons a seat ends that no source stated: the holder died
        # (app.roles.DEATH), the company ceased (app.dissolution.DISSOLUTION)
        ("HAS_ROLE", "until_reason", "REGEXP", _vocab(("death", "dissolution"))),
    ]
    # A flag that means "not asserted" is false, never null: since ArcadeDB
    # 26.10.1 a null compares as false in `<>` and drops out of every filter,
    # so the readers wrapped each in COALESCE. `stale` since 2026-10-25, the
    # other five the same day (`field_twins.FLAGS` heals the rows written
    # before). `Entity.active` stays three-valued on purpose: null means no
    # register has said whether the company still exists.
    rules += [(vtype, prop, "DEFAULT", "false") for vtype, prop in FLAGS]
    for vtype, prop in (("OWNS", "stake_percent"), ("OWNS", "voting_power_pct"),
                        ("Claim", "stake_percent"), ("Claim", "voting_power_pct"),
                        ("StakeHistory", "stake_percent"), ("StakeHistory", "previous_percent")):
        rules += [(vtype, prop, attr, v) for attr, v in pct]
    return rules


def _rule_sql(vtype: str, prop: str, attr: str, value: str) -> str:
    lit = f"'{value}'" if attr == "REGEXP" else value
    return f"ALTER PROPERTY {vtype}.{prop} {attr} {lit}"


def _same_rule(live, value: str) -> bool:
    """The catalog reports `min`/`max`/`default` as strings and the flags as
    booleans; compare on the lower-cased text."""
    return live is not None and str(live).strip().lower() == value.strip().lower()


def _statements(existing: tuple | None = None) -> list[str]:
    """The DDL the schema needs — all of it, or only what `existing` lacks.

    `existing` is (type names, index names, declared properties — a
    {"Type.property": type} map, or bare names from an older caller) as the
    server reports them (the third may be missing altogether). A declared
    property whose type differs from the code's is dropped and declared again. Even an `IF NOT EXISTS` statement takes the exclusive schema
    lock, and on a large database that lock is held for an hour at a time by
    an index compaction: the API's startup used to issue thirty of them,
    each timing out on the client but queuing on the server, and every
    request — searches, profiles — waited behind that queue. With the
    catalog in hand a complete schema means no DDL at all.
    """
    types, indexes, props, rules = (tuple(existing) + (None, None))[:4] if existing else (None, None, None, None)

    def has_type(name: str) -> bool:
        return types is not None and name in types

    def has_index(vtype: str, prop: str) -> bool:
        return indexes is not None and f"{vtype}[{prop}]" in indexes

    def has_property(vtype: str, prop: str) -> bool:
        return props is not None and f"{vtype}.{prop}" in props

    def declared_type(vtype: str, prop: str) -> str | None:
        """The type the server declares for the property, when the catalog
        says (an older caller may pass bare names)."""
        return props.get(f"{vtype}.{prop}") if isinstance(props, dict) else None

    stmts: list[str] = []
    for vtype in sorted({t for t, _, _ in _INDEXES}):
        if not has_type(vtype):
            stmts.append(f"CREATE VERTEX TYPE {vtype} IF NOT EXISTS")
    for etype in _EDGE_TYPES:
        if not has_type(etype):
            stmts.append(f"CREATE EDGE TYPE {etype} IF NOT EXISTS")
    for vtype, prop, kind in _INDEXES:
        if has_index(vtype, prop):
            continue
        # `IF NOT EXISTS` goes BEFORE the type in ArcadeDB SQL — makes the DDL
        # idempotent so re-runs (startup, bulk-load index rebuild) don't log a
        # "property already exists" failure for every property.
        ptype = _PROPERTY_TYPES.get((vtype, prop), "STRING")
        stmts.append(f"CREATE PROPERTY {vtype}.{prop} IF NOT EXISTS {ptype}")
        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) {kind}")
    for etype, prop, kind in _EDGE_INDEXES:
        if has_index(etype, prop):
            continue
        stmts.append(f"CREATE PROPERTY {etype}.{prop} IF NOT EXISTS STRING")
        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {etype} ({prop}) {kind}")
    for vtype, prop in _FULLTEXT_INDEXES:
        if has_index(vtype, prop):
            continue
        stmts.append(f"CREATE PROPERTY {vtype}.{prop} IF NOT EXISTS STRING")
        stmts.append(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) FULL_TEXT")
    # The typed, unindexed properties: a declaration each, nothing to index.
    for vtype, prop, ptype in _TYPED_PROPERTIES:
        if (vtype, prop) in _PROPERTY_TYPES:
            continue
        if has_property(vtype, prop):
            live = declared_type(vtype, prop)
            if live is None or live == ptype:
                continue
            # The server declares another type (shares_outstanding was LONG for a
            # day before it became DOUBLE). ArcadeDB has no `ALTER PROPERTY …
            # TYPE`; dropping the declaration touches no row, and the fresh one
            # coerces every write from then on — heal-property-types rewrites
            # the rows that still carry the old type.
            log.info("redeclaring %s.%s: %s on the server, %s in the code", vtype, prop, live, ptype)
            stmts.append(f"DROP PROPERTY {vtype}.{prop} IF EXISTS")
        stmts.append(f"CREATE PROPERTY {vtype}.{prop} IF NOT EXISTS {ptype}")
    # An indexed property declared with another type cannot be redeclared
    # without dropping and rebuilding its index — a full reindex on a large
    # database — so it is only reported.
    for (vtype, prop), ptype in _PROPERTY_TYPES.items():
        live = declared_type(vtype, prop)
        if live is not None and live != ptype:
            log.warning("%s.%s is declared %s on the server, %s in the code; it is indexed, "
                        "so the bootstrap leaves it — redeclare it by hand (drop the index, "
                        "the property, then init-schema)", vtype, prop, live, ptype)
    # The rules (see `constraints`): a declaration for the strings nothing else
    # declares, then one ALTER per rule the catalog does not already hold.
    for vtype, prop in _RULED_STRINGS + string_properties():
        if not has_property(vtype, prop):
            stmts.append(f"CREATE PROPERTY {vtype}.{prop} IF NOT EXISTS STRING")
    for vtype, prop, attr, value in constraints():
        live = (rules or {}).get(f"{vtype}.{prop}", {}).get(_RULE_KEYS[attr]) if rules is not None else None
        if rules is not None and _same_rule(live, value):
            continue
        stmts.append(_rule_sql(vtype, prop, attr, value))
    return stmts


def _existing_schema() -> tuple[set, set, dict, dict] | None:
    """(type names, index names, {"Type.property": declared type},
    {"Type.property": {rule key: value}}) from the server's catalog, or None
    when it cannot be read — then the full idempotent DDL runs, as it always
    did."""
    types = run_sql("SELECT name, properties FROM schema:types")
    indexes = run_sql("SELECT name FROM schema:indexes")
    if not isinstance(types, list) or not isinstance(indexes, list):
        return None
    declared: dict = {}
    rules: dict = {}
    for r in types:
        if not isinstance(r, dict):
            continue
        for p in r.get("properties") or []:
            name = p.get("name") if isinstance(p, dict) else p
            if not name:
                continue
            key = f"{r.get('name')}.{name}"
            declared[key] = p.get("type") if isinstance(p, dict) else None
            if isinstance(p, dict):
                rules[key] = {k: p[k] for k in _RULE_KEYS.values() if p.get(k) is not None}
    return ({r.get("name") for r in types if isinstance(r, dict)},
            {r.get("name") for r in indexes if isinstance(r, dict)},
            declared, rules)


def _fulltext_index_names(vtype: str, prop: str) -> list[str]:
    """Every index name on `vtype.<prop>` — the logical `Type[prop]` name AND the
    per-bucket physical names ArcadeDB auto-generates (`Type_<bucket>_<id>`). Used by
    the hard rebuild to drop a stuck FULL_TEXT index completely. `search_text` carries
    only the FULL_TEXT index, so matching on the property alone is unambiguous."""
    names: list[str] = []
    try:
        for row in run_sql("SELECT name, properties FROM schema:indexes"):
            r = dict(row)
            props = r.get("properties")
            name = r.get("name") or ""
            if props == [[prop]] and (name == f"{vtype}[{prop}]"
                                      or name.startswith(f"{vtype}_")):
                names.append(name)
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        log.warning("could not list indexes for %s.%s: %s", vtype, prop, exc)
    return names


def rebuild_fulltext_indexes(timeout: float = 3600, hard: bool = False) -> dict:
    """Repopulate the FULL_TEXT search indexes and return {"ok", "failed"}.

    A `--bulk-load` leaves these incomplete: the load drops them (per-insert FULL_TEXT
    maintenance is slow and, when a load is interrupted, unreliable), and afterwards
    ``ensure_indexes()`` only re-`CREATE`s them — `CREATE INDEX IF NOT EXISTS` is a
    no-op the moment the index exists, so it never backfills. An explicit `REBUILD`
    repopulates from the final data in one pass, which is what /search depends on.
    Runs with a long timeout (REBUILD is synchronous, minutes on millions of rows);
    a bulk load already talks to ArcadeDB directly (`--db-url`), bypassing any proxy
    read-timeout ceiling.

    `hard=True` first **drops** the FULL_TEXT index entirely (by every physical + logical
    name) and re-`CREATE`s it before the `REBUILD`. A plain `REBUILD` rebuilds the
    existing index structure in place; if that structure is itself corrupted/stuck — as
    happens when a past `CREATE`/`REBUILD` was cut off by a proxy read-timeout mid-flight,
    leaving a half-built Lucene index that later `REBUILD`s report "ok" on yet never
    repopulate — only a full drop + fresh `CREATE` (which reindexes all rows) recovers it.
    Run the hard path directly against ArcadeDB (`--db-url http://localhost:2480`) so it
    isn't cut off by the same proxy timeout again.
    """
    ok: list[str] = []
    failed: list[dict] = []
    for vtype, prop in _FULLTEXT_INDEXES:
        name = f"{vtype}[{prop}]"
        try:
            if hard:
                for idx in _fulltext_index_names(vtype, prop):
                    run_sql(f"DROP INDEX `{idx}` IF EXISTS", timeout=timeout)
                    log.info("hard rebuild: dropped %s", idx)
                # CREATE over existing rows indexes every one of them — that IS
                # the rebuild. A REBUILD on top re-indexed the same rows a second
                # time (14M entities: 25 min for the CREATE, 25 more for nothing).
                run_sql(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) FULL_TEXT",
                        timeout=timeout)
                ok.append(name)
                continue
            try:
                run_sql(f"REBUILD INDEX `{name}`", timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                if "not found" not in str(exc).lower():
                    raise
                # A bulk load drops the FULL_TEXT index and relies on the schema
                # bootstrap to re-create it; when that bootstrap timed out during
                # the flush (the full PSC load did), there is nothing to rebuild
                # — and /search stays dark until someone notices. Create it;
                # the CREATE indexes the existing rows, so no REBUILD after.
                log.info("FULL_TEXT index %s is missing — creating it", name)
                run_sql(f"CREATE INDEX IF NOT EXISTS ON {vtype} ({prop}) FULL_TEXT",
                        timeout=timeout)
            ok.append(name)
        except Exception as exc:  # noqa: BLE001 - best-effort maintenance
            log.warning("FULL_TEXT rebuild failed (%s): %s", name, exc)
            failed.append({"index": name, "error": str(exc)})
    log.info("FULL_TEXT rebuild (hard=%s): %d ok, %d failed", hard, len(ok), len(failed))
    return {"ok": ok, "failed": failed}


def ensure_indexes() -> dict:
    """
    Create the types/properties/indexes best-effort. Returns a summary:
    {"ok": [stmt, ...], "failed": [{"stmt", "error"}, ...], "skipped": bool}.
    """
    ok: list[str] = []
    failed: list[dict] = []
    try:
        existing = _existing_schema()
    except ConnectionError as exc:
        log.warning("Schema bootstrap skipped — ArcadeDB unreachable: %s", exc)
        return {"ok": ok, "failed": failed, "skipped": True}
    except Exception as exc:  # noqa: BLE001 - catalog unreadable: fall back to full DDL
        log.warning("Schema catalog unreadable (%s) — issuing the full DDL", exc)
        existing = None
    stmts = _statements(existing)
    if existing is not None and not stmts:
        log.info("Schema complete — no DDL issued")
        return {"ok": ok, "failed": failed, "skipped": False, "complete": True}
    # The DDL goes to the server in scripts of `_SCRIPT_SIZE` statements, not
    # one request each: a fresh database takes 520 statements (2026-10-25),
    # which is 5.5 s of HTTP round trips one by one and 0.4 s batched — the
    # difference between an integration suite of 20 minutes and one of a few,
    # and a faster API start. Every statement is idempotent (IF NOT EXISTS,
    # IF EXISTS, ALTER to the same value), so a script that fails is simply
    # re-run statement by statement, which keeps the per-statement report:
    # the one statement the server refuses is named, the rest still apply.
    for chunk in _chunks(stmts, _SCRIPT_SIZE):
        try:
            run_sqlscript(_script(chunk))
            ok.extend(chunk)
            continue
        except ConnectionError as exc:
            log.warning("Schema bootstrap skipped — ArcadeDB unreachable: %s", exc)
            return {"ok": ok, "failed": failed, "skipped": True}
        except Exception as exc:  # noqa: BLE001 - one of the chunk's statements is refused: find it
            log.info("Schema script of %d statements refused (%s) — re-issuing one by one", len(chunk), exc)
        for stmt in chunk:
            try:
                run_sql(stmt)
                ok.append(stmt)
            except ConnectionError as exc:
                # DB not reachable — don't spam a warning per statement.
                log.warning("Schema bootstrap skipped — ArcadeDB unreachable: %s", exc)
                return {"ok": ok, "failed": failed, "skipped": True}
            except Exception as exc:  # noqa: BLE001 - best-effort DDL
                log.warning("Schema statement failed (%s): %s", stmt, exc)
                failed.append({"stmt": stmt, "error": str(exc)})
    log.info("Schema bootstrap complete: %d applied, %d failed", len(ok), len(failed))
    return {"ok": ok, "failed": failed, "skipped": False}


#: Statements per bootstrap script. 60 of 520 took 0.4 s in nine requests on
#: 26.10.1; the chunking also bounds what a refused script re-issues singly.
_SCRIPT_SIZE = 60


def _chunks(stmts: list[str], size: int) -> list[list[str]]:
    return [stmts[i:i + size] for i in range(0, len(stmts), size)]


def _script(stmts: list[str]) -> str:
    """One ArcadeDB SQL script: the statements terminated and separated by
    ';' on their own lines — no statement of ours contains a ';'."""
    return ";\n".join(stmts) + ";"


def _statements_of(script: str) -> list[str]:
    """The inverse of `_script` (the test double splits a script with it)."""
    return [s[:-1] if s.endswith(";") else s for s in script.split(";\n") if s.strip()]
