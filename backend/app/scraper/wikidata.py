"""
Wikidata scraper — company search and structured SPARQL fetch.

Data source:  https://www.wikidata.org
Manual lookup: https://www.wikidata.org/wiki/<QID>  (e.g. Q380 for Apple Inc.)

Endpoints used:
  Search:  GET https://www.wikidata.org/w/api.php
             ?action=wbsearchentities&search=<name>&language=en&type=item
  SPARQL:  GET https://query.wikidata.org/sparql?query=<SPARQL>&format=json
             Fetches basic info, subsidiaries, parent org, and CEO for a QID.

Fields returned and Owlgraph mapping:
  itemLabel        → entity.name
  itemDescription  → entity.description
  altLabel         → entity.aliases (skos:altLabel, English only)
  instance (P31)   → used to classify entity type (company / person / etc.)
  countryCode      → entity.country (primary ISO-2) + entity.countries (all
                     domiciles; dual-listed companies have >1)
  founded (P571)   → entity.founded_year
  revenue (P2139)  → entity.revenue_usd
  subsidiary (P355)→ OWNS edge (target entity)
  parent (P749)    → OWNS edge (source entity)
  ceo (P169)       → person node + HAS_ROLE edge (role="CEO")
  founder (P112)   → person node + HAS_ROLE edge (role="Founder")
  chairperson (P488)   → person node + HAS_ROLE edge (role="Chairman")
  board member (P3320) → person node + HAS_ROLE edge (role="Board Member")
  owned by (P127)  → OWNS edge (owner → this company; owner may be person or entity)
  headquarters (P159) + coordinate (P625) → primary entity.hq_lat/hq_lng/hq_city/
                     hq_country (city and country always agree) + entity.hq_locations
                     ("City|CC" for every HQ; dual-listed companies have >1)

Rate limits:
  Wikimedia policy: no hard public limit, but requests must include a User-Agent
  and should be polite. Every call goes through `_wd_get`, which spaces calls (0.4 s)
  and, on a 429/503 rate-limit, waits per `Retry-After` (else exponential backoff with
  jitter) and retries — so a burst (a deep scrape, or concurrent on-demand scrapes on
  one Render egress IP) recovers instead of failing.
  Docs: https://www.wikidata.org/wiki/Wikidata:Data_access#Rate_limits

Data licence:
  CC0 1.0 Universal (public domain dedication).
  https://creativecommons.org/publicdomain/zero/1.0/

How to verify:
  1. Open https://www.wikidata.org/wiki/<QID> in a browser.
  2. Compare P31 (instance of), P17 (country), P355 (subsidiaries), P169 (CEO)
     with the values returned by fetch_company_data().
  3. Run the SPARQL query directly at https://query.wikidata.org/ to inspect raw rows.
"""

import logging
import random
import re
import time
import unicodedata
import httpx
from functools import lru_cache

from app.models.relationship import RoleType

log = logging.getLogger(__name__)

WIKIDATA_API  = "https://www.wikidata.org/w/api.php"
SPARQL_URL    = "https://query.wikidata.org/sparql"
USER_AGENT    = "Owlgraph/1.0 (https://pamten-frontend.onrender.com)"
HEADERS       = {"User-Agent": USER_AGENT}
REQUEST_DELAY = 0.4   # polite spacing between Wikidata calls
_MAX_RETRIES  = 4     # retries on a transient rate-limit / gateway error
_MAX_BACKOFF  = 30.0  # cap on the backoff wait (seconds)
# Transient statuses worth retrying: 429 rate-limit, plus 502/503/504 — query.wikidata.org
# sits behind a proxy that intermittently returns Bad Gateway / Service Unavailable /
# Gateway Timeout under load; a short backoff-and-retry clears them.
_RETRY_STATUS = frozenset({429, 502, 503, 504})


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds to wait per the `Retry-After` response header, if present + numeric."""
    try:
        val = resp.headers.get("Retry-After")
        return float(val) if val else None
    except (AttributeError, TypeError, ValueError):
        return None


def _wd_get(url: str, params: dict, timeout: int) -> httpx.Response:
    """GET a Wikidata endpoint politely (REQUEST_DELAY between calls) and resiliently.

    query.wikidata.org rate-limits per IP and answers 429 (or 503) when a burst — e.g. a
    deep scrape's many SPARQL queries, or concurrent on-demand scrapes sharing one egress
    IP — exceeds its budget, and its fronting proxy intermittently returns 502/504 under
    load. On any of these (see `_RETRY_STATUS`) we wait per the `Retry-After` header (else
    an exponential backoff with jitter, capped) and retry up to `_MAX_RETRIES`, instead of
    failing the query. Any other status raises; the final transient status raises if
    retries run out."""
    delay = REQUEST_DELAY
    for attempt in range(_MAX_RETRIES + 1):
        time.sleep(delay)
        r = httpx.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code not in _RETRY_STATUS:
            r.raise_for_status()
            return r
        if attempt >= _MAX_RETRIES:
            r.raise_for_status()          # exhausted → surface the transient error
        delay = _retry_after(r) or min(2.0 ** attempt, _MAX_BACKOFF) + random.uniform(0, 0.5)
        log.warning("Wikidata transient error (HTTP %s); backing off %.1fs (retry %d/%d)",
                    r.status_code, delay, attempt + 1, _MAX_RETRIES)
    raise RuntimeError("unreachable")     # pragma: no cover


def search_entity(query: str, limit: int = 5) -> list:
    """Full-text search on Wikidata. Returns list of {id, label, description}."""
    r = _wd_get(WIKIDATA_API, {
        "action":   "wbsearchentities",
        "search":   query,
        "language": "en",
        "type":     "item",
        "limit":    limit,
        "format":   "json",
    }, timeout=10)
    return r.json().get("search", [])


# ── Searching inside one country ──────────────────────────────────────────────
#
# `search_entity` above asks Wikidata for the best "Alphabet" in the world, and
# gets the one in Mountain View. Filtering that answer afterwards cannot find
# Alphabet Fuhrparkmanagement, the German company of that name — it is nowhere
# near the global top few. The country has to be part of the question.
#
# `haswbstatement:P17=Q183` does that: the search index is asked only for items
# whose country IS Germany. `inlabel:` keeps the text match on labels and aliases
# rather than descriptions, which otherwise drags in every item that merely
# mentions the word.
#
# The consequence, and it is deliberate: an item with no `P17` at all cannot be
# found this way. Asked for a company in Germany, "we do not know where this is"
# is not an answer.


def _fold(name: str) -> str:
    """A name reduced to what a comparison should care about: no accents, no
    legal suffix, lower case. 'Nestlé S.A.' and 'nestle' land in the same place."""
    from app.scraper.mapper import normalize_entity_name

    stripped = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(c for c in stripped if not unicodedata.combining(c))
    return (normalize_entity_name(ascii_only) or ascii_only.strip().lower())


@lru_cache(maxsize=512)
def country_item(iso2: str) -> str | None:
    """The Wikidata item for an ISO-2 country code, e.g. 'DE' → 'Q183'.

    Looked up through P297 (ISO 3166-1 alpha-2) rather than hardcoded, and cached
    for the life of the process: it is the same handful of countries all day, and
    the answer does not change.
    """
    code = (iso2 or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    r = _wd_get(WIKIDATA_API, {
        "action": "query", "list": "search", "srsearch": f"haswbstatement:P297={code}",
        "srnamespace": 0, "srlimit": 1, "format": "json",
    }, timeout=10)
    hits = r.json().get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def _names_for(qids: list[str]) -> dict[str, list[str]]:
    """Every English name an item goes by — label first, then aliases — for up to
    50 items in one call.

    Aliases matter: a search for "DTAG" reaches Deutsche Telekom through one, and
    judging that hit on its label alone would throw it away.
    """
    if not qids:
        return {}
    r = _wd_get(WIKIDATA_API, {
        "action": "wbgetentities", "ids": "|".join(qids[:50]),
        "props": "labels|aliases", "languages": "en", "format": "json",
    }, timeout=15)
    out: dict[str, list[str]] = {}
    for qid, ent in (r.json().get("entities") or {}).items():
        label = ((ent.get("labels") or {}).get("en") or {}).get("value") or ""
        aliases = [a.get("value", "") for a in ((ent.get("aliases") or {}).get("en") or [])]
        out[qid] = [n for n in [label, *aliases] if n]
    return out


def rank_by_name(candidates: list[dict], query: str) -> list[dict]:
    """Order candidates by how well the NAME matches: exact, starts-with, contains,
    then the search engine's own order — shorter names first within a tier.

    The country-restricted search ranks by text relevance across the whole item,
    so "barclays" in the United Kingdom comes back with the Premier League first:
    it was the Barclays Premier League for years and the alias is still there.
    Ranking on the label puts the bank back on top. Same shape as `_rank` in
    routers/search.py, for the same reason.

    Names are compared folded — accents stripped, legal suffix dropped — so
    "Nestle" matches "Nestlé" exactly instead of losing to "Nestle Nido", and
    "Alphabet" matches "Alphabet Inc." exactly instead of merely starting it.
    """
    return sorted(candidates, key=lambda item: _sort_key(item, query))


def _sort_key(item: dict, query: str) -> tuple:
    """Best match over all names, then whether the LABEL is what matched, then the
    matched name's length, then the search engine's order.

    The label tiebreak earns its place on "Deutsche Telekom" in Germany: the
    cycling team Wikidata labels "T-Mobile" carries "Deutsche Telekom" as an alias
    — it raced under that name — so it ties the telco exactly and, being first in
    the search order, won. An item actually *called* what you typed beats one that
    merely answers to it.
    """
    tier, length = best_match(item, query)
    label_tier, _ = best_match({"names": [item.get("label") or ""]}, query)
    return (tier, label_tier, length, item.get("order", 0))


def name_tier(item: dict, query: str) -> int:
    """How well one of an item's names matches the query: 0 exact, 1 starts-with,
    2 contains, 3 not really. The best of its label and aliases."""
    return best_match(item, query)[0]


def best_match(item: dict, query: str) -> tuple[int, int]:
    """`(tier, length)` of the name that matches the query best.

    The length is of the *matching* name, not the label. Judging the match on an
    alias and then ranking on the label lets a short unrelated title win: an Azure
    cloud region labelled "westindia" carries "Microsoft Azure West India" as an
    alias, and a nine-character label beat the fifteen of "Microsoft India".
    """
    q = _fold(query)
    best = (3, 0)
    for name in item.get("names") or [item.get("label") or ""]:
        n = _fold(name)
        tier = 0 if n == q else 1 if n.startswith(q) else 2 if q in n else 3
        best = min(best, (tier, len(n)))
    return best


def search_entity_in_country(query: str, iso2: str, limit: int = 8) -> list:
    """Label search for `query`, restricted **at Wikidata** to items in `iso2`.

    Returns `[{id, label}]`, best first, or `[]` when that country has nothing by
    that name — which is a real answer, not a failure to look.
    """
    qid = country_item(iso2)
    if not qid:
        log.warning("Wikidata: no country item for %r — cannot restrict the search", iso2)
        return []
    r = _wd_get(WIKIDATA_API, {
        "action": "query", "list": "search",
        "srsearch": f'inlabel:"{query}" haswbstatement:P17={qid}',
        "srnamespace": 0, "srlimit": limit, "format": "json",
    }, timeout=15)
    ids = [h["title"] for h in r.json().get("query", {}).get("search", [])]
    if not ids:
        return []
    names = _names_for(ids)
    candidates = [{"id": qid_, "names": names.get(qid_, []),
                   "label": (names.get(qid_) or [""])[0], "order": i}
                  for i, qid_ in enumerate(ids)]

    # The item has to actually be called what the user typed. Restricted to one
    # country the search reaches much deeper than a global one, so when a country
    # has no company by that name it starts offering whatever it does have:
    # "Alphabet" in France comes back with a breast-cancer trial whose acronym is
    # ALPHABET. Requiring the query to *begin* one of the item's names keeps
    # Alphabet Fuhrparkmanagement, Alphabet Brewing Company and Nestlé, and drops
    # that. Nothing left means nothing there — which is a true answer.
    named = [c for c in candidates if name_tier(c, query) <= 1]
    return rank_by_name(named, query)


#: Languages the label service may fall back through, in order of preference.
#:
#: `mul` is the one that matters and the one that is easy to miss. Wikidata added
#: it in 2024 for labels that are identical in every language — which is exactly
#: what a personal name is — so newer person items increasingly carry their label
#: ONLY in `mul` and have no `en` label at all. HashiCorp's CEO is one: labelled
#: in de and mul, not en.
#:
#: That matters because of how the label service fails. Asked for a language the
#: item does not have, it does not return nothing — it returns **the QID as the
#: label**. So `?ceoLabel` came back as the string "Q132983199" and was stored as
#: a person's name, which then also poisons search_text and name_normalized, so
#: the record cannot be found by the name it should have had.
#:
#: The rest of the chain covers registers we actually import from. It is not
#: exhaustive and cannot be — see `_label()` for the backstop.
_LABEL_LANGUAGES = "en,mul,de,fr,es,it,nl,pt,sv,da,fi,pl,[AUTO_LANGUAGE]"
_LABEL_SERVICE = (
    f'SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{_LABEL_LANGUAGES}" }}')


def _sparql(qid: str) -> list:
    """
    Run the three targeted SPARQL queries for a QID and return their combined
    result rows.

    A single query joining every multi-valued property (aliases × instances ×
    countries × HQs × subsidiaries × people × owners) is a cartesian product
    that explodes for large companies — Unilever produced a 140 MB response.
    Splitting by concern keeps each query bounded; _aggregate reads fields
    per-row, so the combined rows aggregate correctly.
    """
    # 1. Core: identity, aliases, instances, all domicile countries, all HQs.
    core = f"""
    SELECT ?itemLabel ?itemDescription ?altLabel ?instance ?countryCode
           ?founded ?revenue ?itemCoord ?hqLabel ?hqCoord ?hqCountryCode
           ?lei ?cik ?website
    WHERE {{
      BIND(wd:{qid} AS ?item)
      OPTIONAL {{ ?item skos:altLabel ?altLabel . FILTER(LANG(?altLabel) = "en") }}
      OPTIONAL {{ ?item wdt:P31 ?instance }}
      # Hard external identifiers — the bridge to the register-sourced nodes.
      # A GLEIF entity and its Wikidata counterpart otherwise share nothing a
      # merge can key on: GLEIF supplies lei_id, Wikidata supplies wikidata_id,
      # and the dedup only calls a group definitive when two members carry the
      # SAME id. SEC's own LEI field is null for most operating companies
      # (Microsoft included), so Wikidata's P1278 is the bridge that exists.
      OPTIONAL {{ ?item wdt:P1278 ?lei }}
      OPTIONAL {{ ?item wdt:P5531 ?cik }}
      # P856 = official website. NOT single-valued in practice — Apple carries
      # 100+ regional storefronts at equal rank, which multiplied core rows
      # (the P1128 hazard) and made the picked URL arbitrary (apple.com/za).
      # The uncorrelated subselect returns AT MOST ONE row: preferred rank
      # first, then the shortest URL, which is the root domain in every
      # multi-storefront case seen.
      OPTIONAL {{
        {{ SELECT ?website WHERE {{
            wd:{qid} p:P856 ?wsStmt .
            ?wsStmt ps:P856 ?website ; wikibase:rank ?wsRank .
            FILTER(?wsRank != wikibase:DeprecatedRank)
            BIND(IF(?wsRank = wikibase:PreferredRank, 0, 1) AS ?wsOrd)
          }} ORDER BY ASC(?wsOrd) ASC(STRLEN(STR(?website))) LIMIT 1 }}
      }}
      OPTIONAL {{ ?item wdt:P17 ?country . ?country wdt:P297 ?countryCode }}
      OPTIONAL {{ ?item wdt:P625 ?itemCoord }}
      OPTIONAL {{
        ?item wdt:P159 ?hq .
        OPTIONAL {{ ?hq wdt:P625 ?hqCoord }}
        OPTIONAL {{ ?hq wdt:P17 ?hqCountry . ?hqCountry wdt:P297 ?hqCountryCode }}
      }}
      OPTIONAL {{ ?item wdt:P571 ?founded }}
      OPTIONAL {{ ?item wdt:P2139 ?revenue . FILTER(?revenue > 0) }}
      {_LABEL_SERVICE}
    }}
    """
    # 2. People: CEO / founder / chair / board — UNION so one person per row.
    people = f"""
    SELECT ?ceo ?ceoLabel ?ceoDescription ?ceoNationalityCode ?ceoStart ?ceoEnd
           ?founder ?founderLabel ?founderStart ?founderEnd
           ?chair ?chairLabel ?chairStart ?chairEnd
           ?board ?boardLabel ?boardStart ?boardEnd
    WHERE {{
      BIND(wd:{qid} AS ?item)
      {{
        ?item p:P169 ?ceoStmt . ?ceoStmt ps:P169 ?ceo .
        OPTIONAL {{ ?ceoStmt pq:P580 ?ceoStart }}
        OPTIONAL {{ ?ceoStmt pq:P582 ?ceoEnd }}
        OPTIONAL {{ ?ceo wdt:P27 ?ceoNationality . ?ceoNationality wdt:P297 ?ceoNationalityCode }}
        OPTIONAL {{ ?ceo schema:description ?ceoDescription . FILTER(LANG(?ceoDescription) = "en") }}
      }}
      UNION {{ ?item p:P112 ?founderStmt . ?founderStmt ps:P112 ?founder .
               OPTIONAL {{ ?founderStmt pq:P580 ?founderStart }}
               OPTIONAL {{ ?founderStmt pq:P582 ?founderEnd }} }}
      UNION {{ ?item p:P488 ?chairStmt . ?chairStmt ps:P488 ?chair .
               OPTIONAL {{ ?chairStmt pq:P580 ?chairStart }}
               OPTIONAL {{ ?chairStmt pq:P582 ?chairEnd }} }}
      UNION {{ ?item p:P3320 ?boardStmt . ?boardStmt ps:P3320 ?board .
               OPTIONAL {{ ?boardStmt pq:P580 ?boardStart }}
               OPTIONAL {{ ?boardStmt pq:P582 ?boardEnd }} }}
      {_LABEL_SERVICE}
    }}
    """
    # 3. Relations: subsidiaries, parent, owners, succession (replaced-by / replaces).
    relations = f"""
    SELECT ?subsidiary ?subsidiaryLabel ?subsidiaryInstance ?parent
           ?owner ?ownerLabel ?ownerInstance
           ?successor ?successorLabel ?successorDate
           ?predecessor ?predecessorLabel ?predecessorDate
    WHERE {{
      BIND(wd:{qid} AS ?item)
      OPTIONAL {{ ?item wdt:P355 ?subsidiary . OPTIONAL {{ ?subsidiary wdt:P31 ?subsidiaryInstance }} }}
      OPTIONAL {{ ?item wdt:P749 ?parent }}
      OPTIONAL {{ ?item wdt:P127 ?owner . OPTIONAL {{ ?owner wdt:P31 ?ownerInstance }} }}
      # Succession — read the full statement so the P585 point-in-time qualifier
      # (when the rename/merger took effect) can be attached to the edge.
      OPTIONAL {{ ?item p:P1366 ?succStmt . ?succStmt ps:P1366 ?successor .   # replaced by (this → successor)
                  OPTIONAL {{ ?succStmt pq:P585 ?successorDate }} }}
      OPTIONAL {{ ?item p:P1365 ?predStmt . ?predStmt ps:P1365 ?predecessor . # replaces (predecessor → this)
                  OPTIONAL {{ ?predStmt pq:P585 ?predecessorDate }} }}
      {_LABEL_SERVICE}
    }}
    """
    # 4. Employees (P1128) — the latest statement by point-in-time (P585).
    # P1128 usually has one statement per year; take the most recent value and
    # keep its as-of year for provenance. Its own query so the yearly statements
    # don't multiply the core query's rows.
    employees = f"""
    SELECT ?employees ?employeesAsOf
    WHERE {{
      wd:{qid} p:P1128 ?empStmt .
      ?empStmt ps:P1128 ?employees .
      OPTIONAL {{ ?empStmt pq:P585 ?employeesAsOf }}
    }}
    ORDER BY DESC(?employeesAsOf)
    LIMIT 1
    """
    rows: list = []
    # core/people/relations are essential — a failure there fails the scrape.
    for query in (core, people, relations):
        r = _wd_get(SPARQL_URL, {"query": query, "format": "json"}, timeout=30)
        rows.extend(r.json()["results"]["bindings"])
    # Employees is supplementary: a flaky 4th request must NOT abort the whole
    # company scrape — just skip the field on error.
    try:
        r = _wd_get(SPARQL_URL, {"query": employees, "format": "json"}, timeout=30)
        rows.extend(r.json()["results"]["bindings"])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("Wikidata employees query failed for %s (skipping field): %s", qid, exc)
    return rows



# ── Scraping a person ─────────────────────────────────────────────────────────
#
# The company scrape reads a company and finds its people. This is the other
# direction: a person, and the companies they run, founded or own. Wikidata models
# it only from the company side (P169 CEO, P112 founder, P488 chairperson, P3320
# board member, P127 owner), so the query is a reverse lookup — every item that
# points at this person through one of those.

#: Marks the one link that is ownership rather than a job. The caller turns it
#: into an OWNS edge; everything else becomes HAS_ROLE.
OWNER_ROLE = "owner"

#: Wikidata property -> the role name this graph stores.
#:
#: Taken from `RoleType` rather than written out. The first version spelled one of
#: them "Board member", the company-side scrape writes "Board Member", and the two
#: strings are two different edges — which is exactly how a person ended up listed
#: twice on the same board.
PERSON_LINK_PROPS = {
    "P169": RoleType.ceo.value,
    "P112": RoleType.founder.value,
    "P488": RoleType.chairman.value,
    "P3320": RoleType.board_member.value,
    "P127": OWNER_ROLE,
}

#: Signals that an item is an actual company rather than something a person
#: merely "founded". Wikidata's founder property is used loosely: Larry Page is
#: recorded as founder of Googleplex (a building), Google Summer of Code (a
#: programme) and Google Photos (software), and Elon Musk of a school, a
#: political party, a supercomputer, his own Tesla Roadster and an aeroplane.
#: A legal form, an industry, an LEI or a stock listing is something none of
#: those has and every real company does.
COMPANY_SIGNAL_PROPS = ("P1454", "P452", "P1278", "P414")


def looks_like_a_company(instances: list[str], signal_count: int) -> bool:
    """Whether a person's link target belongs in an ownership graph.

    A recognised organisation type, or any of the company signals above. Both
    are needed: the type table is 24 QIDs and misses "limited liability company"
    (H211, LLC), while signals alone would miss a company with a sparse item.
    """
    from app.scraper.mapper import INSTANCE_TYPE_MAP

    return bool(signal_count) or any(i in INSTANCE_TYPE_MAP for i in instances)



def classify_candidates(qids: list[str]) -> dict[str, dict]:
    """For each candidate: what it is an instance of, whether it is a human, and
    how many company signals it carries — in one query.

    Needed because a name is not a kind. Searching "Steve Jobs" returns the 2015
    film first, the book second and the man third; searching "Larry Page" returns
    the man first and a British singer second. Whichever path is asking has to
    pick its own kind out of that list rather than trust the ranking.
    """
    if not qids:
        return {}
    values = " ".join(f"wd:{q}" for q in qids)
    signals = "\n".join(f"      OPTIONAL {{ ?item wdt:{p} ?sig{i} }}"
                         for i, p in enumerate(COMPANY_SIGNAL_PROPS))
    counts = " ".join(f"(COUNT(DISTINCT ?sig{i}) AS ?n{i})" for i in range(len(COMPANY_SIGNAL_PROPS)))
    query = f"""
    SELECT ?item (GROUP_CONCAT(DISTINCT ?inst; separator="|") AS ?instances) {counts} WHERE {{
      VALUES ?item {{ {values} }}
      OPTIONAL {{ ?item wdt:P31 ?i . BIND(STRAFTER(STR(?i), "entity/") AS ?inst) }}
{signals}
    }} GROUP BY ?item
    """
    r = _wd_get(SPARQL_URL, {"query": query, "format": "json"}, timeout=45)
    out: dict[str, dict] = {}
    for row in r.json()["results"]["bindings"]:
        qid = _qid(_v(row, "item"))
        instances = [i for i in (_v(row, "instances") or "").split("|") if i]
        signal_count = sum(int(_v(row, f"n{i}") or 0) for i in range(len(COMPANY_SIGNAL_PROPS)))
        out[qid] = {
            "instances": instances,
            "is_human": "Q5" in instances,
            "is_company": looks_like_a_company(instances, signal_count),
        }
    return out


def pick_candidate(results: list, kind: str) -> str | None:
    """The first search hit that is the kind being asked for.

    `kind` is "company" or "person". Returns None when none of them is — which is
    a real answer: "Steve Jobs" has no company by that name, and inventing one
    from the film is how a Danny Boyle picture ended up in an ownership graph.
    """
    qids = [r["id"] for r in results][:5]
    if not qids:
        return None
    facts = classify_candidates(qids)
    want = "is_human" if kind == "person" else "is_company"
    for qid in qids:
        if facts.get(qid, {}).get(want):
            return qid
    return None

def fetch_person_details_for(qid: str) -> dict | None:
    """Everything needed to write one person: their name and description, plus the
    dates, birthplace, nationalities and aliases `_fetch_person_details` collects.

    Returns None when the item is not a human. `is_human` is left None by the
    detail query when the item states no P31 at all, and that counts as "not
    confirmed" here — writing an unconfirmed item as a Person is how a company
    ends up in the person shape, which is the mirror of the bug this whole path
    exists to fix.
    """
    detail = dict(_fetch_person_details({qid}).get(qid) or {})
    if detail.get("is_human") is not True:
        return None

    # Ask for `mul` as well as `en`, and it is not a nicety: Wikidata added `mul`
    # in 2024 for labels identical in every language — which is exactly what a
    # personal name is — and newer or edited person items increasingly carry
    # their label ONLY there. Steve Jobs is one. Asking for English alone
    # returned nothing and made him look like not-a-person.
    langs = "en|mul|de|fr|es|it|nl|pt|sv|da|fi|pl"
    r = _wd_get(WIKIDATA_API, {
        "action": "wbgetentities", "ids": qid, "props": "labels|descriptions",
        "languages": langs, "languagefallback": 1, "format": "json",
    }, timeout=15)
    ent = (r.json().get("entities") or {}).get(qid) or {}

    def _first(block: dict) -> str:
        for lang in langs.split("|"):
            value = (block or {}).get(lang, {}).get("value")
            if value:
                return value
        return ""

    detail["full_name"] = _first(ent.get("labels") or {})
    detail["description"] = _first(ent.get("descriptions") or {})
    # A missing label does not make somebody not a person. `is_human` decides
    # that; the caller has the name from the search hit and falls back to it.
    return detail


def fetch_person_companies(qid: str, limit: int = 60) -> list[dict]:
    """The companies a person leads, founded or owns, in one query.

    Returns `[{qid, name, country, roles, instances, is_company}]`. Aggregated
    with GROUP_CONCAT so a company with three roles and four P31s is one row
    rather than twelve, and without `P31/P279*` subclass traversal, which is what
    makes WDQS time out (see `search_entity_in_country` for the same lesson).
    """
    values = " ".join(f'(wdt:{p} "{r}")' for p, r in PERSON_LINK_PROPS.items())
    optionals = "\n".join(
        f"  OPTIONAL {{ ?company wdt:{p} ?sig{i} }}" for i, p in enumerate(COMPANY_SIGNAL_PROPS))
    counts = " ".join(f"(COUNT(DISTINCT ?sig{i}) AS ?n{i})" for i in range(len(COMPANY_SIGNAL_PROPS)))
    query = f"""
    SELECT ?company ?companyLabel ?countryCode
           (GROUP_CONCAT(DISTINCT ?role; separator="|") AS ?roles)
           (GROUP_CONCAT(DISTINCT ?inst; separator="|") AS ?instances)
           {counts} WHERE {{
      VALUES (?prop ?role) {{ {values} }}
      ?company ?prop wd:{qid} .
      OPTIONAL {{ ?company wdt:P31 ?i . BIND(STRAFTER(STR(?i), "entity/") AS ?inst) }}
      OPTIONAL {{ ?company wdt:P17 ?c . ?c wdt:P297 ?countryCode }}
{optionals}
      {_LABEL_SERVICE}
    }} GROUP BY ?company ?companyLabel ?countryCode LIMIT {int(limit)}
    """
    r = _wd_get(SPARQL_URL, {"query": query, "format": "json"}, timeout=60)
    out = []
    for row in r.json()["results"]["bindings"]:
        target = row["company"]["value"].rsplit("/", 1)[-1]
        name = _v(row, "companyLabel")
        if not name or name == target:          # label service fell back to the QID
            continue
        instances = [i for i in (_v(row, "instances") or "").split("|") if i]
        signals = sum(int(_v(row, f"n{i}") or 0) for i in range(len(COMPANY_SIGNAL_PROPS)))
        out.append({
            "qid": target,
            "name": name,
            "country": _v(row, "countryCode") or None,
            "roles": [r_ for r_ in (_v(row, "roles") or "").split("|") if r_],
            "instances": instances,
            "is_company": looks_like_a_company(instances, signals),
        })
    return out

def _fetch_person_details(qids: set[str]) -> dict[str, dict]:
    """
    Fetch per-person detail — date of birth (P569) / death (P570), place of birth
    (P19), nationalities (P27) and aliases ("also known as") — for a set of
    Wikidata person QIDs in ONE query.

    GROUP_CONCAT collapses the multi-valued nationality/alias props into a single
    row per person, so the response can't blow up combinatorially. (Joining many
    multi-valued props for a company in a single query is exactly what exploded
    the Unilever scrape — person detail is kept split out and pre-aggregated.)
    """
    if not qids:
        return {}
    values = " ".join(f"wd:{q}" for q in sorted(qids))
    query = f"""
    SELECT ?person ?birth ?death
           (SAMPLE(?bpLabel) AS ?birthPlace)
           (GROUP_CONCAT(DISTINCT ?natCode;  separator="|") AS ?nats)
           (GROUP_CONCAT(DISTINCT ?alias;    separator="|") AS ?aliases)
           (GROUP_CONCAT(DISTINCT ?instance; separator="|") AS ?instances)
    WHERE {{
      VALUES ?person {{ {values} }}
      OPTIONAL {{ ?person wdt:P569 ?birth }}
      OPTIONAL {{ ?person wdt:P570 ?death }}
      OPTIONAL {{ ?person wdt:P19 ?bp . ?bp rdfs:label ?bpLabel . FILTER(LANG(?bpLabel) = "en") }}
      OPTIONAL {{ ?person wdt:P27 ?nat . ?nat wdt:P297 ?natCode }}
      OPTIONAL {{ ?person skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
      OPTIONAL {{ ?person wdt:P31 ?instance }}
    }}
    GROUP BY ?person ?birth ?death
    """
    r = _wd_get(SPARQL_URL, {"query": query, "format": "json"}, timeout=30)
    details: dict[str, dict] = {}
    for row in r.json()["results"]["bindings"]:
        pqid = _qid(_v(row, "person"))
        if not pqid or pqid in details:
            continue
        nats      = [c for c in (_v(row, "nats")    or "").split("|") if c]
        aliases   = [a for a in (_v(row, "aliases") or "").split("|") if a]
        instances = [_qid(u) for u in (_v(row, "instances") or "").split("|") if u]
        # Humans are instance-of Q5. If P31 is present but lacks Q5, it's an org
        # wrongly listed in a person slot (e.g. a company as a subsidiary's
        # "founder") — flag it so the runner won't create a Person node. Unknown
        # (no P31 returned) stays None → treated as a person.
        is_human = ("Q5" in instances) if instances else None
        details[pqid] = {
            "birth_date":    (_v(row, "birth") or "")[:10] or None,
            "death_date":    (_v(row, "death") or "")[:10] or None,
            "birth_place":   _v(row, "birthPlace") or None,
            "nationalities": nats,
            "aliases":       aliases,
            "is_human":      is_human,
        }
    return details


def countries_for(qids: set[str]) -> dict[str, dict]:
    """Jurisdiction and headquarters country for related-company QIDs, in ONE query.

    Subsidiaries and owners are written as stubs when some *other* company is
    scraped, with no country of their own — so a company that only ever appears as
    an owner never gets one. That is why BlackRock and The Vanguard Group, two of
    the most significant owners in the graph, were absent from the map entirely.

    Returns ``{qid: {"country": ISO2 | None, "hq_country": ISO2 | None}}``.

    The two are kept apart rather than coalesced. P17 is where the company belongs
    legally and belongs in `country`; P159's country is where it is *run* and
    belongs in `hq_country`. Collapsing them into one value is precisely the blur
    the map's Registered/Headquarters switch exists to avoid — an earlier version
    of this function did coalesce, and wrote one company's headquarters country
    into its jurisdiction field.

    Batched and pre-aggregated for the same reason as _fetch_person_details: one
    row per company, no combinatorial blow-up.
    """
    if not qids:
        return {}
    values = " ".join(f"wd:{q}" for q in sorted(qids))
    query = f"""
    SELECT ?item (SAMPLE(?directCode) AS ?country) (SAMPLE(?hqCode) AS ?hq) WHERE {{
      VALUES ?item {{ {values} }}
      OPTIONAL {{ ?item wdt:P17 ?c . ?c wdt:P297 ?directCode }}
      OPTIONAL {{ ?item wdt:P159 ?h . ?h wdt:P17 ?hc . ?hc wdt:P297 ?hqCode }}
    }} GROUP BY ?item
    """
    r = _wd_get(SPARQL_URL, {"query": query, "format": "json"}, timeout=30)
    out: dict[str, dict] = {}
    for row in r.json()["results"]["bindings"]:
        qid = _qid(_v(row, "item"))
        if not qid:
            continue
        def code(key: str) -> str | None:
            v = (_v(row, key) or "").strip().upper()
            return v if len(v) == 2 else None
        country, hq = code("country"), code("hq")
        if country or hq:
            out[qid] = {"country": country, "hq_country": hq}
    return out


def fetch_company_data(qid: str) -> dict | None:
    """
    Fetch a company's data from Wikidata: identity, all domicile countries and
    HQs, subsidiaries, parent, owners, and key people. Returns a structured
    dict or None if no results.
    """
    data = _aggregate(qid, _sparql(qid))
    if not data:
        return None

    # Enrich the people (CEOs, founders/chair/board, person-owners) with birth /
    # death date, nationalities and aliases in one further bounded query.
    person_qids = {p["qid"] for p in data["ceos"]     if p.get("qid")}
    person_qids |= {p["qid"] for p in data["officers"] if p.get("qid")}
    person_qids |= {o["qid"] for o in data["owners"]
                    if o.get("qid") and "Q5" in o.get("instances", [])}
    # Countries for the companies we are about to create as stubs, so an owner or
    # subsidiary is not written with country=None and then never revisited.
    company_qids = {c["qid"] for c in data.get("subsidiaries", []) if c.get("qid")}
    company_qids |= {o["qid"] for o in data.get("owners", [])
                     if o.get("qid") and "Q5" not in o.get("instances", [])}
    countries = countries_for(company_qids)
    for group in ("subsidiaries", "owners"):
        for item in data.get(group, []) or []:
            if found := countries.get(item.get("qid")):
                item["country"] = found["country"]
                item["hq_country"] = found["hq_country"]

    details = _fetch_person_details(person_qids)
    if details:
        for group in (data["ceos"], data["officers"], data["owners"]):
            for person in group:
                if extra := details.get(person.get("qid")):
                    person.update(extra)
    return data


def _v(row: dict, key: str) -> str | None:
    return row.get(key, {}).get("value")


#: A bare Q-number, which is what the label service returns when it has no label
#: in any requested language.
_BARE_QID = re.compile(r"^Q\d+$")


def _label(row: dict, key: str) -> str | None:
    """A label from a SPARQL row, or None if Wikidata had none to give.

    The backstop behind `_LABEL_LANGUAGES`. No fallback chain can be complete —
    LINKEDIN FRANCE SAS is labelled only in French, and something will always be
    labelled only in Czech — so rather than widening the list forever, refuse the
    QID itself as a name.

    Callers already skip records with no label, which is the right outcome: an
    officer we cannot name is better left out than recorded as "Q132983199". A
    fake name is worse than a gap, because it looks like data.
    """
    value = _v(row, key)
    return None if value and _BARE_QID.match(value) else value


def _qid(uri: str | None) -> str | None:
    """Extract Q-id from a Wikidata entity URI."""
    if not uri:
        return None
    return uri.rstrip("/").split("/")[-1]


def _parse_point(wkt: str | None) -> tuple[float, float] | None:
    """
    Parse a Wikidata P625 WKT literal into (latitude, longitude).

    WKT stores coordinates as 'Point(<longitude> <latitude>)', so the order is
    swapped on the way out.
    """
    if not wkt:
        return None
    m = re.match(r"\s*Point\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", wkt, re.IGNORECASE)
    if not m:
        return None
    try:
        lng, lat = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    return (lat, lng)


def normalize_lei(raw: str | None) -> str | None:
    """An ISO 17442 LEI (20 alphanumerics, upper case), or None if it isn't one.

    Wikidata is crowd-edited, so P1278 occasionally holds a truncated code or a
    stray note. A malformed value is worse than no value here: `lei_id` is a
    merge key, and a wrong one would fold two unrelated companies together.
    """
    if not raw:
        return None
    lei = raw.strip().upper()
    return lei if len(lei) == 20 and lei.isalnum() else None


def normalize_url(raw: str | None) -> str | None:
    """A displayable http(s) URL, or None — crowd-edited values get no trust.

    Same reasoning as the id normalisers above: a wrong value is worse than
    none. Here the failure mode is sharper still — this string becomes an <a
    href> in the panel, so anything that is not plainly http/https (a
    javascript: URI, a bare host, stray markup) is rejected rather than
    repaired. Guessing a scheme would assert something the source did not say.
    """
    if not raw:
        return None
    url = raw.strip()
    if url.lower().startswith(("http://", "https://")) and " " not in url:
        return url
    return None


def normalize_cik(raw: str | None) -> str | None:
    """A CIK zero-padded to 10 digits, matching how SEC EDGAR reports it.

    Wikidata's P5531 is sometimes padded ("0000789019") and sometimes not
    ("789019"). Storing the unpadded form would silently break the match against
    an EDGAR-sourced node, which always stores the padded one.
    """
    if not raw:
        return None
    digits = raw.strip().lstrip("#").strip()
    return digits.zfill(10) if digits.isdigit() and len(digits) <= 10 else None


def _aggregate(qid: str, rows: list) -> dict | None:
    if not rows:
        return None

    result = {
        "qid":         qid,
        "name":        None,
        "description": None,
        "aliases":     set(),
        "instances":   set(),
        "country":     None,   # primary domicile (first P17) — used for grouping/map
        "countries":   set(),  # all P17 domiciles (dual-listed companies have >1)
        "founded":     None,
        "revenue":     None,
        "lei":         None,   # P1278 — bridges to a GLEIF node's lei_id
        "sec_cik":     None,   # P5531 — bridges to a SEC EDGAR node's sec_cik
        "website":     None,   # P856 — the company's official site
        "employees":   None,   # latest P1128 value
        "employees_as_of": None,  # year of that value (P585 qualifier)
        "subsidiaries": {},
        "parents":     set(),
        "successors":   {},  # replaced by (P1366): this entity → successor (SUCCEEDED_BY)
        "predecessors": {},  # replaces (P1365): predecessor → this entity (SUCCEEDED_BY)
        "ceos":        {},
        "officers":    {},   # founder / chairperson / board member → HAS_ROLE
        "owners":      {},   # owned by (P127) → OWNS edge (owner → company)
        "headquarters": {},  # city -> {city, country, coord} (all P159 HQs)
        "hq_lat":      None,  # primary HQ (map pin) — filled after the loop
        "hq_lng":      None,
        "hq_city":     None,
        "hq_country":  None,
    }

    item_coord = None  # company's own P625, used as a fallback HQ coordinate

    for row in rows:
        # Basic fields (set once)
        if result["name"] is None:
            result["name"]        = _label(row, "itemLabel")
            result["description"] = _v(row, "itemDescription")
            result["country"]     = _v(row, "countryCode")

            if raw_date := _v(row, "founded"):
                try:
                    result["founded"] = int(raw_date[:4])
                except (ValueError, TypeError):
                    pass

            if raw_rev := _v(row, "revenue"):
                try:
                    result["revenue"] = float(raw_rev)
                except (ValueError, TypeError):
                    pass

        # Identifiers can arrive on any row (they are OPTIONAL joins in the core
        # query, so an early row may have them null while a later one carries the
        # value) — read them outside the set-once block above, first non-null wins.
        if result["lei"] is None:
            result["lei"] = normalize_lei(_v(row, "lei"))
        if result["sec_cik"] is None:
            result["sec_cik"] = normalize_cik(_v(row, "cik"))
        # Shortest valid URL wins, not first-seen: the subselect in the core
        # query already returns one row, but rows from a cache or an older
        # query shape may still carry the full storefront list — and among
        # https://apple.com/ and 100 regional variants, the root domain is
        # always the shortest.
        if candidate := normalize_url(_v(row, "website")):
            if result["website"] is None or len(candidate) < len(result["website"]):
                result["website"] = candidate

        # Employees — from the dedicated employees query (its own rows, so read
        # independently of the name block above). Latest value + its as-of year.
        if result["employees"] is None and (raw_emp := _v(row, "employees")):
            try:
                result["employees"] = int(float(raw_emp))
            except (ValueError, TypeError):
                pass
            if raw_asof := _v(row, "employeesAsOf"):
                try:
                    result["employees_as_of"] = int(raw_asof[:4])
                except (ValueError, TypeError):
                    pass

        # All domicile countries (P17 may repeat across rows for a dual-listed
        # company); the company's own P625 is a fallback HQ coordinate.
        if cc := _v(row, "countryCode"):
            result["countries"].add(cc)
        if item_coord is None:
            item_coord = _parse_point(_v(row, "itemCoord"))

        # Headquarters (P159) — collect each with its OWN city/country/coord so
        # they can never disagree (dual-listed firms have several).
        if hq_city := _label(row, "hqLabel"):
            hq = result["headquarters"].setdefault(
                hq_city, {"city": hq_city, "country": None, "coord": None})
            if hq["country"] is None:
                hq["country"] = _v(row, "hqCountryCode")
            if hq["coord"] is None:
                hq["coord"] = _parse_point(_v(row, "hqCoord"))

        # Aliases (skos:altLabel, English)
        if alias := _v(row, "altLabel"):
            result["aliases"].add(alias)

        # Instance (entity type)
        if inst_uri := _v(row, "instance"):
            result["instances"].add(_qid(inst_uri))

        # Subsidiaries
        if sub_uri := _v(row, "subsidiary"):
            sub_qid = _qid(sub_uri)
            if sub_qid and sub_qid not in result["subsidiaries"]:
                result["subsidiaries"][sub_qid] = {
                    "qid":       sub_qid,
                    "name":      _label(row, "subsidiaryLabel"),
                    "instances": set(),
                }
            if sub_inst := _v(row, "subsidiaryInstance"):
                result["subsidiaries"][sub_qid]["instances"].add(_qid(sub_inst))

        # Parent org
        if parent_uri := _v(row, "parent"):
            result["parents"].add(_qid(parent_uri))

        # Succession (P1366 replaced-by / P1365 replaces) → SUCCEEDED_BY edges.
        # Directed predecessor → successor; store just qid + name (like an owner).
        if succ_uri := _v(row, "successor"):
            succ_qid = _qid(succ_uri)
            if succ_qid and succ_qid not in result["successors"]:
                result["successors"][succ_qid] = {
                    "qid": succ_qid, "name": _label(row, "successorLabel"),
                    "date": (_v(row, "successorDate") or "")[:10] or None}
        if pred_uri := _v(row, "predecessor"):
            pred_qid = _qid(pred_uri)
            if pred_qid and pred_qid not in result["predecessors"]:
                result["predecessors"][pred_qid] = {
                    "qid": pred_qid, "name": _label(row, "predecessorLabel"),
                    "date": (_v(row, "predecessorDate") or "")[:10] or None}

        # CEO (keyed by qid+since to capture multiple tenures)
        if ceo_uri := _v(row, "ceo"):
            ceo_qid = _qid(ceo_uri)
            since   = (_v(row, "ceoStart") or "")[:10] or None
            until   = (_v(row, "ceoEnd")   or "")[:10] or None
            key     = f"{ceo_qid}|{since}"
            if ceo_qid and key not in result["ceos"]:
                result["ceos"][key] = {
                    "qid":         ceo_qid,
                    "label":       _label(row, "ceoLabel"),
                    "description": _v(row, "ceoDescription"),
                    "nationality": _v(row, "ceoNationalityCode"),
                    "since":       since,
                    "until":       until,
                }

        # Founder / chairperson / board member → HAS_ROLE (person + role).
        # Keyed by qid+role+since (like CEO) to capture separate tenures and carry the
        # position's start/end dates (P580/P582) so they show on the timeline.
        for var, role in (("founder", "Founder"), ("chair", "Chairman"),
                          ("board", "Board Member")):
            if uri := _v(row, var):
                pqid = _qid(uri)
                since = (_v(row, f"{var}Start") or "")[:10] or None
                until = (_v(row, f"{var}End") or "")[:10] or None
                okey = f"{pqid}|{role}|{since}"
                if pqid and okey not in result["officers"]:
                    result["officers"][okey] = {
                        "qid":   pqid,
                        "label": _label(row, f"{var}Label"),
                        "role":  role,
                        "since": since,
                        "until": until,
                    }

        # Owned by (P127) → OWNS edge. Owner may be a person or an entity;
        # keep its P31 instances so the runner can tell which.
        if owner_uri := _v(row, "owner"):
            owner_qid = _qid(owner_uri)
            if owner_qid and owner_qid not in result["owners"]:
                result["owners"][owner_qid] = {
                    "qid":       owner_qid,
                    "label":     _label(row, "ownerLabel"),
                    "instances": set(),
                }
            if owner_inst := _v(row, "ownerInstance"):
                result["owners"][owner_qid]["instances"].add(_qid(owner_inst))

    # ── Headquarters: choose a consistent primary + list them all ────────────
    multi_country = len(result["countries"]) > 1
    hqs = list(result["headquarters"].values())
    # Primary HQ (map pin + main display): prefer one with BOTH coordinates and
    # a resolved country, so city/country agree and the pin is placeable.
    primary = (next((h for h in hqs if h["coord"] and h["country"]), None)
               or next((h for h in hqs if h["country"]), None)
               or next((h for h in hqs if h["coord"]), None)
               or (hqs[0] if hqs else None))
    if primary:
        result["hq_city"]    = primary["city"]
        # Use the HQ's own country. Only fall back to the entity's country for a
        # single-domicile company — for a dual-listed firm, guessing would
        # reintroduce the mismatch (e.g. Rotterdam labelled GB).
        result["hq_country"] = primary["country"] or (None if multi_country else result["country"])
        coord = primary["coord"] or item_coord
        if coord:
            result["hq_lat"], result["hq_lng"] = coord
    elif item_coord:
        # No named P159 HQ, but the company has its own coordinate.
        result["hq_lat"], result["hq_lng"] = item_coord
        result["hq_country"] = result["country"]

    # All HQs as "City|CC" strings (CC may be empty) for display.
    result["hq_locations"] = [
        f"{h['city']}|{h['country'] or ''}" for h in hqs if h.get("city")
    ]

    # Domicile countries: primary first, then the rest, de-duplicated.
    others = sorted(c for c in result["countries"] if c and c != result["country"])
    result["countries"] = ([result["country"]] if result["country"] else []) + others

    # Convert sets/dicts to lists
    result["aliases"]      = sorted(result["aliases"])
    result["instances"]    = list(result["instances"])
    result["subsidiaries"] = list(result["subsidiaries"].values())
    result["parents"]      = list(result["parents"])
    result["successors"]   = list(result["successors"].values())
    result["predecessors"] = list(result["predecessors"].values())
    result["ceos"]         = list(result["ceos"].values())
    result["officers"]     = list(result["officers"].values())
    for o in result["owners"].values():
        o["instances"] = list(o["instances"])
    result["owners"]       = list(result["owners"].values())
    result.pop("headquarters", None)

    return result
