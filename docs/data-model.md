# Data model

Owlgraph stores an ownership graph in ArcadeDB. Nodes are vertices; ownership
and roles are edges. Location is **not** a node — an entity carries its own HQ
(`hq_address`, `hq_city`, `hq_country`, `hq_lat`, `hq_lng`, `hq_locations[]`) and
its registered address (`address`, `reg_lat`, `reg_lng`), so the map reads one
record instead of traversing. Both are geocoded, because where a company is run
and where it is registered are different places and the map can show either.

## Nodes

| Label | Key properties |
|---|---|
| `Entity` | `id`, `name`, `name_normalized`, `type` (company/brand/holding/government/foundation/fund/nonprofit — inferred from Wikidata P31 instance-of and GLEIF legal form), `country`, `countries`, `founded`, `revenue`, `employees` (+ `employees_as_of` year, from Wikidata P1128), `wikidata_id`, `sec_cik`, `lei_id`, `companies_house_id`, `registered_address` (normalized GLEIF registered office — corroborates same-company dedup), `address` (human-readable GLEIF legal address for display), `legal_form` (ISO 20275 ELF name, e.g. "Private Limited Company" — resolved from the LEI-CDF ELF code via the bundled GLEIF ELF list), `registration_authority` + `registration_authority_code` + `registration_number` (gleif.org's "Registered At" — register name resolved from the RA code via the bundled GLEIF RA list, plus the entity's id there; also stored for corporate PSCs and OpenCorporates lookups), `register_id` (**hard identifier** `"{RA code}:{number}"`, e.g. `RA000585:07524813` — joins LEI/CH/CIK/QID in every dedup and resolution path; see docs/deduplication.md for the placeholder-code exclusions and the GB double key), `jurisdiction_code` (ISO 3166-2 legal jurisdiction where a source gives one, e.g. `US-DE` — see *How countries are represented* below; sparse, ~1% of GLEIF records), `hq_lat`/`hq_lng`/`hq_city`/`hq_country`/`hq_street`/`hq_postcode` (where it is **run**), `reg_lat`/`reg_lng`/`reg_geo_precision`/`reg_street`/`reg_city`/`reg_postcode` (where it is **registered** — geocoded from `address`; the two differ exactly where it matters, e.g. an agent's office on Grand Cayman versus a London headquarters, and the map's Registered/Headquarters switch draws one or the other; the `_street`/`_city`/`_postcode` parts are kept **as the source gave them** so geocoding is a structured query rather than an attempt to re-parse an assembled string — every country writes an address differently, and picking the city out of a comma-separated line is guesswork), `source_id`, `source_statement_ids[]` (BODS statement ids that declared the entity — accumulated for id-less parties collapsed under one name key, so per-statement provenance survives the collapse), `aliases[]` (other names — Wikidata skos:altLabel and SEC EDGAR `formerNames`), `search_text` (FULL_TEXT-indexed: name + description + aliases + registration number), `is_nominee` (name-detected nominee/custodian — holder of record, not a beneficial owner; `manage.py flag-nominees` backfills existing), `validation_sources` (GLEIF's own statement of how far it checked the record — `FULLY_CORROBORATED` / `PARTIALLY_CORROBORATED` / `ENTITY_SUPPLIED_ONLY` / `PENDING`; scales `name_credibility`, see *How far GLEIF checked a record* below), `no_direct_parent_reason` / `no_ultimate_parent_reason` (+ `_reference`) (why the company reports no parent, from GLEIF's reporting-exceptions file — see *Why a company reports no parent* below) |
| `GeoCache` | `query` (the cleaned address, UNIQUE), `lat`, `lng`, `precision`, `checked_at` — an address→coordinate cache so a shared registered-agent building is geocoded once rather than once per company registered there (24 dev companies share one Wilmington address). Misses are cached too, with the date, and retried after 30 days: re-asking Nominatim about an address OpenStreetMap does not have is the most wasteful thing the geocoder can do, and a permanent "no" would be a lie. |
| `ScrapeMiss` | `key` (normalised company name + `|` + ISO-2 country, or an empty country for an unrestricted search — UNIQUE), `missed_at` — an on-demand search that found nothing. The freshness gate protects the sources by looking at the *company* (`last_scraped_at`, `scrape_depth`), and a search that found nothing has no company to hang that on, so every repeat used to ask every source the same hopeless question again. Honoured for `SCRAPER_ONDEMAND_COOLDOWN_HOURS`, cleared by a later successful scrape of the same name+country, and expired rows are deleted when next looked up. The country is part of the key because France having no "Alphabet" says nothing about Germany. |
| `SearchDemand` | `key` (normalised query + `|` + ISO-2 country, UNIQUE), `query`, `country`, `searches`, `zero_results`, `selected`, `first_seen`, `last_seen` — one row per question asked, never per person asking. `zero_results` is the useful one: demand the graph could not answer, in demand order. Pruned after 365 idle days |
| `UsageCounter` | `key` (an allow-listed event name, UNIQUE), `count`, `first_seen`, `last_seen` — feature usage and clicked result positions (`result.rank.3`). The key space is closed because a public endpoint feeds it |
| `EndpointStat` | `key` (`METHOD route-template status-class latency-band`, UNIQUE), `count`, `last_seen` — request health. Keyed on the route **template**, never the path, so no record id is ever a key |
| `Person` | `id`, `full_name`, `first_name`, `last_name`, `alias[]`, `nationality` (**ISO-3166-1 alpha-2**, e.g. `GB` — Companies House records a demonym like "British", normalised on import; a value that cannot be recognised is kept verbatim rather than blanked, and `manage.py normalize-nationalities` reports the residue), `birth_date` (**month and year only** where the source publishes no more, which is the case for UK PSC), `birth_place`, `wikidata_id`, `sec_cik`, `wikipedia_url` |
| `Source` | `id`, `name`, `url`, `type`, `credibility_score`; for peers also `verified`, `key_id` |
| `User` | `id`, `email`, `password_hash`, `role` (admin/contributor/viewer), `language` (UI language at registration — emails to this person are written in it) |
| `ScraperSource` | `name`, `enabled`, `description` |
| `MergeLog` | `id`, `kind` (`person`/`entity`), `keep_id`, `keep_name`, `dup_id`, `dup_name`, `at`, `count` — merge history, deduped by (keep, dup_name) so a re-scraped duplicate bumps `count` rather than adding a row. `kind` keeps the two logs apart |
| `Peer` | `id`, `name`, `base_url`, `credibility_score`, `auth_token`, `public_key`, `enabled` — a trusted federation peer |
| `Claim` | `claim_key` (UNIQUE), `kind` (owns/role/succession), `from_id`, `to_id`, `source_id`, `stake_percent`, `voting_power_pct`, `ownership_type`, `role`, `since`, `until`, `source_url`, `source_date`, `credibility_score`, `first_seen_at`, `last_seen_at` — **what one source asserts about one relationship**; see below |
| `ScrapeRun` | `id`, `source`, `target`, `status` (running/ok/failed), `started_at`, `finished_at`, `total`, `error` — the scrape run log (capped) |

## Relationships

| Pattern | Properties |
|---|---|
| `(Entity\|Person)-[:OWNS]->(Entity)` | `stake_percent` (economic holding), `voting_power_pct` (voting rights — kept separate from the stake; from BODS `votingRights` interests and DEF 14A proxies), `interest_types[]` (BODS interest kinds behind the edge: shareholding/votingRights/appointmentOfBoard/…), `direct_or_indirect` (from GLEIF RR-CDF: `direct` = directly-consolidated parent, `indirect` = ultimate parent), `also_ultimate` / `ultimate_since` / `ultimate_until` (see *One edge per pair* below), `shortcut` (set by `manage.py mark-shortcuts`: this indirect edge duplicates a path of direct edges. Edges marked `true` are excluded from the full-profile owners/subsidiaries lists **and their counts**, so the panel and the graph agree; absent means unproven and it is kept), `ownership_type` (see the vocabulary below), `since`, `until`, `source_id`, `source_url`, `source_date`. Free float / >100% conflicts aren't stored — the full-profile endpoint derives them from the disclosed stakes on read (`ownership` summary) |
| `(Person)-[:HAS_ROLE]->(Entity)` | `role`, `since`, `until`, `source_id`, `source_url`, `source_date` |
| `(Person)-[:RELATED_TO]->(Person)` | `relation`, `source_id` |
| `(Person)-[:NOT_DUPLICATE]->(Person)`<br>`(Entity)-[:NOT_DUPLICATE]->(Entity)` | `at` — marks two nodes confirmed to be *different* (keep-separate). The entity dedup checks these **per pair**, not per group: a third same-named company must not drag a node someone explicitly separated into a destructive auto-merge |
| `(Entity)-[:DUAL_LISTED_WITH]->(Entity)` | links share classes of a dual-listed company |
| `(Entity)-[:SUCCEEDED_BY]->(Entity)` | corporate succession/rename, directed predecessor → successor (e.g. Twitter → X Corp.); from Wikidata P1366/P1365 (with `since` from the P585 qualifier) and from GLEIF LEI-CDF (MERGED/DUPLICATE → `SuccessorLEI`, keyed `lei:{LEI}`). `since`, `source_id`, `source_url`, `source_date` |

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

Values arriving from outside are coerced onto this set (`coerce_ownership_type`),
and a moderator pin is validated against it at the API boundary. `free_float` is
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

- **Keyed** on `claim_key` = digest of (kind, from_id, to_id, source_id), UNIQUE.
  A source re-asserting the same relationship updates its own row, so re-imports
  are idempotent here even though the edges still need a dedup pass. The parts
  are length-prefixed before hashing, so an id containing the separator cannot
  make two different claims collide.
- **Written by the edge writers themselves** — `_BatchWriter.owns/role/succeeded_by`
  for bulk imports and `record_claim` in the incremental scrapers — so an importer
  cannot record an edge and forget the evidence. This now includes **every** OWNS
  writer: the GLEIF delta, both PSC refresh paths (batched, same statement shape
  as their edge writes), federation imports (credibility = the peer's configured
  trust), and the manual API (skipped when the request names no `source_id` — a
  claim is one source's statement, and unsourced claims would all share a key).
- **Which claim wins**: highest `credibility_score`, ties broken by the most
  recent `source_date` (`best_claim` in [`app/claims.py`](../backend/app/claims.py)).
  A credible "owns, amount undisclosed" deliberately beats a weak source's number.
- **Read** by the Sources panel via `to_id` — everything asserted *about* an
  entity. Claims about the subsidiaries it owns carry `from_id` = that entity and
  are never selected, so the panel cannot flood with one row per subsidiary.

An entity's own record provenance is a different question, answered from its hard
identifiers (`_entity_own_source_rows`) — claims describe relationships, not nodes.

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
* **Staleness** (`manage.py mark-stale`, default 180 days). Wikidata has no
  retirement signal — a deleted statement just stops being seen — so a
  community-tier edge nothing has confirmed in six months is marked `stale=true`:
  dimmed in the UI, never deleted, never closed, because an unconfirmed community
  edge is weak evidence of removal and nobody stated an end date. Exempt: register
  edges (they retire facts properly via deltas, snapshot diffs and 0% amendments),
  pairs any register claim vouches for, and closed edges. The pass clears as well
  as sets, and the quality report counts stale edges per source.

## The OWNS property schema

`app/scraper/edge_schema.py` is the one place that knows what sits on an edge:
`OWNS_PROPS` (25 properties), `ROLE_PROPS`, `RELATED_TO_PROPS`. The merge paths
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

A Schedule 13D filed by several parties acting together is one **`voting_group`**
Entity, not a bloc percentage hung on whoever submitted the form. AB InBev's
52.3% sat on BRC S.à r.l. for exactly that reason, when nine parties vote it and
BRC merely filed.

```
(Stichting)─┐
(BRC)───────┤ RELATED_TO {relation:'group_member'}
(EPS)───────┼─▶ (Voting group — AB InBev)──OWNS──▶ (AB InBev)
(Lemann)────┘        type: voting_group        stake_percent: null
                                               voting_power_pct: 52.3
```

* **Only Schedule 13D.** A 13G reporting "shared voting power" is an asset
  manager aggregating across its own subsidiaries — State Street, Morgan Stanley
  — which is not a governance bloc. Modelling those would be misleading; on the
  dev graph it would have created 17 group nodes instead of ~4.
* **Membership is `RELATED_TO {relation:'group_member'}`**, following
  `_upsert_affiliate`, which already models 13F fund groups this way. Membership
  is not ownership, and no new edge type was needed.
* **The group's OWNS edge carries a null stake.** Its members hold the shares
  individually; adding a bloc percentage to theirs is what put companies over
  100% of themselves.
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

A voting group is **not a legal entity**: it holds no LEI and has no country, so
it is excluded from the quality report's identity ratio, country backfill,
name-based dedup, federation export, and `/entities/without-country`.

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

`manage.py backfill-countries` fills `country` where it is blank, from Wikidata
(batched P17, falling back to the headquarters' country) and SEC EDGAR. It only
ever fills a blank — an existing country is never overwritten.

Worth knowing why it was needed: subsidiaries and owners are written as *stubs*
when some other company is scraped, with no country of their own, so a company
that only ever appeared as an owner had none at all. BlackRock and The Vanguard
Group were both missing from the map for that reason. The Wikidata scraper now
fetches those countries during the scrape, so new stubs arrive with one.

For SEC filers the order is deliberate: **state of incorporation first, business
address only as a fallback.** EDGAR's business address is where the *filing* comes
from — DEUTSCHE BANK AKTIENGESELLSCHAFT lists New York — so trusting it would move
German banks to the United States. A filer with only a US address and no stated
incorporation is left blank, because a wrong country is worse than none.

The live scrapers now set a country themselves, so the pass is a repair for older
records rather than a routine step: the Wikidata scraper fetches related companies'
countries during a scrape, and the SEC scraper reads the filer's from EDGAR.

Jurisdiction and headquarters are kept in **separate fields throughout**. Wikidata's
P17 is a legal domicile and goes to `country`; the country of its P159 headquarters
goes to `hq_country`. Coalescing them is tempting when one is missing, and it is
what the Registered/Headquarters switch on the map exists to avoid — an early
version of the backfill did coalesce, and wrote Morgan, Grenfell & Co.'s London
headquarters into its jurisdiction field, which the source never claimed.

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
| `also_ultimate` | `true` when the ultimate relationship was stated for the same pair, i.e. this direct parent is also the top of the tree |
| `ultimate_since` / `ultimate_until` | the ultimate relationship's period, kept only when it **differs** from the direct one (6.9% of folded pairs — a parent can become the direct consolidator years before an intermediate holding dissolves and makes it the ultimate one) |

An ultimate record whose pair has no direct record keeps `direct_or_indirect =
indirect` and is *not* rewritten as direct: GLEIF never stated a direct holding
there, and that edge is often the only route to the company.

`mark-shortcuts` still runs, but only the genuinely multi-hop shortcuts are left
for it to prove.

The daily `gleif-update` delta maintains the same invariant: it looks its edges up
by **pair**, not by marker, so an ultimate-parent record for a folded pair updates
that edge instead of creating a parallel one. Retiring one of a folded edge's two
relationships does not close the edge — the other still stands, so the edge either
drops `also_ultimate` (the ultimate link ended) or reverts to `indirect` with its
own period (the direct holding ended).

Once the full copy is loaded, `manage.py gleif-update` applies GLEIF's published
**delta files** for all three sections — entities, relationships and reporting
exceptions (only records changed since the last publish) — as a fast daily
refresh — see [`app/scraper/gleif_incremental.py`](../backend/app/scraper/gleif_incremental.py).
It is **retirement-aware**: a relationship whose `RelationshipStatus` becomes
non-ACTIVE has its `OWNS` edge *closed* (`until` = the relationship period's
`EndDate`), and an entity whose `EntityStatus` is `INACTIVE` is flagged
`active=false` with its `gleif_registration_status` recorded — neither is ever
deleted (GLEIF never deletes; merges keep flowing through `SUCCEEDED_BY`). All
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
on `gb-coh:{number}`), so the 5.6M-row register never creates isolated company
nodes; former names land in `aliases` + `search_text`. Individuals become `Person`
nodes, corporate/legal PSCs become
`Entity` nodes (keyed on their own UK company number where present). `natures_of_control`
map to `stake_percent` (ownership-of-shares band floor), `voting_power_pct` (voting-rights
band floor) and `ownership_type` (`controlling` when they carry voting / appointment /
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

* **Ceased PSCs stay in the snapshot**, carrying `ceased_on` — 17.9% of records.
  A PSC's control ending is an in-record change, not a record disappearing.
* **`data.links.self`** identifies an *appointment* and is unique across the file,
  so each changed record maps to exactly one OWNS edge (via `psc_self_link`).
* **A snapshot is a complete state.** Diffing against a week-old digest yields a
  week's changes; there is no catch-up window to fall out of, so nothing here
  corresponds to GLEIF's `choose_catchup_interval`.

| the record | the graph |
|---|---|
| new | nodes upserted, edge created |
| changed | re-mapped and rewritten; `until` written **unconditionally**, so a correction that removes `ceased_on` reopens the edge |
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
