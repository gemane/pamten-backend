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
never changes which node a writer finds. `heal_keyed_ids` renames an older
database's nodes through `merged_ids.rename_node_id` — claims re-keyed,
references re-pointed, a forwarding row per old id — so old links keep
opening.
"""
import logging
import re
import uuid

log = logging.getLogger(__name__)

_QID = re.compile(r"^Q[0-9]+$")
_CIK = re.compile(r"^[0-9]{1,10}$")
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
    if sec_cik and _CIK.match(sec_cik.strip()):
        return f"sec:{sec_cik.strip()}"
    return None


def mint_node_id(lei: str | None = None, wikidata_id: str | None = None,
                 sec_cik: str | None = None) -> str:
    """The id for a node a writer is about to create: keyed when it can be,
    a uuid otherwise."""
    return keyed_id(lei, wikidata_id, sec_cik) or str(uuid.uuid4())


def is_uuid(node_id: str | None) -> bool:
    return bool(node_id) and bool(_UUID.match(node_id))


def heal_keyed_ids(apply: bool = False) -> dict:
    """Give every uuid-keyed Entity and Person that carries a hard id the
    keyed id it would get today (see the module docstring). Dry run unless
    `apply`: counts per label what would move, what has nothing to key on,
    and what cannot move because the keyed id is already taken (a twin the
    dedup has yet to merge — reported, never forced)."""
    from app.database import db
    from app.db import paging
    from app.merged_ids import rename_node_id
    out: dict = {"applied": bool(apply)}
    for label in ("Entity", "Person"):
        counts = {"scanned": 0, "moved": 0, "unkeyed": 0, "taken": 0, "sample": []}
        for page in paging.iter_id_pages(label, columns="id, lei_id, wikidata_id, sec_cik, name, full_name"):
            for r in page:
                old = r.get("id")
                if not is_uuid(old):
                    continue
                counts["scanned"] += 1
                new = keyed_id(r.get("lei_id"), r.get("wikidata_id"), r.get("sec_cik"))
                if not new:
                    counts["unkeyed"] += 1
                    continue
                if len(counts["sample"]) < 20:
                    counts["sample"].append((r.get("name") or r.get("full_name"), old, new))
                if not apply:
                    counts["moved"] += 1
                    continue
                with db.get_session() as session:
                    if rename_node_id(session, label, old, new):
                        counts["moved"] += 1
                    else:
                        counts["taken"] += 1
                        log.warning("keyed id %s already taken; %s %s keeps its uuid", new, label, old)
        out[label] = counts
    log.info("keyed ids heal: %s", {k: {kk: vv for kk, vv in v.items() if kk != "sample"} if isinstance(v, dict) else v
                                   for k, v in out.items()})
    return out
