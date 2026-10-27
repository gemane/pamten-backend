"""A node's id carries the register's key when the writer knows one.

The bulk importers always did — `lei:{LEI}`, `gb-coh:{number}`, `chpsc:…` —
while every node a per-company scrape created got a random uuid, whatever
hard id came with it. A uuid has one cost the forwarding table cannot cover:
a rebuild mints new ones, so every shared link, pin, flag and suppression
naming a Wikidata or SEC node broke on reload (2026-10-26: 814 entities and
1,411 persons on dev carried a QID or a CIK under a uuid).

`mint_node_id` keys a new node by the best hard id it arrives with — the LEI
(the same key the GLEIF import uses, so a later bulk load upserts the node
instead of creating a twin), else the Wikidata item (`wd:Q…`), else the SEC
CIK (`sec:{cik}`) — and falls back to a uuid for a name-only node (an Exhibit
21 row, a 13D party without a CIK), which has nothing to key on. The lookup
that precedes every creation still resolves by the hard ids, so a keyed id
never changes which node a writer finds.
"""
import logging
import re
import uuid

from app.scraper.mapper import normalize_cik

log = logging.getLogger(__name__)

_QID = re.compile(r"^Q[0-9]+$")
_LEI = re.compile(r"^[A-Z0-9]{20}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def keyed_id(lei: str | None = None, wikidata_id: str | None = None,
             sec_cik: str | None = None) -> str | None:
    """The register-keyed id a node with these hard ids gets, or None when it
    has none worth keying on (a malformed value counts as none)."""
    if lei and _LEI.match(lei.strip()):
        return f"lei:{lei.strip()}"
    if wikidata_id and _QID.match(wikidata_id.strip()):
        return f"wd:{wikidata_id.strip()}"
    cik = normalize_cik(sec_cik)
    if cik:
        return f"sec:{cik}"                      # always the 10-digit form: one filer, one id
    return None


def mint_node_id(lei: str | None = None, wikidata_id: str | None = None,
                 sec_cik: str | None = None) -> str:
    """The id for a node a writer is about to create: keyed when it can be,
    a uuid otherwise."""
    return keyed_id(lei, wikidata_id, sec_cik) or str(uuid.uuid4())
