# Data model

Owlgraph stores an ownership graph in ArcadeDB. Nodes are vertices; ownership
and roles are edges. Location is **not** a node — an entity carries its own HQ
(`hq_address`, `hq_city`, `hq_country`, `hq_lat`, `hq_lng`, `hq_locations[]`) and
its registered address (`address`, `reg_lat`, `reg_lng`), so the map reads one
record instead of traversing. Both are geocoded, because where a company is run
and where it is registered are different places and the map can show either.
**One registry owns the shape** (`app/addresses.py`, since 2026-10-26): each kind
of place — `registered`, `headquarters` — stores the same parts under its prefix
(readable address, matching key where the kind keeps one, street, city, postcode,
country where the kind has its own, pin and precision), and every importer, the
SEC writer, the geocoder's passes and the schema ledger read
the field names from it (`addresses.parts`, `fields`, `read`, `fill_clause`). A
third kind of place is one registry entry. Two names do not follow the prefix and
are stated there: the registered office's `address` and `address_normalized`; the
registered office has no country field because the entity's `country` is it.

## Every property is declared

Since 2026-10-25 the schema bootstrap declares **every** property a writer sets, the
strings included (`app/db/schema.py`: `_STRING_PROPERTIES` for the plain strings, the
typed, indexed and ruled lists for the rest; `schema.all_properties()` is the union). ArcadeDB
accepts a property nobody declared — there is no strict mode — so this is not a gate but a
**ledger**: the catalog (`schema:types`) then *is* the model, and `manage.py schema-audit`
reads a sample of every type and names each stored property the ledger does not know. That
is the shape a typo'd writer takes (`countrry`), or a field added to a writer and not to the
schema, or the null-valued leftover of a renamed field (dev carried `founded_date` keys with
no value after the rename to `incorporated_on`). A conformance test holds the writers'
own property lists (`edge_schema.OWNS_PROPS`, `ROLE_PROPS`, `RELATED_TO_PROPS`, `MEMBER_OF_PROPS`, the claim
property bag, the StakeHistory and IndustryCode rows) against the ledger, so an edge or
claim property cannot be added without declaring it.

## Property types

Every number, flag and list a writer sets is **declared** on its type
(`app/db/schema.py`, `_TYPED_PROPERTIES`, since 2026-10-24): `stake_percent`,
`voting_power_pct`, `value_usd`, `revenue` and the coordinates are DOUBLE, and so are
`shares`, `shares_outstanding` and `voting_shares` — a Form 4 reports fractional shares
(562,701.427 sat on a dev edge and ArcadeDB refused a LONG over it), and a double holds
every whole count a filing states exactly; `founded`, `employees`, `credibility_score`,
`position` INTEGER;
`is_nominee`, `active`, `stale`, `shortcut`, `also_ultimate`, `enabled` BOOLEAN;
`aliases`, `other_names`, `former_names`, `gleif_former_names`, `gleif_trading_names`, `interest_types` and the other lists LIST.
ArcadeDB then coerces every write (an int into `stake_percent` is stored `20.0`, a
numeric string into `employees` is parsed) and refuses one it cannot convert.
Schemaless, a value took whatever type the writer's JSON carried: `stake_percent` sat
as DOUBLE on 974 edges and INTEGER on 20, `shares` as DOUBLE, INTEGER and LONG side by
side. **Dates stay STRING** on purpose — the writers keep a source's precision
(`2019-00-00` is a Wikidata year, `1951-08` a UK PSC month of birth), which no DATE
type holds, and ISO strings compare correctly as strings. A declaration never touches
the rows already stored: it holds for new writes, and a fresh import is typed throughout.

The bootstrap sends its DDL to the server in **scripts of 60 statements**, not one request
each: a fresh database takes 520 statements (2026-10-25), which is 5.5 s of HTTP round trips
one by one and 0.4 s batched — the integration suite creates a database per test, and that
difference was 20 minutes of CI. Every statement is idempotent, so a script the server
refuses is re-issued statement by statement and the one statement refused is still named in
`init-schema`'s report. A declaration the server holds with **another type** than the code's
is dropped and declared again by the schema bootstrap (`init-schema`, every API start): ArcadeDB has
no `ALTER PROPERTY … TYPE`, and `DROP PROPERTY` removes only the declaration, never a
value. The case that taught this: `shares_outstanding` and `voting_shares` were LONG for
a day (2026-10-24) before a Form 4's fractional shares made them DOUBLE, and the deploy
that followed left the LONG declarations standing — the bootstrap only ever added what
was missing — so the rows rewritten as doubles were coerced back to LONG. An *indexed* property declared with
another type is only logged: redeclaring it would mean dropping and rebuilding its
index, which is a full reindex on a large database and a decision to take by hand.

## Rules the database enforces

Beyond a type, a property can carry a rule, and since 2026-10-25 the bootstrap declares
them (`app/db/schema.py`, `constraints()`, as `ALTER PROPERTY`): an `Entity`, `Person`
or `Source` cannot exist without its `id`, a `Claim` not without `claim_key`, `kind`,
`from_id`, `to_id` and `source_id` (mandatory, not null — a claim *is* what one source
says; `record_claim` had always dropped a sourceless one, and no database held any);
`Entity.type`, `Claim.kind`, `OWNS.ownership_type` and `read_from` (on OWNS, Claim and
HAS_ROLE) are restricted to their vocabularies — built from the enums the code validates
against (`EntityType`, `OwnershipType`, `READ_GRADES`), so a new value is added once;
`country` and `hq_country` match `^[A-Z]{2}$`; **every date** matches
`^[0-9]{4}(-[0-9]{2}){0,2}$` and **every timestamp**
`YYYY-MM-DDTHH:MM[:SS[.ffffff]]` with an offset (`+00:00`, or `Z`; seconds optional because
ArcadeDB parses a Cypher `…T00:00:00Z` into a datetime and stores it back as `…T00:00Z`) — the shape, never
the calendar: a Wikidata year stays `2019-00-00`, a PSC birth month `1965-04`, but
"4 March 2019", "03/04/2019", a timestamp in a date field and a naive timestamp are
refused, because the readers compare dates as text and any other notation sorts into
the wrong place (`_DATES` / `_TIMESTAMPS` list the fields; a new one is added there);
`stake_percent`, `voting_power_pct` and the StakeHistory percentages lie between 0 and
100; **every flag that means "not asserted" defaults to `false`** — `OWNS.stale`,
`shortcut`, `also_ultimate`, `Entity.is_nominee`, `Entity.on_demand_scraped`,
`Person.on_demand_scraped` (`schema.FLAGS`; since ArcadeDB 26.10.1 a null compares as
false and drops out of every filter, so each reader wrapped the flag in COALESCE; the
voting groups, 8,177 shortcut and 8,194 ultimate flags and every person's on-demand flag
were null on dev on 2026-10-25). `Entity.active` is the one
flag kept three-valued on purpose: null means no register has said whether the company
still exists. The server refuses a write that breaks a rule
with a clear message ("does not match the regular expression", "is greater than 100",
"is mandatory"), which turns a quietly wrong row into a failed scrape that the run log
shows. A rule leaves a null alone (`country` unknown is allowed; `notnull` is the one
rule that does not), a default applies to new rows only, and ArcadeDB **refuses to
declare a rule while any stored row breaks it** — `init-schema`'s `failed` list names the rule that still waits. `name` carries no rule on
purpose: a GLEIF relationship record creates a placeholder `lei:` node that a later
import names. A pattern travels inside an SQL string literal, so it is written without
backslashes (`[0-9]`, `[.]`).

## Nodes

| Label | Key properties |
|---|---|
| `Entity` | `id` (**the register's key when the writer knows one**: `lei:{LEI}` from GLEIF (also when GLEIF writes a company Wikidata pointed at — Wikidata writes no hard id since 2026-10-29, `app.scraper.register_pointer`), `gb-coh:{number}` and `chpsc:…` from Companies House, `sec:{CIK}` for one SEC creates with a CIK, `wd:{QID}` for a node Wikidata creates — precedence LEI > CIK > QID, a register's key before a knowledge base's, and a node that learns a better one is re-keyed whoever wrote first (since 2026-10-30; see "A node that learns its hard id later"), always the 10-digit form (`mapper.normalize_cik`, which every writer and lookup applies — 84 dev entities held a bare CIK until 2026-10-26, so the CIK lookup missed their padded twins) — a uuid only for a name-only node, an Exhibit 21 row or a 13D party without a CIK, which has nothing to key on (`app.node_ids`, since 2026-10-26; before, every per-company scrape minted a uuid, which a rebuild re-minted, breaking every shared link, pin and flag that named the node). The lookup before a creation resolves by the hard ids, so the id never decides which node a writer finds), `name`, `name_normalized`, `type` (company/brand/holding/government/foundation/fund/nonprofit — inferred from Wikidata P31 instance-of and GLEIF legal form; a 13D voting group is **not** an entity type since 2026-10-26, it is the `VotingGroup` vertex below, and `company` when neither says otherwise — the GLEIF bulk writer fills the field only where it is empty and defaults it, so no entity is typeless; **`government` covers the organs of a state**, not just its agencies — cabinet, executive branch, legislature, sovereign state — because P31 files a national cabinet under none of the agency classes and the unmatched default is `company`, which labelled the State Council of the PRC a company. A sovereign wealth fund still classifies as `fund`: the table is ordered most-specific-first and `fund` is checked before `government`), `country`, `countries` (every country the sources name, **containing `country` whenever the list exists** — the GLEIF bulk writer sets only `country`, so 5,839 of 6,138 dev entities have no list at all and a reader that prefers the list falls back to the scalar; 17 entities carried an empty list beside a filled `country` until the writers started folding it in on 2026-10-25; **absent when no country is known at all** — never `[]`, the writers since the same day), `founded` (the **year the business began** — Wikidata's inception; a register never writes it), `incorporated_on` (the **legal entity's registration date** as the register states it — GLEIF `EntityCreationDate`, Companies House `IncorporationDate`, OpenCorporates' incorporation date; only ever moved earlier, never blanked; the Details row "Incorporated" and what time travel hides a node before. Was `founded_date` until 2026-10-24, when the registers also wrote its year into `founded`, so Barclays PLC read 1690 and 1896 under one name), `dissolved_on` (**the day the legal entity ceased to exist**, as a source states it — Wikidata P576 at the statement's precision, GLEIF `EntityExpirationDate` (rarely filled), Companies House `DissolutionDate` (empty in the live-companies snapshot); fill-only, never blanked; the Details row "Dissolved", the "† YEAR" mark after the name, the timeline's last entry, and what time travel hides a node after. **Nothing of a dissolved company stays open** — `app.dissolution` closes its holdings, the holdings in it and its seats on that day with `until_reason = dissolution`, founders and edges dated after it excepted; the Wikidata writer applies it when the date arrives, every scrape scope to the companies it touched or wrote an edge onto (an edge written onto a company already known dissolved, 2026-10-28; a company the runner only resolved, like an Exhibit 21 parent, since 2026-10-29), `manage.py heal-ends-at-dissolution` for a database written before or loaded in bulk. Until 2026-10-25 nothing read an end: a dissolved company read as live), `company_status` (Companies House's `CompanyStatus` as the register words it — "Active", "Liquidation", "Active - Proposal to Strike off", "In Administration", …; the panel's Status row; the other registers state only `active`), `revenue`, `employees` (+ `employees_as_of` year, from Wikidata P1128), `wikidata_id`, `sec_cik`, `lei_id`, `lei_source_url` (the filer's own EDGAR filing that states the node's LEI, where that LEI joined the node to an EDGAR CIK it carried no other id in common with — `app.scraper.sec_lei`, 2026-10-27; absent otherwise), `lei_source` (what joined the node's LEI and CIK: "filing" for that document, else the registers' evidence — "name+place", "name+country+address@2021-07-16", … — `app.scraper.legal_identity`, docs/deduplication.md; absent otherwise), `gleif_last_update` (the day GLEIF last updated the record, `LastUpdateDate`; for a LAPSED record the day its data was last true), `companies_house_id`, `address` (human-readable legal address for display — GLEIF's `LegalAddress`, the Companies House registered office; the panel's "Registered address" row and the registered pin's fallback when the parts are missing), `address_normalized` (its matching key — lowercased, punctuation stripped, `mapper.normalize_address` — written by every importer from its readable address and indexed as the same-name dedup's corroborator; until 2026-10-25 the field was `registered_address`, which GLEIF filled with this key and the Companies House importers with the readable address, so a UK company never corroborated its GLEIF twin), `legal_form_code` (the ISO 20275 ELF code as GLEIF states it, beside the resolved `legal_form` name — the names come in 171 spellings on dev), `legal_form` (ISO 20275 ELF name, e.g. "Private Limited Company" — resolved from the LEI-CDF ELF code via the bundled GLEIF ELF list), `registration_authority` + `registration_authority_code` + `registration_number` (gleif.org's "Registered At" — register name resolved from the RA code via the bundled GLEIF RA list, plus the entity's id there; also stored for corporate PSCs and OpenCorporates lookups), `country_registered` (a corporate PSC's stated country **only when it resolves to no ISO code** — "Not Specified/Other", an ambiguous "Korea" — kept as the filer wrote it; `country` stays empty rather than carrying words, since every filter and flag reads it as a code), `cusip` (13F info-table join key, harvested from SEC schedules — a MATCHING aid, not a dedup id: one company has one CUSIP per share class), `former_register_ids[]` (register pairs the entity previously carried — recovered from GLEIF snapshot archives, maintained by the daily delta; join the hard-id dedup, see docs/deduplication.md), `register_id` (**hard identifier** `"{RA code}:{number}"`, e.g. `RA000585:07524813` — joins LEI/CH/CIK/QID in every dedup and resolution path; see docs/deduplication.md for the placeholder-code exclusions and the GB double key), `jurisdiction_code` (ISO 3166-2 legal jurisdiction where a source gives one, e.g. `US-DE` — see *How countries are represented* below; sparse, ~1% of GLEIF records), `hq_lat`/`hq_lng`/`hq_city`/`hq_country`/`hq_street`/`hq_postcode` (where it is **run**), `reg_lat`/`reg_lng`/`reg_geo_precision`/`reg_street`/`reg_city`/`reg_postcode` (where it is **registered** — geocoded from `address`; the two differ exactly where it matters, e.g. an agent's office on Grand Cayman versus a London headquarters, and the map's Registered/Headquarters switch draws one or the other; the `_street`/`_city`/`_postcode` parts are kept **as the source gave them** so geocoding is a structured query rather than an attempt to re-parse an assembled string — every country writes an address differently, and picking the city out of a comma-separated line is guesswork), `source_id`, `source_statement_ids[]` (BODS statement ids that declared the entity — accumulated for id-less parties collapsed under one name key, so per-statement provenance survives the collapse), `website` (the company's official site — Wikidata P856 / SEC submissions `website`+`investorWebsite`, http(s)-validated, fill-if-missing across sources; display-only: unindexed and never a search token), `logo_url` (direct `upload.wikimedia.org` thumbnail from Wikidata P154 logo, P18 image as fallback — resolved server-side because Commons' `Special:FilePath` redirect broke on mobile networks; fill-if-missing, display-only), `aliases[]` (other names — Wikidata skos:altLabel, SEC EDGAR `formerNames`, Companies House previous names), `former_names[]` (the renames with their dates: `"NAME|YYYY-MM-DD"`, the date being the last day the old name applied — Companies House's change-of-name date, EDGAR's `to`; `"NAME|"` when a source knows the name but not the date; the `"City|CC"` string convention of `hq_locations`, split on the LAST pipe; module `app.former_names`. Unioned by (name, date) on an entity merge, a date filling in an undated twin; `aliases` keeps the plain names for search, this list feeds the timeline's "Renamed" entries — see `/relationships/history` — and gives each name its time span: lookups rank an ended name below a name in force, and the Wikidata writer refuses an item that names the node only by an ended one, docs/sec_edgar_scraper.md "Former names"), `other_names[]` (the register's own other CURRENT names: GLEIF alternative-language legal names and transliterations — what makes 삼성전자(주) findable as "Samsung Electronics"; GLEIF-owned and folded into `search_text`; its previous legal names are in `gleif_former_names[]` (`app.former_names` entries, dated where GLEIF allows, read with `former_names` through `former_names.of_node`) and its trading names in `gleif_trading_names[]` (searchable, never a legal name) since findings item 16a, kept apart from the Wikidata/SEC-owned `aliases`), `search_text` (FULL_TEXT-indexed: name + description + aliases + registration number), `is_nominee` (name-detected nominee/custodian — holder of record, not a beneficial owner; `manage.py flag-nominees` backfills existing), `validation_sources` (GLEIF's own statement of how far it checked the record — `FULLY_CORROBORATED` / `PARTIALLY_CORROBORATED` / `ENTITY_SUPPLIED_ONLY` / `PENDING`; scales `name_credibility`, see *How far GLEIF checked a record* below), `lei_registration_date` (the day the LEI was first issued, GLEIF `InitialRegistrationDate` — kept because relationship records reuse it as the relationship start, see `since_basis = gleif_registration_day`), `no_direct_parent_reason` / `no_ultimate_parent_reason` (+ `_reference`) (why the company reports no parent, from GLEIF's reporting-exceptions file — see *Why a company reports no parent* below), `active` (three-valued on purpose: false once a register says the entity ceased — GLEIF `EntityStatus` INACTIVE, 254,659 of 3.45M records on 2026-10-06, a Companies House status "Dissolved", a stated `dissolved_on` — true when a register lists it as live (GLEIF ACTIVE, the Companies House snapshot of live companies), **null when no register has said**: a knowledge base asserts neither; never deleted) + `gleif_registration_status` (the LEI's own registration status as stated: ISSUED, LAPSED — 1.2M, a third — RETIRED, …; both stamped by the full load and the delta), `created_at` (when the node entered the graph — stamped by every creation site since 2026-10-24, kept by an UPSERT, the earlier of the two kept by a merge) |
| `GeoCache` | `query` (the cleaned address, UNIQUE), `lat`, `lng`, `precision`, `checked_at` — an address→coordinate cache so a shared registered-agent building is geocoded once rather than once per company registered there (24 dev companies share one Wilmington address). Misses are cached too, with the date, and retried after 30 days: re-asking Nominatim about an address OpenStreetMap does not have is the most wasteful thing the geocoder can do, and a permanent "no" would be a lie. Mirrored to the shared object store (`geocode/<sha256 of the address>`, one JSON object each) so the answers — misses included — survive a database rebuild and are shared by every environment; a table miss reads through to the bucket and refills the table. Both lookup paths use it: free-text addresses keyed by the cleaned string, **structured** ones (street / city / postcode / country in parts, the preferred path) keyed `structured:<part>=<value>|…` in a fixed order — until 2026-10-05 the structured path had only an in-process dict, so every rebuild re-asked Nominatim for each distinct address at one a second (2,740 lookups, 36 minutes on the test import). |
| `ScrapeMiss` | `key` (normalised company name + `|` + ISO-2 country, or an empty country for an unrestricted search — UNIQUE), `missed_at` — an on-demand search that found nothing. The freshness gate protects the sources by looking at the *company* (`last_scraped_at`, `scrape_depth`), and a search that found nothing has no company to hang that on, so every repeat used to ask every source the same hopeless question again. Honoured for `SCRAPER_ONDEMAND_COOLDOWN_HOURS`, cleared by a later successful scrape of the same name+country, and expired rows are deleted when next looked up. The country is part of the key because France having no "Alphabet" says nothing about Germany. |
| `SearchDemand` | `key` (normalised query + `|` + ISO-2 country, UNIQUE), `query`, `country`, `searches`, `zero_results`, `selected`, `first_seen`, `last_seen` — one row per question asked, never per person asking. `zero_results` is the useful one: demand the graph could not answer, in demand order. Pruned after 365 idle days |
| `UsageCounter` | `key` (an allow-listed event name, UNIQUE), `count`, `first_seen`, `last_seen` — feature usage and clicked result positions (`result.rank.3`). The key space is closed because a public endpoint feeds it |
| `EndpointStat` | `key` (`METHOD route-template status-class latency-band`, UNIQUE), `count`, `last_seen` — request health. Keyed on the route **template**, never the path, so no record id is ever a key |
| `Person` | `id` (`wd:{QID}` from Wikidata, `sec:{CIK}` from an SEC filing with a CIK, `chpsc:…` from Companies House, a uuid for a name-only person — `app.node_ids`, as for an Entity), `full_name`, `first_name`, `last_name`, `aliases[]` (`alias` until 2026-10-25 — one name for the one list on both node types), `nationalities[]` (**ISO-3166-1 alpha-2** codes, e.g. `GB` — Companies House records a demonym like "British", normalised on import; a value that cannot be recognised is kept verbatim rather than blanked. The one field: a scalar `nationality` sat beside it until 2026-10-25 — the PSC writer filled only the scalar, the SEC writer wrote an empty string into it, the Wikidata path filled the list and copied its first entry, so 204 of 371 dev persons had an empty list beside the scalar and every reader needed a fallback. **Absent when no source stated one** — never `[]`, as a scalar is never `''`; 196 dev persons carried an empty list until 2026-10-25), `birth_date` (**month and year only** where the source publishes no more, which is the case for UK PSC), `birth_place`, `description` (what a knowledge base says the person is — **absent when no source described them, never `''`**: the SEC and PSC person writers and the Wikidata path wrote an empty string until 2026-10-25, 1,378 dev persons), `wikidata_id`, `sec_cik`, `created_at`, `last_scraped_at` (when a source last wrote the person — every person writer stamps it since 2026-10-25; until then only the person scrape did, so no company-scraped person had one), `wikipedia_url` (the English Wikipedia article — Wikidata's sitelink, written by the person scrape; the panel's link and the portrait fallback when the item has no P18), `image_url` (the portrait, Wikidata P18 as a direct Commons thumbnail like `Entity.logo_url` — the pointer `GET /media/person/{id}` resolves and licence-checks), `created_at` (as on `Entity`) |
| `VotingGroup` | `id`, `name` ("Voting group · N parties" — what it is and how big; the panel says whose shares), `name_normalized` (the matching key carries the issuer, `voting group <issuer>`, so two blocs of one size never share it), `search_text` (name + the issuer's name: "anheuser voting" finds it), `description`, `type` (always `voting_group` — the API's node kind, which the client draws and labels by; the vertex type says the same thing to the database), `member_keys[]` (the roster, `cik\|name` per party — the group's identity: a filing with ≥ 2 shared members and ≥ 50 % of the smaller roster is the same group, see *Voting groups* below), `source_id`, `created_at`, `last_scraped_at`. **Its own vertex type since 2026-10-26**: until then a group was an Entity of type `voting_group`, which every query that assumes an entity is a legal person had to fence off — eight did (`anchors.not_a_voting_group`, now gone), the rest forgot: the 38 groups on dev were the only entities without a nominee flag, were counted in the entity totals and offered to every pass that walks the entities. The group opens through the entity routes (`/search/entity/{id}/full-profile`, `/relationships/history`, the tree) by two indexed point reads, never an unlabelled scan |
| `Source` | `id` (**stable**, the same on every instance — `gleif`, `sec-edgar`, `uk-psc`, `wikidata`, `opencorporates`; `peer:<slug>` for a federation peer; the slug of the name for a manual one. A random uuid per database until 2026-10-24, which made a claim's `source_id` meaningless outside the database that minted it), `name`, `url`, `type` (register / knowledge_base / peer / …), `credibility_score`, and for a catalogue source its switches: `registry_key` (the catalogue key, `sec_edgar`, `bods_gleif`, … — the toggle name in the API), `enabled`, `data_mode` (`full` / `claims_only`), `kind`, `description`; for peers also `verified`, `key_id`. One node per source — the `ScraperSource` toggle node it used to sit beside is folded in |
| `User` | `id`, `email`, `password_hash`, `role` (admin/contributor/viewer), `language` (UI language at registration — emails to this person are written in it) |
| `MergeLog` | `id`, `kind` (`person`/`entity`), `keep_id`, `keep_name`, `dup_id`, `dup_name`, `at`, `count` — merge history, deduped by (keep, dup_name) so a re-scraped duplicate bumps `count` rather than adding a row. `kind` keeps the two logs apart |
| `Peer` | `id`, `name`, `base_url`, `credibility_score`, `auth_token`, `public_key`, `enabled` — a trusted federation peer |
| `Claim` | `claim_key` (UNIQUE), `kind` (owns/role/succession), `from_id`, `to_id`, `source_id`, `stake_percent`, `voting_power_pct`, `ownership_type`, `role`, `since`, `until`, `source_url`, `source_date`, `credibility_score`, `filing_type` (which KIND of record asserted it — "13F", "13G/A", "RR", "PSC", "10-Q XBRL", and on a seat claim "Form 4", "Form 3", "Form D" or, for a departure, "8-K"; the Sources panel shows it beside the source name. The SEC seat claims carried none until 2026-10-25 — 208 on dev; the next scrape of a company rewrites its claims with the form), `until_source_url` + `until_read_from` (a seat claim's departure, when another record than the appointment stated it: a close **updates** the source's own claim for the position — it keeps the appointment's `source_url`, `read_from` and `filing_type` and gains the departure's citation and grade — and a source that never asserted the seat gets a claim stating only the end. A re-read that brings no end and the same start — a Form 4 after an 8-K — keeps the end; a different start is a new spell and replaces it; `claims._END_KEPT`), `first_seen_at`, `last_seen_at` — **what one source asserts about one relationship**; see below. One claim per (kind, pair, source), with two exceptions keyed apart: a role claim carries its canonical role, and an owns claim read from an acquirer's XBRL business-combination facts (`filing_type` "10-K XBRL" / "10-Q XBRL") sits beside the same source's Exhibit 21 claim for the pair — the list says "held at year-end", the deal says "bought on this day for this much" |
| `ScrapeRun` | `id`, `source`, `target`, `status` (running/ok/failed/skipped), `started_at`, `finished_at` (once finished), `total`, `error` (only on a failure; on a `run-all` row, the sources that errored), `reason` (why an on-demand run happened: absent, stale, forced…), `entity_id` (the company the run resolved — every per-company runner sets it; a bulk run has none) — the scrape run log (capped). What is not known is absent, never `''`: the row was created with empty strings in `finished_at`, `error` and `entity_id` until 2026-10-25 |

## Relationships

| Pattern | Properties |
|---|---|
| `(Entity\|Person)-[:OWNS]->(Entity)` | `stake_percent` (economic holding; for SEC 13D/G re-divided by the issuer's newest stated total of the same class, see `denominator_date`), `shares` / `shares_outstanding` (the counts behind it), `denominator_date` (the filing date `shares_outstanding` comes from when it is newer than the holder's own filing — unset otherwise; for a Form 4 holding, the date of the issuer's cover whose per-class count the stake is of), `share_class` (the security the count is of — a Form 4 stake is a percent of that class's own count, never of another class's), `event_date` (the day a 13D/G cover states its numbers as of — the *date of event*, not the filing date `source_date`), `voting_power_pct` (voting rights — kept separate from the stake; from BODS `votingRights` interests and DEF 14A proxies), `interest_types[]` (BODS interest kinds behind the edge: shareholding/votingRights/appointmentOfBoard/…), `direct_or_indirect` (from GLEIF RR-CDF: `direct` = directly-consolidated parent, `indirect` = ultimate parent; also `direct` from an SEC Exhibit 21 that draws its tree, then with `structure_basis` = `ex21_indent` / `ex21_heading` / `ex21_column` / `ex21_stated` saying how the place was read — a stated GLEIF marker has none and is never overwritten by an inferred one), `also_ultimate` / `ultimate_since` / `ultimate_until` (see *One edge per pair* below), `shortcut` (set by `manage.py mark-shortcuts`: this indirect edge duplicates a path of **current** direct edges — an ended chain proves nothing. Edges marked `true` are excluded from the full-profile owners/subsidiaries lists **and their counts**, so the panel and the graph agree; `false` means unproven and it is kept), `ownership_type` (see the vocabulary below), `since`, `until` (+ `until_reason` — how the source says it ended: `gleif_inactive`, `gleif_dropped`, `ceased`, `withdrawn`; or `dissolution` when no source said so and either company's `dissolved_on` ended it, `app.dissolution`, then with `until_source_url` citing the record that stated the dissolution when the rule knows it), `source_id`, `source_url`, `source_date`. Free float / >100% conflicts aren't stored — the full-profile endpoint derives them from the disclosed stakes on read (`ownership` summary) |
| `(Person)-[:HAS_ROLE]->(Entity)` | `role`, `since`, `until`, `source_id`, `source_url`, `source_date` (the start when stated; an **undated** seat is dated by the scrape day — the source lists it as current then — so time travel shows it from that day rather than dimmed in every year). **One position, one seat at a time:** an assertion is about the seat whose period it touches — the same canonical role, open or a closed spell it falls into (`app.roles.periods_overlap`) — and the seat keeps the earlier of the two starts (`combined_start`; the finer of one date at two precisions). Two different days used to be two seats, so a Form 3 filed days after Wikidata's appointment day drew a second "Director" row (fixed 2026-10-25; the merges fold seats the same way). A second spell needs the first one ended. An end a source states later (a Wikidata end time, an OpenCorporates resignation) closes the open seat, but never one that began after it — the same guard as SEC departures. `until_source_url` cites the record that stated the end when it is not the one that drew the seat (an 8-K Item 5.02 after a Form 4; the seat keeps its own `source_url` and `source_date` — until 2026-10-25 the closing filing's replaced them). `until_reason` says why a seat ended when **no source said so**: `death` — **a seat ends no later than its holder's death** — or `dissolution` — **nothing of a dissolved company stays open** (`app.dissolution`, the same rule from the company's side: its seats end on `dissolved_on`, founders excepted). A person's death date (Wikidata P570) closes every open seat on that day, except a founder's (a founder is not a seat that ends) and except a seat the source dates after the death (its error, left open and reported); the person writer applies it when a death date arrives, the seat writers when they open a seat for someone already dead. The source's claim is left as stated: it named no end, and the claim is what the source said. Two dev directors sat on open boards years after their deaths before the rule (2026-10-25) |
| `(Entity\|Person)-[:MEMBER_OF]->(VotingGroup)` | `source_id`, `last_scraped_at` — a party of a 13D filing group pointing at the group that holds the bloc (`sec_writer._upsert_group_membership`; 244 on dev, 201 from entities and 43 from persons). Its own edge type since 2026-10-26 (RELATED_TO {relation: group_member} before); a merge carries it (`maintenance._migrate_membership`), one edge per group |
| `(Entity)-[:RELATED_TO]->(Entity)` | `relation` (the one value left is `affiliate`: two managers named on one 13F cover page, `runner._upsert_affiliate` — "reports 13F holdings alongside", never ownership), `source_id`, `source_url`, `source_date`, `last_scraped_at`. No person-to-person relation is drawn by anything |
| `(Person)-[:NOT_DUPLICATE]->(Person)`<br>`(Entity)-[:NOT_DUPLICATE]->(Entity)` | `at` — marks two nodes confirmed to be *different* (keep-separate). The entity dedup checks these **per pair**, not per group: a third same-named company must not drag a node someone explicitly separated into a destructive auto-merge |
| `(Entity)-[:DUAL_LISTED_WITH]->(Entity)` | links share classes of a dual-listed company |
| `(Entity)-[:SUCCEEDED_BY]->(Entity)` | corporate succession/rename, directed predecessor → successor (e.g. Twitter → X Corp.); from Wikidata P1366/P1365 (with `since` from the P585 qualifier) and from GLEIF LEI-CDF (MERGED/DUPLICATE → `SuccessorLEI`, keyed `lei:{LEI}`). `since`, `source_id`, `source_url`, `source_date` |

**Wikidata dates.** Owners (P127) and subsidiaries (P355) are read as **statements** with their start/end qualifiers (P580/P582): a current one is best rank, an ended one is kept as history even when it is not (a former owner beside a preferred current one), an open non-best one is left out as `wdt:` did. Several statements fold to the earliest start, ended only when every one has. Dates keep their precision (`wikibase:timePrecision`): a year is `YYYY-00-00`, a month `YYYY-MM-00` — Wikidata's own January 1st is not the source's day. The generic writer writes an ended statement as its own closed edge and combines the start of a current one; an undated assertion is dated by the scrape day. (Ownership is claims-only on this deployment, so the claims carry these dates; the edges do whenever the source draws.) Founding dates from scrapes (Wikidata `founded`, OpenCorporates' incorporation date) only move **earlier**, like the bulk importers'.


`until = null` means the relationship is currently active.  
`ownership_type` is a closed vocabulary, defined once as `OwnershipType` in
[`app/models/relationship.py`](../backend/app/models/relationship.py) and derived by
`derive_ownership_type` in [`app/scraper/mapper.py`](../backend/app/scraper/mapper.py):

| Value | Meaning |
|---|---|
| `full` | >= 99% — essentially wholly owned |
| `majority` | > 50% — outright control |
| `controlling` | >= 20% — significant blocking minority |
| `minority` | > 0% — passive stake |
| `unknown` | owner known, stake undisclosed — the most common case, and deliberately not guessed |

A value arriving from outside (the manual create endpoint, a moderator pin) is
validated against this set at the API boundary. `free_float` is
**not** a stored value: the widely-held remainder is derived on read as
`free_float_pct`, because nobody holds the free float.

Vertex/edge types and lookup indexes are created idempotently on startup and via
`python manage.py init-schema` (see the README's *Deployment → Schema & indexes*).

## Claims: per-source provenance

An `OWNS` edge holds **one** answer — the value traversals read. Several sources
routinely assert the same relationship with different numbers (GLEIF and
Companies House will disagree about a stake, and both are right about their own
register). Those used to be lost: the second writer overwrote the first, and the
Sources panel reconstructed attribution by guessing from which identifier fields
happened to be populated.

A `Claim` records what **one source** said. The edge is unchanged — still the
single, fast, current-best answer — and the claims sit beside it as the evidence:

```
(:Entity)-[:OWNS {stake_percent: 60}]->(:Entity)      <- traversals read this
(:Claim {kind:'owns', from_id, to_id, stake_percent: 60, source_id:'gleif'})
(:Claim {kind:'owns', from_id, to_id, stake_percent: 75, source_id:'ch-psc'})
```

- **Keyed** on `claim_key` = digest of (kind, from_id, to_id, source_id — and, for a role, the canonical role; for an ownership claim, the **SEC filing family**), UNIQUE.
  A source re-asserting the same relationship updates its own row, so re-imports
  are idempotent here even though the edges still need a dedup pass. SEC EDGAR
  speaks about one pair through several filing families, each its own statement:
  `insider` (Form 3/4/5, the holding of record), `13dg` (Schedule 13D/G, the
  beneficial stake the edge rule prefers), `13f`, `ex21`, `ex81`, `acquisition`
  (the acquirer's XBRL) — `claims.filing_family`, read off the filing type's text,
  so no other source's key changed. Since 2026-10-26 (the acquisition since
  2026-10-12); before, one claim per (pair, source) held whichever filing was read
  last, and the Sources panel showed one "SEC EDGAR" line where the register had
  made two or three statements. An amendment of the same family still updates its
  own claim. The stake history (`app.stake_history`) is kept per claim, so each
  family is its own series and a Form 4 number no longer "replaces" a 13G one. The parts
  are length-prefixed before hashing, so an id containing the separator cannot
  make two different claims collide.
- **Written by the edge writers themselves** — `_BatchWriter.owns/role/succeeded_by`
  for bulk imports and `record_claim` in the incremental scrapers — so an importer
  cannot record an edge and forget the evidence. This now includes **every** OWNS
  writer: the GLEIF delta, both PSC refresh paths (batched, same statement shape
  as their edge writes), federation imports (credibility = the peer's configured
  trust), and the manual API (skipped when the request names no `source_id` — a
  claim is one source's statement, and unsourced claims would all share a key).
- **Which claim wins**: the official tier (credibility ≥ 90), then a stated
  stake, then `credibility_score`, ties broken by the most recent `source_date`
  (`best_claim` in [`app/claims.py`](../backend/app/claims.py)). A register's
  "owns, amount undisclosed" deliberately beats a community source's number; among
  registers a stated stake beats a list that states none (the UK PSC's 75% over
  SEC's Exhibit 21).

### One edge per pair, whichever sources assert it

The pair's single active edge carries **every** source's contribution; the
incremental writers share it rather than drawing one each
([`app/scraper/owns_merge.py`](../backend/app/scraper/owns_merge.py)). Until
2026-09 each writer found only the edge it had drawn itself — SEC by `source_id`,
GLEIF by its marker, UK PSC by its appointment link — so a second source drew a
second edge: 129 pairs on the dev DB (128 GLEIF + SEC Exhibit 21), News Corp's
timeline listing Dow Jones twice, and the dedup after every GLEIF import deleting
the SEC edge (its "listed since 2013" with it) for the next SEC scrape to redraw.

| Part of the edge | Rule |
|---|---|
| **The answer** — stake, voting, type, share counts, `until`, source, link, date, credibility, filing type, `read_from`, `validation_sources` / `validation_documents` (how far the register checked the record behind it — see *How far GLEIF checked a relationship*) | One source's, moved as a unit (never one source's link with another's number). A source takes it over only when it **outranks** the holder — same order as `best_claim`, but a tie keeps the incumbent, so two sources cannot flip the edge nightly. Within **one** source the newer statement replaces the older (`supersedes`: same source, equal rank, later `source_date`, then `last_scraped_at`) — a register re-read says what the register says now; before 2026-10-18 a bulk re-import's twin lost to the old edge the dedup kept for its marker, and three PSC edges on dev still cited the 2016 snapshot |
| **`since` / `since_basis` / `since_source_url`** | Combined: the earliest date any source gives. A start that is not stated keeps its label: `first_listed` ("since 2013 or earlier", the oldest Exhibit 21 naming it), `amendment` (a 13D/G amendment's date — held by then, the start not seen), `register_start` (UK PSC notified on 2016-04-06, the register's first day), `register_first_year` (UK PSC notified inside the register's first year by a company incorporated before it — declared on its first confirmation statement, held by then; set after `ch-company-data` brings the incorporation date, see *UK PSC sourcing*), `first_reported` (the earliest 13F quarter a manager reported the holding in), `gleif_registration_day` (a GLEIF start that is just the child's LEI registration day and not its founding day — GLEIF records reuse that date: 27,874 of 260,250 on 2026-10-06, Barclays Bank PLC "since 2012-06-06", owned since 1985; set by the RR import, the daily delta and `gleif-rr-history`, the claim keeps the date as stated), `gleif_first_seen` (the oldest archived GLEIF golden copy listing the relationship — the archive begins 2018-02-09, so that date says only "at least since February 2018"; `gleif-rr-history`) or `newly_listed` ("first listed 2025": the list for the year before does not name it, nor any older one); a stated start on or before it replaces it. **For time travel every basis is a lower bound except `newly_listed`** (`owns_merge.STATED_BASES`, `started_by_clause`): before its date the edge is shown dimmed, not hidden. The source's **claim** carries the same listing date and keeps it when the list is re-read (a re-scrape rewrites the claim and states no start; it used to wipe the date) |
| **Structure** — `direct_or_indirect` (+ `structure_basis`), `also_ultimate`, `ultimate_*`, `interest_types`, `psc_self_link` | Stays on the edge whoever holds the answer; an inferred marker travels with its basis, a stated one never gains one |

A source that does not outrank the holder still records its claim, and does not
close or reopen the edge: GLEIF retiring a relationship SEC still lists, or a PSC
record vanishing from an edge an SEC 13D took over, closes only that source's
claim. A **ceased** PSC appointment keeps its own closed edge — history, not the
current holding. The generic writer (Wikidata, OpenCorporates) always shared the
pair's edge and never takes the answer over; those sources sit below the official
tier anyway. Bulk imports still `CREATE` blindly, and the post-import dedup now
**folds** a cross-source pair into its survivor before deleting the rest.
- **Read** by the Sources panel via `to_id` — everything asserted *about* an
  entity. Claims about the subsidiaries it owns carry `from_id` = that entity and
  are never selected, so the panel cannot flood with one row per subsidiary.

An entity's own record provenance is a different question, answered from its hard
identifiers (`_entity_own_source_rows`) — claims describe relationships, not nodes.

### Stake history — what a source stated before

A claim holds ONE stake, the latest its source stated, and so does the edge:
until 2026-10 every 13F quarter, 13D/G amendment and register re-read
overwrote the number before it, a past year showed today's stake, and nothing
could say when a holder became material. `StakeHistory` rows keep what was
overwritten ([`app/stake_history.py`](../backend/app/stake_history.py)):

```
(:StakeHistory {from_id, to_id, source_id, stake_percent: 6.1, previous_percent: 4.8,
                as_of: '2025-06-30', filing_type: '13F', source_url, recorded_at})
```

- **Written on a material change only.** Every claim write — `record_claim`, the
  bulk importer's batched UPSERT, the PSC refresh's — reads the stored claim
  first and writes a row when the new stake differs from the old AND one side
  is at or above **5 %** (the SEC 13D/G line: the holders who must disclose).
  Sub-5 % 13F drift never reaches the table; a crossing of 5 % always does.
- **The first change also records the statement it replaces.** A claim's first
  statement writes nothing — the claim holds it and most never change. When one
  does, the old stake with its own date goes in beside the new, so the series
  starts where the claim stood (dated by the source's LAST statement of that
  number — the claim's date moved with each restatement; before it, time travel
  falls back to the earliest row anyway). One row per change after that. The pre-read is
  one indexed query per batch; a re-import that restates everything writes
  nothing.
- **One statement per run.** A scrape that writes the same claim twice in one
  run states its LAST value, compared with the claim stored before the run. The
  SEC company scrape used to put a holder's 13D/G figure and then their Form 4
  figure into the one claim (Musk → Tesla: 19.9 % beneficially owned, then
  28.4 % of record), and every run recorded a flap; the Form 4 now yields to
  the 13D/G statement (sec_edgar_scraper, *Of record versus beneficial*) and
  this rule stays as the safety net. A runner wraps its write phase in
  `one_statement_per_run()`; `record_claim` defers inside it, the stored claim
  is read once at a key's first write, the rows are written when the run ends.
  The bulk importers batch anyway and state each claim once.
- **A statement dated before the one it replaces** (that 13G/A of 16 June
  taking over from a Form 4 count restated against an October total) writes no
  replaced-statement row: the series must not carry a later-dated number the
  claim no longer states. Its `previous_percent` still says what the claim held.
- **`as_of` is when the source stated it**: the filing's date of event, else
  its filing date; a 13D/G stake re-divided by a newer total of the class
  (`denominator_date` after the statement) is as of that total. `recorded_at`
  is when we saw it. `previous_percent` null marks the first kept statement.
- **Read** by time travel (`full-profile?as_of=` shows the stake the source
  stated on that day — the latest row on or before it, else the earliest, which
  is what the edge showed before the table existed; `stake_as_of` says which),
  by the timeline (**only the two crossings** — `stake_reached`, `stake_dropped`
  — never the drift between, so a company with six quarterly filers does not
  drown its real transitions), by `/relationships/stake-history` (the whole
  series behind a holding's row) and by the spreadsheet export (its own sheet).
- Rows carry the pair's ids and the source, not the claim key: a merge
  re-points them with the claims (`migrate_claims` → `stake_history.migrate`).
- Not read by the subsidiary tree: its edges show the current stake at any date.

### Industry classification — what a company does, in one tree

Every company's sector is shown in **NACE Rev. 2** — Eurostat's statistical
classification of economic activities, four levels: section > division > group
> class (C > 23 > 23.3 > 23.32 *Manufacture of bricks, tiles and construction
products, in baked clay*) — and beside it what each source actually said, in the
source's own scheme ([`app/industry.py`](../backend/app/industry.py)):

```
(:IndustryCode {key, entity_id, scheme: 'uk-sic', code: '23320', label, position: 1,
                nace_code: '23.32', source_id, source_url, source_date,
                first_seen_at, last_seen_at})
Entity.nace_code    the node the panel draws the tree from
Entity.uk_sic_codes the register's SIC cells as written (BasicCompanyData), the
                    statements' raw material
```

- **One statement per (entity, scheme, position)**, keyed by their hash and
  UPSERTed: the source's code and title as it gave them, the NACE node it maps
  to, and where and when it was said. A re-read restates; nothing is appended.
- **The schemes.** `us-sic` — the SIC code EDGAR assigns every filer
  (`sic`/`sicDescription` in the submissions document), written whenever the SEC
  scrape upserts an entity with a CIK: the target, its 13D/G investors, a
  holdings scrape's filer and subjects. `uk-sic` — Companies House's UK SIC 2007
  codes, up to four per company with the first the principal activity, from the
  BasicCompanyData snapshot: `ch-company-data` stores the cells on the entity and
  ends by turning them into statements dated by the snapshot
  (`industry.sync_uk_sic`). `manage.py backfill-industry` (and `POST
  /scraper/backfill-industry`, which the scrape scripts call after their passes)
  fills both for companies the sources already know — needed after every scrape
  run, not only once: a CIK that reaches a node any other way than the SEC
  scrape resolving that company by name (a Wikidata identifier, as on SpaceX and
  Nestlé; a holder in another company's filing) gets no code until it runs. The
  dev rebuild of 2026-10-26 left 173 such filers.
- **The mapping** lives in a bundle (`app/scraper/data/industry_nace.json`,
  regenerated by `scripts/build_industry_bundle.py`): the NACE tree with EN and
  DE labels (Eurostat's codelist, CC BY 4.0), the SEC's SIC list (public domain)
  and Companies House's SIC list (OGL). A UK subclass IS a NACE class plus a
  fifth digit (23320 → 23.32); three UK-only codes — 74990 non-trading, 98000
  residents' management, 99999 dormant — map to nothing and show only the
  register's wording. A US SIC code reaches NACE through the US Census
  crosswalks (SIC → NAICS 2002 → 2007 → 2012 → 2017 → ISIC Rev. 4) and the UN's
  ISIC → NACE table, each step one-to-many, so it lands at the **deepest node
  holding a majority of its paths**: a class for a tight industry (3711 motor
  vehicles → 29.10), a division or section for a catch-all (7370 computer
  programming, data processing → J). EDGAR's rounded group codes (3250) stand
  for every 1987 industry under their prefix. 414 of the SEC's 444 codes reach a
  node; the rest (blank checks, ADRs, foreign governments, "services NEC") show
  the SEC's title alone. Two ties are decided by hand in the builder, with
  their reasons.
- **`nace_code` on the entity** is the principal statement of the most exact
  scheme: a register's native code (`uk-sic`, position 1) before a crosswalked
  one (`us-sic`); a statement that maps to nothing yields to one that does.
  Indexed, so "every company in 23.32" is a lookup.
- **Read** by the full profile (`industry`: `nace_code`, the `tree` from the
  section down with both labels, the `statements` with their source names) —
  the panel merges every statement's branch into one tree (a shared ancestor
  drawn once, the principal first), hangs the source's own title under a
  branch where it says more than the NACE row, and lists each source's codes
  under the tree with a link to the register page. Rows carry
  the entity id, not a claim key; a merge re-keys them with the claims
  (`migrate_claims` → `industry.migrate`), the survivor's own statement for the
  same slot winning.
- NACE Rev. 2.1 replaces Rev. 2 from 2025 and the registers migrate over the
  following years; the bundle records its codelist version, and a Rev. 2.1
  bundle is a regeneration plus a mapping of the codes that moved.

## Source tiers, containment and staleness

Sources sit in tiers, encoded by their credibility scores rather than by name so a
new source lands in the right tier by scoring itself honestly:

| tier | sources | authoritative for |
|---|---|---|
| statutory (≥97) | SEC EDGAR 98, UK PSC 97 | ownership **and** stakes |
| official (≥90) | GLEIF 92 (validation-scaled) | existence, structure, consolidation |
| community (<90) | Wikidata 80, OpenCorporates 85 | discovery and enrichment |

Two rules keep the tiers meaning something on OWNS edges:

* **The freshness gate.** A source may refresh `last_scraped_at` on an edge at or
  below its own credibility, and no higher — a Wikidata visit re-confirming an SEC
  edge records a claim (corroboration) but does not launder the register fact's
  freshness. `last_scraped_at` on a register edge therefore means *the register
  confirmed it*.
* **Staleness** (`manage.py mark-stale`, default 180 days). `stale` is always `true` or `false` on an OWNS edge — never unset: since ArcadeDB 26.10.1 a `<>` on a null is false, so an unset flag drops out of every filter silently. The GLEIF and PSC writers stamp `false` since 2026-10-25 (1,274 dev edges had none). Wikidata has no
  retirement signal — a deleted statement just stops being seen — so a
  community-tier edge nothing has confirmed in six months is marked `stale=true`:
  dimmed in the UI, never deleted, never closed, because an unconfirmed community
  edge is weak evidence of removal and nobody stated an end date. Exempt: register
  edges (they retire facts properly via deltas, snapshot diffs and 0% amendments),
  pairs any register claim vouches for, and closed edges. The pass clears as well
  as sets, and the quality report counts stale edges per source.
* **Why, and since when** (since 2026-10-14). Every stale mark carries its evidence:
  `stale_reason` — one of `13f_not_confirmed` (the holder's 13F for the quarter
  ending `stale_since` no longer lists the position), `ex21_not_listed` (the newer
  Exhibit 21 / 8.1 dated `stale_since`, linked in `stale_source_url`, no longer
  names the subsidiary), `unconfirmed` (no source has confirmed the community-tier
  edge since `stale_since`, its last confirmation), `lei_lapsed` (the GLEIF record
  lapsed; `stale_since` is its last update), `not_restated` (the holder's last
  Schedule 13D/G, filed `stale_since`, is older than the form's restatement
  window — a 13G is restated every February while it lasts, so 15 months; a 13D
  only on a material change, so five years; `maintenance.RESTATEMENT_WINDOW_DAYS`,
  the second pass of `mark-stale`. Register-tier holdings were exempt from every
  stale pass, so a 13G/A from 2014 stood at full strength beside this year's and
  VeriSign's holders summed to 106 %, Sirius XM's to 202 %, on dev 2026-10-25) —
  and `stale_since`, the day that evidence is dated. The history endpoint hands them to the panel's timeline,
  which shows a dated "no longer confirmed" entry under that year with the reason,
  so a reader can see why a holding went grey. Every writer that clears the mark
  clears the three fields with it (`edge_schema.STALE_CLEAR`), so a healed edge
  carries no old reason. Edges marked before the fields existed keep a bare
  `stale = true` until the next pass or refresh re-marks them. The same mark is
  set by the writers themselves where a register says a record is no longer
  maintained: a GLEIF relationship record whose registration has **LAPSED** (see
  *The record's own registration status* under GLEIF below), and a 13F holding or
  Exhibit 21 row a newer filing no longer confirms.

### How surely it was read — `read_from`

Credibility says **who speaks**; `read_from` says **how much of the value is
ours**. The two are different questions, and conflating them would break the
tiers: an Exhibit 21 written as sentences is still a statutory filing, only one
we may have misread, and Wikidata's clean JSON is still a community source.
Every claim and every OWNS / HAS_ROLE edge carries one of six grades
(`edge_schema.READ_GRADES`, best first):

| grade | the value came from | examples |
|---|---|---|
| `field` | a named field of a structured record (XML, JSON, NDJSON, XBRL) | GLEIF, UK PSC, Wikidata, OpenCorporates, 13F, Form 3/4, Form D, a 13D/G filed as XML (since 2024-12-18), an acquirer's XBRL business-combination facts (`sec-acquisitions`: the closing date and price of a deal, read from the 10-K / 10-Q instance) |
| `table` | a cell under a header the filer wrote for that table | most Exhibit 21 rows, a "Direct controlling entity" column |
| `form` | a numbered item of a regulator's form, found in its text rendering — the label is the regulator's, only the figure is read | a pre-2024 13D/G cover page (row 11's share count, row 13's percent, the power rows) |
| `layout` | the page's layout: an indented or heading-named parent, a header carried onto the next page, a headerless table, a list grouped under country headings | the Exhibit 21 tree, Western Union's second page |
| `prose` | items written as text lines — a list in sentence form, each line shaped like an entry | an Exhibit 21 / 8.1 written as "Name (Jurisdiction)" paragraphs (Alibaba) |
| `narrative` | a fact picked out of running text: a name, a verb, a date in a sentence | an 8-K Item 5.02 departure; a news-like source, should one ever be admitted |

The three text grades differ in how much of the text the pattern has to
understand: a form's row carries a fixed label and one figure; a list line has
a shape; a sentence has only its words. `form` sits above `layout` because a
layout reading recovers a *relationship* the filer expressed through spacing
alone, while a form reading recovers a figure the regulator told the filer
where to write.

Rules:

- **An edge is as surely read as its least sure part** (`weakest_reading`): an
  Exhibit 21 row from a table whose place in the tree came from indentation is
  a `layout` edge.
- **Below credibility, above the date** in `answer_rank` / `best_claim`: the
  grade orders what *equally credible* sources say — a 13G read from its XML
  over one read off a text cover page — and never lifts a source over a more
  credible one. A register's field is not more true than a filing's sentence,
  only more surely ours.
- **Unset** means nobody read anything: a manual entry, a federated edge whose
  peer published none, or an edge from before the grade.
- **It is about our reading, not the source's worth.** A low grade is a parser
  worth improving (`manage.py quality-report` counts edges per source and grade),
  never a reason to admit a weaker source.

## The OWNS property schema

`app/scraper/edge_schema.py` is the one place that knows what sits on an edge:
`OWNS_PROPS` (32 properties), `ROLE_PROPS`, `RELATED_TO_PROPS`. The merge paths
generate their Cypher from it; the two runner writers build their CREATEs from
`owns_props(**kw)`, which rejects any keyword the schema lacks; and
`tests/scraper/test_writer_parity.py` parses every other writer's source and
fails when one invents or drops a property.

**Adding a property to an OWNS edge** is therefore: add it to `OWNS_PROPS`,
pass it where a writer learns it, and update that writer's expected subset in
the parity test — which fails with a readable diff until you do. The merges,
the schema-parameterised merge tests, and `owns_props` all pick it up with no
further edits. The claim should usually learn it too (`claim_props`); the
parity test's claim check says which fields count as factual.

## Voting groups

A Schedule 13D filed by several parties acting together is one **`VotingGroup`**
vertex, not a bloc percentage hung on whoever submitted the form. AB InBev's
52.3% sat on BRC S.à r.l. for exactly that reason, when nine parties vote it and
BRC merely filed.

```
(Stichting)─┐
(BRC)───────┤ MEMBER_OF
(EPS)───────┼─▶ (VotingGroup · 5 parties)──OWNS──▶ (AB InBev)
(Lemann)────┘        type: voting_group        stake_percent: null
                                               voting_power_pct: 52.3
```

* **Only Schedule 13D.** A 13G reporting "shared voting power" is an asset
  manager aggregating across its own subsidiaries — State Street, Morgan Stanley
  — which is not a governance bloc. Modelling those would be misleading; on the
  dev graph it would have created 17 group nodes instead of ~4.
* **Membership is `MEMBER_OF`**, its own edge type since 2026-10-26 (it was
  `RELATED_TO {relation:'group_member'}` beside the 13F affiliates, which kept
  a `relation` filter in every membership read). Membership is not ownership.
* **The group's OWNS edge carries a null stake.** Its members hold the shares
  individually; adding a bloc percentage to theirs is what put companies over
  100% of themselves. The edge is written with `owner_label="VotingGroup"`.
* **A newer filing's roster replaces the old one, members included**: the
  name follows the count and the membership lines of parties it no longer names
  are removed (`_prune_group_membership`, since 2026-10-26 — Symbotic's group
  was named for 8 and listed 12).
* **The filer's own bloc edge is retired** when the group is written — it is the
  same filing, not a second fact — but only the stakeless one, so a member that
  also reports a real holding keeps it.

### Identity

Groups are matched by **roster overlap**, never by name or by filer:

* each member key holds *both* a CIK and a diacritic-folded normalised name, and
  two entries match when **either** does. EDGAR gives a CIK only to registrants —
  one of AB InBev's nine — and pre-2024 filings carry names alone, so a
  single-identifier key would fail to match a member against itself across the
  December-2024 XML boundary. Folding matters too: EDGAR writes ASCII
  ("Eugenie Patri Sebastien"), everyone else accents it.
* two rosters are the same group when they share **≥2 members and ≥50% of the
  smaller roster**. Keying on the filer breaks when another member files the next
  amendment; keying on the exact set orphans the node when one party joins or
  leaves. Overlap survives both, while keeping AB InBev's two overlapping
  agreements — they share only the Stichting — as separate groups.

A voting group is **not a legal entity**: it holds no LEI and has no country. It
used to be an Entity of type `voting_group` that the quality report's identity
ratio, the country backfill, the name-based dedup, the federation export and
`/entities/without-country` each had to fence off — and everything that forgot the
fence treated it as a company (the flag defaults, the entity totals, the passes
that walk the entities). Since 2026-10-26 it is its own vertex type and nothing
that walks the entities meets one; a group is never merged (its identity is its
roster), and its claims and stake history are keyed by its id as before. The
client is unchanged: the group opens through the entity routes and reads
`type = voting_group` off every node it draws.

## GLEIF sourcing

GLEIF data comes from the **GLEIF golden copy** (current, daily), keyed `lei:{LEI}`:
`manage.py gleif-lei-cdf` imports the **entities** (name, country, legal address,
legal-form type) from LEI-CDF; `gleif-rr` imports the **relationships** (direct/
ultimate parents) from RR-CDF; `gleif-succession` imports mergers from LEI-CDF;
`gleif-repex` imports the **reasons a company gives for reporting no parent**.
This replaces the OpenOwnership GLEIF BODS export (`bods-gleif`), which was frozen
at 2025‑03.

Worth knowing about the provenance, because it shapes how much weight the data
carries: **GLEIF collects almost none of it itself.** An entity applies for an LEI
to an accredited LOU (~40 of them — LSEG, Bloomberg, GS1, WM Datenservice …) and
supplies its own reference data; the LOU validates it against the national
business register where one exists, and the record names that register in
`RegistrationAuthority`. So the real upstream source is ~200 national registers,
and GLEIF is the normalising layer over them. Level 1 (who is who) is
registry-corroborated and reliable; Level 2 (who owns whom) is **accounting
consolidation, not shareholding** — no percentages — and can be declined, which is
what the reporting exceptions record.

### A node that learns its hard id later

A node created by name (an Exhibit 21 row or a 13D party without a CIK, a founder
name) carries a uuid. When a writer later stamps a hard id onto it (the SEC upsert
meets its CIK, the person writer matches it by name with a CIK, Wikidata identifies
it), the writer renames it to the keyed id at once (`node_ids.key_on_learning`, via
`merged_ids.rename_node_id`). The rename moves the node, re-hashes its claims under
the new endpoint, re-points the stored references and leaves a MergedId forwarding
row, so old links keep opening. A keyed id already taken is a twin for the dedup and
is never forced. Until 2026-10-28 a one-off heal did this after the fact; the
2026-10-27 rebuild, run without it, left WARBURG PINCUS LLC and Steven Sarracino
under uuids.

**A keyed node that learns a better id is re-keyed too** (2026-10-30, findings
item 20). The ranking is `lei:` > `sec:` > `wd:` > uuid, the user's call: a
register's id before a knowledge base's. Before, only a uuid was re-keyed, so the id
depended on which source wrote first: Viavi was `wd:` in one rebuild and `sec:` in the
next (Wikidata's search was too busy once), and since #490 a GLEIF record joined to an
EDGAR filer left the LEI under the filer's `sec:` id — 22 such nodes on dev, among them
JPMorgan, Morgan Stanley, State Street and eBay. The GLEIF delta updates `lei:` ids
only (`gleif_incremental.existing_lei_ids`), so it never refreshed them or their
relationships, and a full GLEIF import would have minted `lei:` twins. Now the GLEIF
join (`register_pointer._join_filer`), the SEC and Wikidata writers and every entity
merge (`maintenance._apply_entity_merge`: the survivor may carry the loser's better
id) re-key; the merge command reports the new id. An id that had been forwarded away
and comes back (a `lei:` node folded into its filer, which now takes the LEI's id)
loses its old forwarding row, so nothing forwards to itself. The bulk importers' own
keys (`gb-coh:`, `chpsc:`) are never replaced. Stored nodes change as writers touch
them, and all of them on the next rebuild.

**A caller keeps writing to the node under its new id.** A re-key inside a call the
writer only asked for help (the GLEIF join behind `register_pointer.attach_stated_lei`)
leaves the writer holding the old id, which names nothing any more: the 2026-10-31
rebuild left 17 companies whose Wikidata item stated their LEI (Bank of America, Morgan
Stanley, NYT, State Street, eBay, …) without their item id, and their Wikidata board
members without the seat that joins them to the Form 4 person — 18 people stood twice
(findings item 22). The Wikidata writer follows the forwarding row
(`merged_ids.resolve_current_id`) when the call landed elsewhere than the node it had;
a GLEIF record written on its own leaves no forwarding row, and the writer stays put.

### How far GLEIF checked a record

Every LEI record carries `ValidationSources`, GLEIF's own statement of whether an
LOU corroborated it against the register or simply took the entity's word for it.
It is the only per-record quality signal in the file, and it scales the
credibility we stamp on the entity rather than every GLEIF record scoring the
source's flat 92:

| `ValidationSources` | `name_credibility` | meaning |
|---|---|---|
| `FULLY_CORROBORATED` | 92 | an LOU checked it against the business register |
| `PARTIALLY_CORROBORATED` | 88 | some of it, or against a source short of the register |
| `ENTITY_SUPPLIED_ONLY` | 82 | nobody checked; the entity said so |
| `PENDING` | 82 | validation unfinished — the same evidential state |
| absent / unrecognised | 92 | no penalty for a field we could not read |

That score decides which source's name survives a conflict and which claim wins
in [`claims.py`](../backend/app/claims.py), so without it a self-declared name
outranked a registry-checked one from elsewhere on the strength of its source
alone. The deductions are deliberately small: a company's own statement of its own
legal name is still better evidence than a Wikidata label (usually the common name
rather than the registered one), so an uncorroborated GLEIF record stays above the
community sources at 80 while losing to a corroborated one. On a day's delta of
18,166 records: 98.7% fully corroborated, 0.8% entity-supplied, 0.4% partial.

### Which name a company shows

`Entity.name` is **the most credible source's name, whichever source created the
node**, with `name_credibility` beside it: SEC 98, Companies House 97, GLEIF 92
(less a penalty for a record GLEIF did not check, below), Wikidata 80. The Wikidata
writer replaces a name only of lower or equal credibility; the SEC scrape gives the
filer EDGAR's own name for its CIK on the same rule and keeps the replaced name in
`aliases`, so search still finds it. A name another filing spells (a 13D party, an
Exhibit 21 row) never renames a node. Until 2026-10-28 the SEC writer never renamed,
so 23 filers showed the Wikidata label of whichever source ran first: Molson Coors
showed its pre-2020 "Molson Coors Brewing Company".

### How far GLEIF checked a relationship

A relationship record carries the same statement about itself — `ValidationSources`
(the level) and `ValidationDocuments` (what the LOU checked it against:
`ACCOUNTS_FILING`, `REGULATORY_FILING`, `SUPPORTING_DOCUMENTS`, `CONTRACTS`,
`OTHER_OFFICIAL_DOCUMENTS`) — and the picture is nothing like the entities':

| `ValidationSources` on ACTIVE consolidation records, 2026-10-06 | records |
|---|---|
| `FULLY_CORROBORATED` | 135,169 |
| `ENTITY_SUPPLIED_ONLY` | 109,060 — 42 % |
| `PARTIALLY_CORROBORATED` | 16,108 |

Both are kept **as provenance** — on the edge (`validation_sources` /
`validation_documents`, part of *the answer*, so they move with the source that
holds it), on the claim, and in the Sources panel's rows — and **never folded into
the credibility score**. The entity's `name_credibility` penalty (−10 for
entity-supplied) would put 109k relationship edges below the official-tier line
(90) and hand them to the community-tier rules: six-month staleness, a Wikidata
statement outranking them. That is not what "the entity said so, and the LOU
published it" means. A reader sees the level beside the link instead, and
`answer_rank` may weigh it later, within the tier. On a pair stated both ways the
direct record's checking is the edge's, the ultimate's fills a gap; the delta
restates it with the record. `ValidationReference` (the evidence document's URL,
which the API shows for Metaswitch) is on no record of the golden copy, so it is
not read.

### Why a company reports no parent — reporting exceptions (`repex`)

**`repex` is GLEIF's own abbreviation for *reporting exceptions*** — it is the
name of the golden copy's third file, and it is worth spelling out, because the
concept behind it is not obvious from the word.

**The obligation is what makes this data exist.** Every LEI holder is *required*
to report its parent company, and if it will not or cannot, it must file a
**reason why not**. Silence is not an allowed answer. So GLEIF's Level 2 has
three states, not two:

| the record says | what we hold |
|---|---|
| a parent, named | an `OWNS` edge |
| **a reason, no parent** | `no_direct_parent_reason` / `no_ultimate_parent_reason` |
| nothing at all | nothing — genuinely unlooked-at |

We used to collapse the last two into "no parent known", which is the thing this
importer fixes. A worked example from the dev graph: **GITHUB INDIA PRIVATE
LIMITED** has no `OWNS` edge and obviously has a parent. What GLEIF holds is
GitHub India's own declaration that *a parent exists and its accounts are not
published, so it is not naming it* (`NON_PUBLIC`). That is a different fact from
"nobody has looked", and the graph can now tell them apart.

`manage.py gleif-repex` imports the file. The direct and ultimate parents are
separate questions with separate answers — a company can decline them for
different reasons, and most filers answer both.

**Take the definitions from GLEIF, not from the code names.** They are published
only as `xs:documentation` annotations inside the [Reporting Exceptions 2.1
XSD](https://www.gleif.org/lei-data/access-and-use-lei-data/level-2-data-reporting-exceptions-2-1-format/2021-07-20_reporting-exceptions-format-v2-1.xsd)
— there is no downloadable code list as there is for ELF and RA, and the API
returns the bare enum. `NO_LEI` is the trap, and this document got it wrong until
2026-08-19: it does **not** mean the parent has no LEI, it means the parent
*refuses* to have one.

| reason | GLEIF's definition, condensed | in the dev graph |
|---|---|---|
| `NO_LEI` | *"The parent does not consent to have an LEI."* A refusal, not an absence. Sometimes the filer points at the parent anyway in `ExceptionReference`, kept as `…_reference` | 23 |
| `NON_PUBLIC` | the relationship must not be disclosed publicly (GitHub India, Nestlé Malaysia). Since v2.1 this is the umbrella for the five deprecated reasons below | 23 |
| `NON_CONSOLIDATING` | controlled by legal entities not subject to preparing consolidated financial statements — so GLEIF's accounting-based Level 2 would never carry the parent however hard we looked | 9 |
| `NO_KNOWN_PERSON` | no known person controls it, e.g. diversified shareholding (Rolls-Royce Holdings, Etsy) | 3 |
| `NATURAL_PERSONS` | controlled by natural persons with no intermediate legal entity meeting the definition of a consolidating parent (Apple, Barclays). **The most useful of them**: it marks exactly where GLEIF stops and the beneficial-ownership registers (UK PSC, SEC) have to take over | 2 |
| `CONSENT_NOT_OBTAINED`, `LEGAL_OBSTACLES`, `BINDING_LEGAL_COMMITMENTS`, `DISCLOSURE_DETRIMENTAL`, `DETRIMENT_NOT_EXCLUDED` | **Deprecated in v2.1 (1 March 2022)** and folded into `NON_PUBLIC`. Still arriving on records filed before then and not since refreshed — 17 of 2,986 on a day's delta — so they are still read and still displayed | — |

An unrecognised reason is stored as it stands rather than dropped: the list has
changed before, in both directions.

The importer **never creates a node**: writes are `UPDATE … WHERE id` with no
`UPSERT`, because the file describes 6.3 million companies and a statement about
one this database does not carry must land nowhere rather than mint a node whose
only content is "has no parent, because". Reasons are stored as GLEIF's own enum
values; turning them into prose is the UI's job.

**Not yet displayed.** The properties reach the API, but no panel says "no parent
reported — the parent's accounts are not public" yet. Until it does, a user still
cannot tell the second state from the third.

### How countries are represented, and one grouping we deliberately do not apply

Countries are ISO 3166-1 alpha-2. Where a source records a sub-national legal
jurisdiction, GLEIF gives it as ISO 3166-2 (`US-DE`). ISO's distinction happens to
be exactly the one that matters here:

| | code | why |
|---|---|---|
| Delaware | `US-DE` | a **subdivision** — Delaware genuinely is part of the US |
| Cayman Islands | `KY` | its **own country** — a British Overseas Territory, *not* part of the UK |

Cayman, and likewise Jersey `JE`, Guernsey `GG`, Isle of Man `IM`, BVI `VG`,
Bermuda `BM` and Gibraltar `GI`, are under UK sovereignty but are separate
jurisdictions with their own legislatures, company law and tax regimes. **Do not
fold them into `GB`.** It is legally wrong, and it would erase the very fact that
makes them worth showing: a company registers in the Caymans precisely *because*
it is not the UK. Barclays Capital (Cayman) is `KY` by jurisdiction and `GB` by
headquarters, and collapsing the two would turn an offshore structure into "a
British company".

**A future lens, not a data change.** "The UK and its dependencies" is a genuinely
useful analytical grouping — the UK has repeatedly been pressed to make its
Overseas Territories adopt public beneficial-ownership registers, so *how much of
this structure sits in the British orbit* is a real question. If that is ever
offered, it must be a **curated list layered over the ISO codes** and labelled as
such, never a remapping of the underlying data. ISO will not supply it, because
legally it is not true.

Which countries actually carry subdivisions is narrow, and measured rather than
assumed — from 250,000 LEI records: **US 90%** (Delaware dominant), **CA 74%**,
**KN 40%** (Nevis), **AE 27%** (Dubai, Abu Dhabi), **MY 19%** (Labuan), **GB 0.3%**.
China, India, Switzerland, Germany and Australia record none: their registers are
regionally administered, but the region is not a choice of legal domicile, so
there is nothing analogous to Delaware to capture.

### Filling a missing country

The SEC scrape fills `country` where it is blank, from EDGAR (the filer's state
or country of incorporation). It only ever fills a blank — an existing country is
never overwritten.

Worth knowing why: subsidiaries and owners are written as *stubs*
when some other company is scraped, with no country of their own, so a company
that only ever appeared as an owner had none at all. BlackRock and The Vanguard
Group were both missing from the map for that reason.

Wikidata is never asked for a country (2026-10-27). It writes no address of
any kind — no `country`, `countries`, `hq_*` or `hq_locations` — and no `type`,
on create or on update: the registers state them, and a Wikidata item often
describes a group rather than the legal entity. Unilever's Q157062 states both
the United Kingdom and the Netherlands in arbitrary order, and the Wikidata
update put NL over the GB that GLEIF and Companies House state for UNILEVER
PLC. A stub Wikidata creates carries an LEI or a CIK, so its register fills
the country; a search target only Wikidata knows stays without one, and has
the default type `company`. Values Wikidata wrote before that date are still
on the nodes.

For SEC filers the order is deliberate: **state of incorporation first, business
address only as a fallback.** EDGAR's business address is where the *filing* comes
from — DEUTSCHE BANK AKTIENGESELLSCHAFT lists New York — so trusting it would move
German banks to the United States. A filer with only a US address and no stated
incorporation is left blank, because a wrong country is worse than none.
**The annual report before the company record** (2026-10-30): both the
incorporation and the headquarters come first from the cover of the filer's
newest 10-K/20-F/40-F (`sec_edgar.report_cover`: `dei:EntityIncorporationStateCountryCode`
and `dei:EntityAddress…`, in the registrant's own context), and from EDGAR's company
record (`stateOfIncorporation`, `addresses.business`) only when the report states none —
older filers before inline XBRL, deregistered ones, filers without annual reports. The
record is the filer's own entry in EDGAR's database, checked against nothing: Hong Kong
for Alibaba (20-F: Cayman Islands), New York for HubSpot (10-K: Delaware), empty for
MercadoLibre, Embraer and Televisa. A P.O. box on the cover is no headquarters (Inter &
Co tags its Cayman registered agent's "PO Box 309, Ugland House"); the record's office
stays. Over the 59 dev filers with an Exhibit 21 the report changes the country of one
(Alibaba HK → KY), fills four, and changes three headquarters' city (Televisa "Colonia
Santa Fe" for "MEXICO, D.F.", PayPay "Tokyo", Cogent "Washington, D.C"; Inter & Co's P.O.
box is refused). Embraer's 20-F names its São Paulo office, the record São José dos Campos.
A headquarters only ever fills a blank, so stored nodes change on the next rebuild.
Jurisdiction and headquarters are kept in **separate fields throughout**.
Coalescing them is tempting when one is missing, and it is what the
Registered/Headquarters switch on the map exists to avoid — an early country
backfill did coalesce, and wrote Morgan, Grenfell & Co.'s London headquarters
(from Wikidata) into its jurisdiction field, which the source never claimed.

### One edge per pair

Whenever a company's direct parent is also the top of its tree, GLEIF states the
pair **twice** — once `IS_DIRECTLY_CONSOLIDATED_BY` and once
`IS_ULTIMATELY_CONSOLIDATED_BY`. On the full golden copy that is **88,839 of
257,651** consolidation relationships (35%).

Those are two statements about one holding, so `gleif-rr` folds them into a single
OWNS edge rather than writing one each. Two parallel edges between the same nodes
used to mean the graph drew one and hid the other, the profile listed the owner
twice, and `mark-shortcuts` had to *prove* the second redundant before anything
could filter it.

Nothing is discarded:

| Property | Meaning |
|---|---|
| `direct_or_indirect` | `direct` when GLEIF stated the direct relationship — the more specific claim wins |
| `since` / `until` | the **earlier** start of the two (the parent has held the child since then, indirectly first perhaps — time travel reads `since`, never `ultimate_since`), and an end only when **both** relationships have ended |
| `also_ultimate` | `true` when the ultimate relationship was stated for the same pair, i.e. this direct parent is also the top of the tree |
| `ultimate_since` / `ultimate_until` | the ultimate relationship's period, kept only when it **differs** from the edge's (6.9% of folded pairs — a parent can become the direct consolidator years before an intermediate holding dissolves and makes it the ultimate one) |

An ultimate record whose pair has no direct record keeps `direct_or_indirect =
indirect` and is *not* rewritten as direct: GLEIF never stated a direct holding
there, and that edge is often the only route to the company.

`mark-shortcuts` still runs, but only the genuinely multi-hop shortcuts are left
for it to prove.

The daily `gleif-update` delta maintains the same invariant: it looks its edges up
by **pair**, not by marker, so an ultimate-parent record for a folded pair updates
that edge instead of creating a parallel one. Retiring one of a folded edge's two
relationships does not end the holding — the other still stands: the edge drops
`also_ultimate` (the ultimate link ended), or — the direct holding ended — the
direct period is **closed as it stood** and the ultimate relationship continues as
its **own indirect edge** from its own start (relabelling the edge "indirect since
2018" erased the direct years from every past year). With no RR edge for the pair it **adopts**
another source's active edge (counted `adopted`) — see *One edge per pair,
whichever sources assert it* above.

**Ended relationships in the full copy.** `gleif-rr` writes an INACTIVE
relationship as its **own closed edge** (counted `ended`) — it used to drop them,
so a holding sold in 2020 never existed in 2015 — with the end date as above.
With several relationship periods, the open one (else the latest-ended) describes
the record, not the first in the file. The full import is for a fresh database:
it CREATEs every edge, and on a populated graph its closed edges would sit beside
the existing ones. Founding dates written in bulk (`founded`, `incorporated_on`) only
ever move **earlier** and are never blanked — a re-registration's creation date
must not hide a company in the years it existed.

Once the full copy is loaded, `manage.py gleif-update` applies GLEIF's published
**delta files** for all three sections — entities, relationships and reporting
exceptions (only records changed since the last publish) — as a fast daily
refresh — see [`app/scraper/gleif_incremental.py`](../backend/app/scraper/gleif_incremental.py).
It is **retirement-aware**: a relationship whose `RelationshipStatus` becomes
non-ACTIVE has its open `OWNS` edge *closed* (`until` = the relationship period's
`EndDate`; GLEIF states none → the record's `LastUpdateDate`, the day it had ended
**by**, with `until_reason = gleif_inactive` — never the run's own date, which made
it look current until today; an already-closed edge is never re-stamped). A
relationship that begins again after it ended is a **new period**, a new edge
beside the old one — reopening the old edge merged 2010–2019 and 2024– into one;
only a record restating the same period reopens it.

**History before the import** (`manage.py gleif-rr-history`, once after the first
full import — [`gleif_rr_history.py`](../backend/app/scraper/gleif_rr_history.py)).
The golden copy only shows today: an ended relationship is gone from it, not
marked (60 INACTIVE of 489,953 on 2026-10-05), and deltas exist for the last
day/week/month only. GLEIF's archive keeps every golden copy since 2018-02-09;
the command reads the relationship file of the first publish of each month plus
the latest (~105 files, 5 MB in 2018 to 34 MB now), and a pair present in one
and gone from the next gets its own closed edge: `until` = the first snapshot
without it, `until_reason = gleif_dropped` ("ended by then" — up to a month
late, never early), `source_date` = the last snapshot listing it. A pair with no
stated start gets `since` = the first snapshot listing it, `since_basis =
gleif_first_seen` — on current edges only where `since` is empty. It never
creates a current edge or a company (a period whose companies are not in the
graph is counted, not written), a **dip** is not believed — a snapshot over 5 %
smaller than the last believed one is held back a month and skipped only if the next
one recovers (2023-08-01: 281,309 records between 398,842 and ~400k); a real
clean-up stays down and is believed (the file shrank in 26 of 105 months, 2019-09 by
10.3 %) — and the intervals file it builds is kept on the box,
so a second run (after a restore) downloads nothing and writes nothing twice.

**A stated start GLEIF's own archive refutes** is not believed. A company that is the
top of a tree has no parent, so a start stated before the last snapshot in which the
child was still someone's ultimate parent — and before the relationship itself
appeared — is contradicted: Activision Blizzard registered Microsoft as its ultimate
parent "since 2001-07-03" in July 2026, while King.com named Activision its own
ultimate parent until 2026-07 (the acquisition closed 2023-10-13). 695 of 367,701
stated starts on 2026-10-06. The edge then gets the first snapshot listing it as a
lower bound (`gleif_first_seen`) and `since_not_before` = the last snapshot the child
was top; GLEIF's date stays in its **claim**, and no merge (`combine_since`,
`owns_merge.fold`) or daily delta brings it back onto the edge. The archive only
proves that one side of the contradiction is wrong, not which: Barclays Bank PLC's
subsidiaries named it their ultimate parent until 2021 although Barclays PLC has
owned it since 1985, so its stated 2012 is weakened too. That failure mode is a
dimmed edge ("not documented" before the date), never a hidden one; the other is a
false owner since 2001.

An ACTIVE record's own end
date is kept, and every RR edge carries the record's `LastUpdateDate` as
`source_date`, its evidence date. And an entity whose `EntityStatus` is `INACTIVE` is flagged
`active=false` with its `gleif_registration_status` recorded — by the full load and
the delta alike — neither is ever
deleted (GLEIF never deletes; merges keep flowing through `SUCCEEDED_BY`).

**The record's own registration status.** A relationship record carries two
statuses: `RelationshipStatus` says whether the relationship holds, and the
record's `Registration.RegistrationStatus` says whether anyone still maintains the
record. GLEIF defines **LAPSED** as "not renewed by its NextRenewalDate" — the LOU
stopped checking it — so an ACTIVE relationship on a lapsed record is the last
word, not a current one: Metaswitch Networks Ltd still reads "consolidated by
Microsoft" on a record last updated 2023-06-02, while Companies House records its
sale to Alianza in 2025-03. On the 2026-10-06 golden copy 102,262 of 490,169
relationship records are LAPSED, and 62,934 of the ~260k ACTIVE consolidation
records stand on a lapsed child — a quarter of GLEIF's ownership. Such an edge is
drawn **`stale = true`** (the mark the staleness pass uses: dimmed in the panel,
"not documented" after its `source_date` in time travel), never closed — nobody
stated an end. A folded pair is stale only when *every* record it stands on has
lapsed; a maintained ultimate record still confirms the pair. **ANNULLED**
("erroneous or invalid after it was issued") and **DUPLICATE** records are not
statements and draw nothing. The delta keeps the mark current: the record that
lapses or is renewed is in that day's delta, and a maintained record clears the
mark (an edge another source holds keeps that source's mark).

**The share the record states.** A relationship record may carry
`RelationshipQuantifiers`: a `QuantifierAmount` measured by
`ACCOUNTING_CONSOLIDATION` in `PERCENTAGE` (or with no units — 16,785 of them
state none; every amount in the file is under that one method). On 2026-10-06
49,527 of the ~260k consolidation records carried one: 40,025 say 100, 8,765
between 50 and 99.99, 453 below 50 (control without a majority — stored as
stated), 269 at or below 0 and 15 above 100. It is written as **`stake_percent`**
(the claim carries it too, `read_from = field`), the same figure every other
register's stated share is. The last two bands are entry errors, not facts:
**refused at the reader** — the edge is drawn without a stake, the record is
logged and counted (`implausible`) — the checklist's plausibility bound. On a
pair stated both ways the direct record's figure is the holding; the ultimate
record's figure (the consolidated interest from the top) fills in only where the
direct one states none. The delta restates the figure on GLEIF's own edge as the
newer statement — a record stating none clears it, as its claim does — and takes
the answer with it where `answer_rank` says so (within the official tier a
stated stake outranks none, so GLEIF's 100 % takes the answer from an Exhibit 21
row without a stake, while an SEC row with one keeps it). The import writes the
figure onto the edge **and the pair's GLEIF claim** from one property bag. How far the LOU checked each record rides along as
provenance — see *How far GLEIF checked a relationship* below. All
writes are idempotent — nodes UPSERT by id (batched) and edges are matched by
endpoints+marker before create — so re-applying a delta can't duplicate, hence no
`--bulk-load` and no whole-DB dedup. The default `--interval auto` is **gap-aware**:
it checkpoints the last publish it applied (an `ImportState` node) and picks the
smallest delta window (`LastDay`/`LastWeek`/`LastMonth`) that covers the gap since
then, so a few missed daily runs self-heal on the next one; a gap past ~30 days
can't be covered by a delta and fails loudly (→ full reload). UK PSC has no
equivalent clean delta feed
(Companies House republishes a daily *full* snapshot), so its incremental refresh
is deferred to a separate design.

## UK PSC sourcing

UK beneficial ownership comes from the **Companies House PSC snapshot** (current,
published daily), imported by `manage.py ch-psc`. Each line is one PSC controlling
a company identified only by its **company number**, so the controlled company is a
node keyed `gb-coh:{number}` — this import ensures it exists but leaves it un-named.
Company **names, addresses, incorporation dates and former names** are filled in by
the companion `manage.py ch-company-data` importer from the Companies House
**BasicCompanyData** product (the full UK register, monthly). That importer is a
pure *enrichment* pass — it only `UPDATE`s companies already in the graph (matched
on `gb-coh:{number}`, and since 2026-10-24 also on the `companies_house_id` a node
under another id carries: GLEIF keys a UK company `lei:…` with its number as a
property, and the external-id dedup merges a later `gb-coh:` twin into that node or
never sees one, a listed company having no PSC — 207 of dev's 238 UK companies,
Barclays and Rolls-Royce among them, had been reached by no register row), so the
5.6M-row register never creates isolated company nodes; former names land in `aliases` + `search_text`, and with their change-of-name
dates (`PreviousName_n.CONDATE`) in `former_names` — the register of record for its own
companies, so its dated list replaces what stood there (a bulk `UPDATE` cannot union;
the entity merge unions a SEC twin's list in). Individuals become `Person`
nodes, corporate/legal PSCs become
`Entity` nodes (keyed on their own UK company number where present). An individual's
name is built from `name_elements` (forename, middle name, surname) — the record's
`name` is a display string with the honorific in front ("Mr Michael Charles Saunders",
90 % of individuals), which until 2026-10-18 made `first_name = "Mr"`; a record without
elements has the title stripped. `natures_of_control` map to `stake_percent` (the
floor of the ownership-of-shares band, or of an LLP member's
right-to-share-surplus-assets band — the LLP's shares; the overseas-entity register's
open `more-than-25-percent` band floors at 25), `voting_power_pct` (voting-rights band
floor) and `ownership_type` (`controlling` when they carry voting / appointment /
significant-influence rights). This replaces the OpenOwnership UK PSC BODS export
(`bods-uk-psc`), which was frozen at 2025‑03.

**Which `kind`s are imported.** Six of the eight the snapshot contains: the four
person-with-significant-control kinds, plus the two Register of Overseas Entities
*beneficial owner* kinds and their corporate twins. Only the two **super-secure**
kinds are skipped — Companies House withholds those details for personal safety and
the record carries no name to write. The corporate beneficial-owner kinds
(`corporate-entity-beneficial-owner`, ~13k register-wide, and
`legal-person-beneficial-owner`, ~540) were **dropped until 2026-08-19** while their
individual twin was imported; a load from before then is missing them.

**A PSC OWNS edge carries `psc_self_link`** — the Companies House appointment link,
one per snapshot record. It is the key an incremental refresh matches an edge on, so
anything that recreates an OWNS edge (notably the entity-merge migration) must carry
it across or the edge is orphaned from its record.

### Refreshing it — a delta from a source that has none

Companies House publishes **no delta files**: one full snapshot, overwritten every
morning, and nothing else. So `manage.py ch-psc-update` computes the delta locally
— digest today's snapshot, compare against the digest of the last one applied, and
write only what moved (`app/scraper/ch_psc_incremental.py`).

Three facts about the data make that exact rather than a guess:

* **Ceased PSCs stay in the snapshot**, carrying `ceased_on` — 17.9% of records — and become closed edges with `until_reason = ceased` (a vanished record is `withdrawn`).
* **`notified_on` of 2016-04-06 is a lower bound**, not a start: the register began that day and control that already existed was notified on it (`since_basis = register_start`, on the edge and its claim). As a stated start it hid a parent holding its subsidiary since 1990 before 2016. **So is any `notified_on` in the register's first year** (to 2017-04-20) **on a company incorporated before the register**: an existing company declared its PSCs on its first confirmation statement, due on the anniversary of its last annual return plus 14 days, and the snapshot dates the control by that filing (54,815 records say 2016-07-01 and 28,737 say 2016-06-30 in the first 3M of the 2026-10-16 snapshot — the confirmation statement replaced the annual return on 30 June 2016). A company incorporated after the register began named its PSCs at incorporation, so for it the day is a real start. The PSC import cannot tell the two apart — the incorporation date comes from `ch-company-data` — so that import marks them at its end (`since_basis = register_first_year`, edge and claim; scoped to the companies an `--only` run touched), and the refresh keeps the mark when a touched record restates the same day. Where the control really did begin in that year the mark is still true, only weaker: time travel dims the edge before the day instead of hiding it.
  A PSC's control ending is an in-record change, not a record disappearing.
* **`source_date` is the snapshot's date** (stamped in the entry name), not `notified_on`: the start of the control is `since`; when the register last *said* it is the day it published the file. With `notified_on` there, a 2016 appointment the 2026-10-16 snapshot still listed read as ten-year-old evidence and lost every freshness tie. The bulk load and the refresh both date by the file they read, so a touched edge carries the snapshot that last confirmed it (a hand-made file without a date falls back to `notified_on`).
* **A corporate PSC's `country` is a code or nothing**: the filer's words go through the spelling table and the register fields, and what still resolves to nothing stays as written in `country_registered`.
* **Addresses are kept in parts** (since 2026-10-18): a corporate PSC's own address and the controlled company's registered office from BasicCompanyData give `reg_street` (premises + address lines, in the register's order), `reg_city` and `reg_postcode`, beside the string in `address` (the panel's "Registered address" row) and its key `address_normalized`. Until then both importers wrote only the assembled string, so the geocoder had to re-parse what the register had handed over separately (203k of 219k corporate PSCs carry a street or postcode; the PostCode column is on every register row). Only the parts a record has are written, so a re-import never blanks what GLEIF supplied on a merged node.
* **The registered office is not a headquarters** (decided 2026-10-18, finding #7 of the checklist walk): neither importer writes anything under `hq_*`. Until then both copied the registered office there so the Headquarters pin had something to draw for a UK company, which labelled an accountant's, agent's or law firm's door "Headquarters" (Bedford Row chambers, `C/O` addresses) and, because the company-data UPDATE sets unconditionally, replaced the real headquarters GLEIF or Wikidata had put on a merged node. A UK company now appears under the map's Registered pin; its Headquarters pin comes only from a source that states one — GLEIF's `HeadquartersAddress`, EDGAR's business address.
* **`data.links.self`** identifies an *appointment* and is unique across the file,
  so each changed record maps to exactly one OWNS edge (via `psc_self_link`).
* **A snapshot is a complete state.** Diffing against a week-old digest yields a
  week's changes; there is no catch-up window to fall out of, so nothing here
  corresponds to GLEIF's `choose_catchup_interval`.

| the record | the graph |
|---|---|
| new | nodes upserted, edge created |
| changed | re-mapped and rewritten; `until` written **unconditionally**, so a correction that removes `ceased_on` reopens the edge; the start is **combined** (the earliest any source gives, a stated one over a lower bound on the same day — an earlier GLEIF start on an edge PSC took over survives) |
| vanished | closed with `until` = the **snapshot's** date and `until_reason = withdrawn` — never deleted, and the `Claim` is closed with it |

`until_reason` distinguishes the two ways a holding ends: a *ceased* PSC really did
control the company until that date; a *withdrawn* one is the register saying the
record was wrong.

**The digest covers a projection** — the fields the mapping reads — not the raw
line and not Companies House's `etag`. Both of those are cheaper and both would
report millions of records as changed while `identity_verification_details` rolls
out across the register, for a field the graph never stores.

**Writes are batched on the indexed `psc_self_link`**: one `IN :links` probe per
1000 sorts a batch into updates and creates. The bulk importer's `CREATE EDGE`
duplicates on re-run and is cleaned up by a whole-database dedup pass — fine once,
for a load into an empty graph, and unacceptable for a refresh. Re-running against
an unchanged snapshot is not merely idempotent, it is a **no-op**: the diff is
empty, so nothing is attempted.

**A churn guard refuses before writing** if more than `--max-churn-pct` (5% by
default, scaled by the gap in days) of records moved, if the snapshot is not newer
than the last applied, or if the projection version changed. A snapshot diff can
rewrite the whole graph in one run if something upstream shifts, and the diff is
computed in full before any write precisely so that refusal is possible.

**Run by hand.** There is deliberately no cron: a new pipeline over 15.6M records
should be driven manually until it has proved itself over several real snapshots.

```
bash ~/scripts/steps/refresh-psc-snapshot.sh   # fetch + verify (2.2 GB)
python3 manage.py ch-psc-update --dry-run      # read the churn first
python3 manage.py ch-psc-update
```

The baseline digest is written by the full import (`ch-psc --digest-out`), from the
same pass over the same bytes, so baseline and digest cannot describe different
files. `--rebuild-digest` re-establishes one, forfeiting a day's changes.

⚠️ **`--only` used to lose data.** The subset import stopped reading once every
requested company had been seen once, on the assumption that a snapshot groups a
company's records together. It does not — measured on the real file, **16.9% of
companies reappear** after another has intervened — so about one company in six lost
its later PSCs, silently. Fixed 2026-08-19 by reading the whole file; **any curated
database built before that is incomplete** and wants reloading.
