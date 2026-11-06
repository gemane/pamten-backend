"""The securities bridge: an EDGAR issuer's CUSIP → its US ISIN → GLEIF's
LEI-ISIN mapping → the issuer's LEI (findings item 71, 2026-11-07).

No register names the other's id: EDGAR states no LEI for most filers, GLEIF
states no CIK, and the registers' comparison of names and addresses
(`legal_identity`) cannot settle a pair whose names differ on the reference day
— Sirius XM's GLEIF record was renamed in 2024, EDGAR recorded it in 2026. A
security settles it. EDGAR states CUSIPs (13F tables; the 13F runner keeps an
issuer's on its node as ``cusips``), a US ISIN is "US" + the CUSIP + a check
digit, and the issuer's national numbering agency reports each ISIN's issuer
LEI to ANNA, which GLEIF certifies and publishes daily as the LEI-ISIN file
(CC0; https://www.gleif.org/en/lei-data/lei-mapping/download-isin-to-lei-relationship-files).

The file is a snapshot of current pairs — two columns, no dates — and the
bridge asks the current issuer of a security. Per node with stored CUSIPs and
no LEI:

- every mapped CUSIP names ONE LEI, and a node holds it → the pair is one legal
  entity: the node takes the LEI with the ISIN as evidence, and the two nodes
  merge as any pair sharing an LEI does (`maintenance._apply_entity_merge`);
- one LEI no node holds → GLEIF writes its record (`register_pointer
  .write_from_gleif`) and the pair merges the same way;
- several LEIs → an umbrella, never written: a fund family's CUSIPs map to each
  fund's own LEI (iShares, Inc. on dev), so the node is none of them;
- the LEI's node carries another CIK → a contradiction, reported;
- a keep-separate mark (NOT_DUPLICATE) → never asked.

A node that already holds an LEI is only checked: the same LEI is confirmed, a
different one reported. Nothing is written in a dry run. Manual command first
(`manage.py gleif-isin-bridge`), as every new pipeline starts (the user's rule,
2026-10)."""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

MAPPING_API = "https://mapping.gleif.org/api/v2/isin-lei"
MAPPING_PAGE = "https://www.gleif.org/en/lei-data/lei-mapping/download-isin-to-lei-relationship-files"
#: a file older than this is refused without ``allow_stale`` — GLEIF publishes daily
MAX_AGE_DAYS = 3
#: where the day's file is kept: a real disk, never /tmp (a 2 GB tmpfs here)
DEFAULT_DIR = os.path.expanduser("~/.cache/gleif-isin")

_CUSIP = re.compile(r"^[0-9A-Z*@#]{8}[0-9]$")


# ── identifiers ───────────────────────────────────────────────────────────────

def _char_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if c.isalpha():
        return ord(c) - ord("A") + 10
    return {"*": 36, "@": 37, "#": 38}[c]


def cusip_valid(cusip: str | None) -> bool:
    """A nine-character CUSIP whose check digit is right — a 13F table's
    typo or a truncated value is no security, and an ISIN derived from it
    could name another issuer."""
    c = (cusip or "").strip().upper()
    if not _CUSIP.match(c):
        return False
    total = 0
    for i, ch in enumerate(c[:8]):
        v = _char_value(ch)
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(c[8])


def us_isin(cusip: str | None) -> str | None:
    """The US ISIN of a CUSIP — "US" + the CUSIP + the ISIN check digit (ISO
    6166: letters as two digits, Luhn over the whole) — or None for an
    invalid CUSIP."""
    if not cusip_valid(cusip):
        return None
    body = "US" + cusip.strip().upper()
    digits = "".join(str(_char_value(ch)) for ch in body)
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return body + str((10 - total % 10) % 10)


# ── the file ──────────────────────────────────────────────────────────────────

@dataclass
class MappingFile:
    path: str
    day: str          # the day GLEIF uploaded it (UTC), the evidence's date
    name: str


def latest_file() -> dict:
    """GLEIF's newest LEI-ISIN file: ``{file_name, uploaded_at, url}``."""
    from app.scraper.gleif_incremental import _api_json
    data = _api_json(MAPPING_API, params={"page[size]": 1})
    attrs = data["data"][0]["attributes"]
    return {"file_name": attrs["fileName"], "uploaded_at": attrs["uploadedAt"], "url": attrs["downloadLink"]}


def fetch(directory: str = DEFAULT_DIR) -> MappingFile:
    """The newest file on disk, downloaded unless the same file is there."""
    from app.scraper.gleif_incremental import _download
    meta = latest_file()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, meta["file_name"])
    if not os.path.exists(path):
        log.info("GLEIF LEI-ISIN: downloading %s", meta["file_name"])
        tmp = path + ".part"
        _download(meta["url"], tmp)
        os.replace(tmp, path)
    return MappingFile(path=path, day=meta["uploaded_at"][:10], name=meta["file_name"])


def local_file(path: str) -> MappingFile:
    """A file given by hand; its day from GLEIF's name (isin-lei-YYYYMMDDTHHMMSS.zip)."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})T\d{6}", os.path.basename(path))
    if not m:
        raise ValueError(f"not a GLEIF LEI-ISIN file name: {path}")
    return MappingFile(path=path, day=f"{m[1]}-{m[2]}-{m[3]}", name=os.path.basename(path))


def leis_for(path: str, isins: set[str]) -> dict[str, set[str]]:
    """ISIN → the LEIs the file maps it to, for the ISINs asked only (the file
    has 9 M rows; holding them all would take hundreds of MB)."""
    out: dict[str, set[str]] = {}
    if not isins:
        return out
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(member) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
            header = next(reader, None)
            if not header or [h.strip().upper() for h in header[:2]] != ["LEI", "ISIN"]:
                raise ValueError(f"unexpected header in {member}: {header}")
            for row in reader:
                if len(row) >= 2 and row[1] in isins:
                    out.setdefault(row[1], set()).add(row[0].strip().upper())
    return out


# ── the plan ──────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    file: MappingFile
    confirmed: list[dict] = field(default_factory=list)
    joins: list[dict] = field(default_factory=list)       # the LEI's node exists
    attaches: list[dict] = field(default_factory=list)    # GLEIF writes the LEI's node
    umbrellas: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    kept_apart: list[dict] = field(default_factory=list)
    unmapped: int = 0
    nodes: int = 0

    def counts(self) -> dict:
        return {"nodes": self.nodes, "confirmed": len(self.confirmed), "joins": len(self.joins),
                "attaches": len(self.attaches), "umbrellas": len(self.umbrellas),
                "contradictions": len(self.contradictions), "kept_apart": len(self.kept_apart),
                "unmapped": self.unmapped}


def _nodes_with_cusips() -> list[dict]:
    from app.db.arcadedb import run_sql
    rows = run_sql("SELECT id, name, sec_cik, lei_id, cusip, cusips FROM Entity "
                   "WHERE cusips IS NOT NULL OR cusip IS NOT NULL")
    return [{k: v for k, v in dict(r).items() if not k.startswith("@")} for r in rows]


def _isins_of(node: dict) -> list[str]:
    cusips = [*(node.get("cusips") or []), *([node["cusip"]] if node.get("cusip") else [])]
    return [i for i in dict.fromkeys(us_isin(c) for c in cusips) if i]


def plan(mapping: MappingFile, kept_separate: set | None = None) -> Plan:
    """What the bridge would do with every node that carries a CUSIP."""
    from app.db.arcadedb import run_sql
    from app.scraper.maintenance import _not_duplicate_pairs
    kept_separate = _not_duplicate_pairs() if kept_separate is None else kept_separate
    nodes = _nodes_with_cusips()
    isins = {i for n in nodes for i in _isins_of(n)}
    mapped = leis_for(mapping.path, isins)
    out = Plan(file=mapping, nodes=len(nodes))
    for n in nodes:
        found = {i: mapped[i] for i in _isins_of(n) if i in mapped}
        leis = set().union(*found.values()) if found else set()
        row = {"id": n["id"], "name": n.get("name"), "sec_cik": n.get("sec_cik"),
               "isins": sorted(found), "leis": sorted(leis)}
        if not leis:
            out.unmapped += 1
            continue
        if len(leis) > 1:
            out.umbrellas.append(row)
            continue
        lei = next(iter(leis))
        row["lei"] = lei
        row["isin"] = sorted(i for i, ls in found.items() if lei in ls)[0]
        if n.get("lei_id"):
            (out.confirmed if n["lei_id"] == lei else out.contradictions).append(
                row if n["lei_id"] == lei else {**row, "why": f"the node holds LEI {n['lei_id']}"})
            continue
        holder = run_sql("SELECT id, name, sec_cik FROM Entity WHERE lei_id = :l LIMIT 2", {"l": lei})
        if len(holder) > 1:
            out.contradictions.append({**row, "why": f"{len(holder)} nodes hold LEI {lei}"})
            continue
        if holder:
            h = dict(holder[0])
            if h.get("sec_cik") and n.get("sec_cik") and h["sec_cik"] != n["sec_cik"]:
                out.contradictions.append({**row, "why": f"{h['id']} holds LEI {lei} and CIK {h['sec_cik']}"})
            elif frozenset((n["id"], h["id"])) in kept_separate:
                out.kept_apart.append({**row, "lei_node": h["id"]})
            else:
                out.joins.append({**row, "lei_node": h["id"], "lei_node_name": h.get("name")})
        else:
            out.attaches.append(row)
    return out


# ── the writes ────────────────────────────────────────────────────────────────

def _evidence(row: dict, mapping: MappingFile) -> str:
    return f"isin:{row['isin']}@{mapping.day}"


def _stamp(node_id: str, lei: str, why: str) -> bool:
    """The node takes the LEI and the evidence — only while it holds none."""
    from app.db.arcadedb import run_command, run_sql
    rows = run_sql("SELECT lei_id FROM Entity WHERE id = :id", {"id": node_id})
    if not rows or rows[0].get("lei_id"):
        return False
    run_command("MATCH (e:Entity {id: $id}) SET e.lei_id = $lei, e.lei_source = $why, "
                "e.lei_source_url = COALESCE(e.lei_source_url, $url)",
                {"id": node_id, "lei": lei, "why": why, "url": MAPPING_PAGE})
    return True


def _merge_lei_group(lei: str, kept_separate: set) -> list[dict]:
    """The nodes that now share ``lei`` fold into one, as `dedupe-entities`
    folds any shared-id group: the most credible name survives and takes the
    best id (`_apply_entity_merge` re-keys it)."""
    from app.db.arcadedb import run_query
    from app.scraper.maintenance import _apply_entity_merge
    members = run_query("MATCH (e:Entity) WHERE e.lei_id = $lei RETURN e.id AS id, e.name AS name, "
                        "COALESCE(e.name_credibility, 0) AS cred", {"lei": lei})
    members = [dict(m) for m in members]
    if len(members) < 2:
        return []
    members.sort(key=lambda m: (-(m.get("cred") or 0), m["id"]))
    keep, merged = members[0], []
    for dead in members[1:]:
        if frozenset((dead["id"], keep["id"])) in kept_separate:
            continue
        _apply_entity_merge(dead, keep)
        merged.append({"kept": keep["id"], "deleted": dead["id"]})
    return merged


def apply(p: Plan, kept_separate: set | None = None) -> dict:
    """Write the plan's joins and attaches; the rest is reported only."""
    from app.scraper.maintenance import _not_duplicate_pairs
    from app.scraper.register_pointer import write_from_gleif
    kept_separate = _not_duplicate_pairs() if kept_separate is None else kept_separate
    done = {"joined": [], "attached": [], "skipped": []}
    for row in p.joins:
        # the marks are read again: a human may have kept the pair apart since the plan
        if frozenset((row["id"], row["lei_node"])) in kept_separate:
            done["skipped"].append({**row, "why": f"kept apart from {row['lei_node']}"})
            continue
        if not _stamp(row["id"], row["lei"], _evidence(row, p.file)):
            done["skipped"].append({**row, "why": "the node took an LEI meanwhile"})
            continue
        done["joined"].append({**row, "merged": _merge_lei_group(row["lei"], kept_separate)})
    for row in p.attaches:
        # GLEIF's record first, written on its own (no name join: the ISIN decides)
        if not write_from_gleif(row["lei"], by_name=False):
            done["skipped"].append({**row, "why": "GLEIF does not know the LEI"})
            continue
        if not _stamp(row["id"], row["lei"], _evidence(row, p.file)):
            done["skipped"].append({**row, "why": "the node took an LEI meanwhile"})
            continue
        done["attached"].append({**row, "merged": _merge_lei_group(row["lei"], kept_separate)})
    return done


def enabled() -> bool:
    """GLEIF's catalogue switch, as every GLEIF reader asks it. No environment
    flag of its own: a default-off flag on a GLEIF path once kept the register
    lookup from running in a whole rebuild (2026-10-29, the conformance rule
    `test_the_bulk_sources_have_no_environment_switch`); the bridge is a
    manual command, which is its rollout gate."""
    from app.scraper.register_pointer import _gleif_enabled
    return bool(_gleif_enabled())


def run(*, apply_writes: bool = False, path: str | None = None, allow_stale: bool = False,
        directory: str = DEFAULT_DIR, today: date | None = None) -> dict:
    """The bridge, end to end: the file, the plan, the writes (``apply_writes``)."""
    from app.scraper.run_log import record_run
    if not enabled():
        raise PermissionError("The securities bridge is off: the GLEIF source is disabled in the source catalogue.")
    mapping = local_file(path) if path else fetch(directory)
    age = ((today or datetime.now(timezone.utc).date()) - date.fromisoformat(mapping.day)).days
    if age > MAX_AGE_DAYS and not allow_stale:
        raise ValueError(f"the LEI-ISIN file of {mapping.day} is {age} days old (max {MAX_AGE_DAYS}); "
                         "pass allow_stale to use it")
    with record_run("gleif-isin", "all", reason="apply" if apply_writes else "dry-run") as run_row:
        p = plan(mapping)
        result = {"file": mapping.name, "day": mapping.day, "applied": apply_writes, **p.counts(),
                  "plan": {"joins": p.joins, "attaches": p.attaches, "umbrellas": p.umbrellas,
                           "contradictions": p.contradictions, "kept_apart": p.kept_apart}}
        if apply_writes:
            done = apply(p)
            result["written"] = done
            run_row["total"] = len(done["joined"]) + len(done["attached"])
        run_row["note"] = (f"{mapping.name}: {p.counts()['confirmed']} confirmed, {len(p.joins)} joins, "
                           f"{len(p.attaches)} attaches, {len(p.umbrellas)} umbrellas, "
                           f"{len(p.contradictions)} contradictions"
                           + ("" if apply_writes else " (dry run)"))
    return result
