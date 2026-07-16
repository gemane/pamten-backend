"""
Wikidata scraper — company search and structured SPARQL fetch.

Data source:  https://www.wikidata.org
Manual lookup: https://www.wikidata.org/wiki/<QID>  (e.g. Q380 for Apple Inc.)

Endpoints used:
  Search:  GET https://www.wikidata.org/w/api.php
             ?action=wbsearchentities&search=<name>&language=en&type=item
  SPARQL:  GET https://query.wikidata.org/sparql?query=<SPARQL>&format=json
             Fetches basic info, subsidiaries, parent org, and CEO for a QID.

Fields returned and Pamten mapping:
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
  and should be polite. We sleep 0.4 s between calls (~2.5 req/s).
  SPARQL endpoint may return 429 under heavy load — callers should retry.
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

import re
import time
import httpx

WIKIDATA_API  = "https://www.wikidata.org/w/api.php"
SPARQL_URL    = "https://query.wikidata.org/sparql"
USER_AGENT    = "Pamten/1.0 (https://pamten-frontend.onrender.com)"
HEADERS       = {"User-Agent": USER_AGENT}
REQUEST_DELAY = 0.4  # seconds between Wikidata calls


def search_entity(query: str, limit: int = 5) -> list:
    """Full-text search on Wikidata. Returns list of {id, label, description}."""
    r = httpx.get(
        WIKIDATA_API,
        params={
            "action":   "wbsearchentities",
            "search":   query,
            "language": "en",
            "type":     "item",
            "limit":    limit,
            "format":   "json",
        },
        headers=HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.json().get("search", [])


_LABEL_SERVICE = 'SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }'


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
    WHERE {{
      BIND(wd:{qid} AS ?item)
      OPTIONAL {{ ?item skos:altLabel ?altLabel . FILTER(LANG(?altLabel) = "en") }}
      OPTIONAL {{ ?item wdt:P31 ?instance }}
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
           ?founder ?founderLabel ?chair ?chairLabel ?board ?boardLabel
    WHERE {{
      BIND(wd:{qid} AS ?item)
      {{
        ?item p:P169 ?ceoStmt . ?ceoStmt ps:P169 ?ceo .
        OPTIONAL {{ ?ceoStmt pq:P580 ?ceoStart }}
        OPTIONAL {{ ?ceoStmt pq:P582 ?ceoEnd }}
        OPTIONAL {{ ?ceo wdt:P27 ?ceoNationality . ?ceoNationality wdt:P297 ?ceoNationalityCode }}
        OPTIONAL {{ ?ceo schema:description ?ceoDescription . FILTER(LANG(?ceoDescription) = "en") }}
      }}
      UNION {{ ?item wdt:P112 ?founder }}
      UNION {{ ?item wdt:P488 ?chair }}
      UNION {{ ?item wdt:P3320 ?board }}
      {_LABEL_SERVICE}
    }}
    """
    # 3. Relations: subsidiaries, parent, owners.
    relations = f"""
    SELECT ?subsidiary ?subsidiaryLabel ?subsidiaryInstance ?parent
           ?owner ?ownerLabel ?ownerInstance
    WHERE {{
      BIND(wd:{qid} AS ?item)
      OPTIONAL {{ ?item wdt:P355 ?subsidiary . OPTIONAL {{ ?subsidiary wdt:P31 ?subsidiaryInstance }} }}
      OPTIONAL {{ ?item wdt:P749 ?parent }}
      OPTIONAL {{ ?item wdt:P127 ?owner . OPTIONAL {{ ?owner wdt:P31 ?ownerInstance }} }}
      {_LABEL_SERVICE}
    }}
    """
    rows: list = []
    for query in (core, people, relations):
        time.sleep(REQUEST_DELAY)
        r = httpx.get(SPARQL_URL, params={"query": query, "format": "json"},
                      headers=HEADERS, timeout=30)
        r.raise_for_status()
        rows.extend(r.json()["results"]["bindings"])
    return rows


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
           (GROUP_CONCAT(DISTINCT ?natCode; separator="|") AS ?nats)
           (GROUP_CONCAT(DISTINCT ?alias;   separator="|") AS ?aliases)
    WHERE {{
      VALUES ?person {{ {values} }}
      OPTIONAL {{ ?person wdt:P569 ?birth }}
      OPTIONAL {{ ?person wdt:P570 ?death }}
      OPTIONAL {{ ?person wdt:P19 ?bp . ?bp rdfs:label ?bpLabel . FILTER(LANG(?bpLabel) = "en") }}
      OPTIONAL {{ ?person wdt:P27 ?nat . ?nat wdt:P297 ?natCode }}
      OPTIONAL {{ ?person skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
    }}
    GROUP BY ?person ?birth ?death
    """
    time.sleep(REQUEST_DELAY)
    r = httpx.get(SPARQL_URL, params={"query": query, "format": "json"},
                  headers=HEADERS, timeout=30)
    r.raise_for_status()
    details: dict[str, dict] = {}
    for row in r.json()["results"]["bindings"]:
        pqid = _qid(_v(row, "person"))
        if not pqid or pqid in details:
            continue
        nats    = [c for c in (_v(row, "nats")    or "").split("|") if c]
        aliases = [a for a in (_v(row, "aliases") or "").split("|") if a]
        details[pqid] = {
            "birth_date":    (_v(row, "birth") or "")[:10] or None,
            "death_date":    (_v(row, "death") or "")[:10] or None,
            "birth_place":   _v(row, "birthPlace") or None,
            "nationalities": nats,
            "aliases":       aliases,
        }
    return details


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
    details = _fetch_person_details(person_qids)
    if details:
        for group in (data["ceos"], data["officers"], data["owners"]):
            for person in group:
                if extra := details.get(person.get("qid")):
                    person.update(extra)
    return data


def _v(row: dict, key: str) -> str | None:
    return row.get(key, {}).get("value")


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
        "subsidiaries": {},
        "parents":     set(),
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
            result["name"]        = _v(row, "itemLabel")
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

        # All domicile countries (P17 may repeat across rows for a dual-listed
        # company); the company's own P625 is a fallback HQ coordinate.
        if cc := _v(row, "countryCode"):
            result["countries"].add(cc)
        if item_coord is None:
            item_coord = _parse_point(_v(row, "itemCoord"))

        # Headquarters (P159) — collect each with its OWN city/country/coord so
        # they can never disagree (dual-listed firms have several).
        if hq_city := _v(row, "hqLabel"):
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
                    "name":      _v(row, "subsidiaryLabel"),
                    "instances": set(),
                }
            if sub_inst := _v(row, "subsidiaryInstance"):
                result["subsidiaries"][sub_qid]["instances"].add(_qid(sub_inst))

        # Parent org
        if parent_uri := _v(row, "parent"):
            result["parents"].add(_qid(parent_uri))

        # CEO (keyed by qid+since to capture multiple tenures)
        if ceo_uri := _v(row, "ceo"):
            ceo_qid = _qid(ceo_uri)
            since   = (_v(row, "ceoStart") or "")[:10] or None
            until   = (_v(row, "ceoEnd")   or "")[:10] or None
            key     = f"{ceo_qid}|{since}"
            if ceo_qid and key not in result["ceos"]:
                result["ceos"][key] = {
                    "qid":         ceo_qid,
                    "label":       _v(row, "ceoLabel"),
                    "description": _v(row, "ceoDescription"),
                    "nationality": _v(row, "ceoNationalityCode"),
                    "since":       since,
                    "until":       until,
                }

        # Founder / chairperson / board member → HAS_ROLE (person + role)
        for var, role in (("founder", "Founder"), ("chair", "Chairman"),
                          ("board", "Board Member")):
            if uri := _v(row, var):
                pqid = _qid(uri)
                okey = f"{pqid}|{role}"
                if pqid and okey not in result["officers"]:
                    result["officers"][okey] = {
                        "qid":   pqid,
                        "label": _v(row, f"{var}Label"),
                        "role":  role,
                    }

        # Owned by (P127) → OWNS edge. Owner may be a person or an entity;
        # keep its P31 instances so the runner can tell which.
        if owner_uri := _v(row, "owner"):
            owner_qid = _qid(owner_uri)
            if owner_qid and owner_qid not in result["owners"]:
                result["owners"][owner_qid] = {
                    "qid":       owner_qid,
                    "label":     _v(row, "ownerLabel"),
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
    result["ceos"]         = list(result["ceos"].values())
    result["officers"]     = list(result["officers"].values())
    for o in result["owners"].values():
        o["instances"] = list(o["instances"])
    result["owners"]       = list(result["owners"].values())
    result.pop("headquarters", None)

    return result
