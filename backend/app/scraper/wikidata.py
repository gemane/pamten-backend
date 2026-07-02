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
  instance (P31)   → used to classify entity type (company / person / etc.)
  countryCode      → entity.country (ISO-2)
  founded (P571)   → entity.founded_year
  revenue (P2139)  → entity.revenue_usd
  subsidiary (P355)→ OWNS edge (target entity)
  parent (P749)    → OWNS edge (source entity)
  ceo (P169)       → person node + HAS_ROLE edge (role="CEO")

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


def fetch_company_data(qid: str) -> dict | None:
    """
    Fetch a company's data from Wikidata via SPARQL:
    basic info, subsidiaries, parent org, and CEO.
    Returns a structured dict or None if no results.
    """
    query = f"""
    SELECT ?itemLabel ?itemDescription
           ?instance
           ?countryCode
           ?founded ?revenue
           ?subsidiary ?subsidiaryLabel ?subsidiaryInstance
           ?parent
           ?ceo ?ceoLabel ?ceoDescription ?ceoNationalityCode
           ?ceoStart ?ceoEnd
    WHERE {{
      BIND(wd:{qid} AS ?item)
      OPTIONAL {{ ?item wdt:P31 ?instance }}
      OPTIONAL {{
        ?item wdt:P17 ?country .
        ?country wdt:P297 ?countryCode
      }}
      OPTIONAL {{ ?item wdt:P571 ?founded }}
      OPTIONAL {{
        ?item wdt:P2139 ?revenue .
        FILTER(?revenue > 0)
      }}
      OPTIONAL {{
        ?item wdt:P355 ?subsidiary .
        OPTIONAL {{ ?subsidiary wdt:P31 ?subsidiaryInstance }}
      }}
      OPTIONAL {{ ?item wdt:P749 ?parent }}
      OPTIONAL {{
        ?item p:P169 ?ceoStmt .
        ?ceoStmt ps:P169 ?ceo .
        OPTIONAL {{ ?ceoStmt pq:P580 ?ceoStart }}
        OPTIONAL {{ ?ceoStmt pq:P582 ?ceoEnd }}
        OPTIONAL {{
          ?ceo wdt:P27 ?ceoNationality .
          ?ceoNationality wdt:P297 ?ceoNationalityCode
        }}
        OPTIONAL {{
          ?ceo schema:description ?ceoDescription .
          FILTER(LANG(?ceoDescription) = "en")
        }}
      }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
    }}
    """
    time.sleep(REQUEST_DELAY)
    r = httpx.get(
        SPARQL_URL,
        params={"query": query, "format": "json"},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()["results"]["bindings"]
    return _aggregate(qid, rows)


def _v(row: dict, key: str) -> str | None:
    return row.get(key, {}).get("value")


def _qid(uri: str | None) -> str | None:
    """Extract Q-id from a Wikidata entity URI."""
    if not uri:
        return None
    return uri.rstrip("/").split("/")[-1]


def _aggregate(qid: str, rows: list) -> dict | None:
    if not rows:
        return None

    result = {
        "qid":         qid,
        "name":        None,
        "description": None,
        "instances":   set(),
        "country":     None,
        "founded":     None,
        "revenue":     None,
        "subsidiaries": {},
        "parents":     set(),
        "ceos":        {},
    }

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

    # Convert sets/dicts to lists
    result["instances"]    = list(result["instances"])
    result["subsidiaries"] = list(result["subsidiaries"].values())
    result["parents"]      = list(result["parents"])
    result["ceos"]         = list(result["ceos"].values())

    return result
