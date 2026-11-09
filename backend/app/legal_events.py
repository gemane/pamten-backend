"""The dated states of a company that still exists — `Entity.gleif_events`
(findings item 85d, 2026-11-09).

GLEIF's `LegalEntityEvents` carry more than renames and ends: LIQUIDATION
(15,998 COMPLETED + 13,215 IN_PROGRESS records in the 2026-11-08 golden
copy), BANKRUPTCY (1,103 + 1,296), INSOLVENCY (177 + 288) and
VOLUNTARY_ARRANGEMENT (115 + 93) are proceedings a company goes through while
it exists — a state, never its end (`gleif_lei_cdf._end_of` reads the ends:
DISSOLUTION, MERGERS_AND_ACQUISITIONS, ABSORPTION, BREAKUP). Until 2026-11-09
nothing kept them: a company in liquidation read like any other, and 17,297
INACTIVE records that hold only such events could say nothing of what
happened to them.

Stored on the node as a list of strings, one per event, as the other dated
lists are (`former_names`: "NAME|YYYY-MM-DD"):

    "LIQUIDATION|IN_PROGRESS|2025-04-28|2025-04-28"
     type       |status     |effective |recorded

The effective day is the event's own; the recorded day is when the register
wrote it down — 2,761 liquidations and 169 bankruptcies in progress carry no
effective day, and the recorded day is then what dates them on the timeline
(`dated_by: "recorded"`, so the reader can say so). A WITHDRAWN_CANCELLED
event is kept: a liquidation that was called off is a fact too. GLEIF alone
writes the list (the full load, the daily delta and the register pointer,
through `gleif_lei_cdf._entity_props`), and overwrites it on every read, so
it is always what the register states today.

What reads it: the timeline (`routers.relationships`) turns each entry into a
`legal_event` row ("Liquidation — in progress since 2025-04-28"); the panel
derives the current state from the raw list (a type whose latest entry is
IN_PROGRESS: "In liquidation since …").
"""
import re

SEP = "|"
#: the event types that are states, in GLEIF's spelling
STATE_TYPES = ("LIQUIDATION", "BANKRUPTCY", "INSOLVENCY", "VOLUNTARY_ARRANGEMENT")
STATUSES = ("COMPLETED", "IN_PROGRESS", "WITHDRAWN_CANCELLED")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day(value) -> str | None:
    """A LEI-CDF date-time as its local day, or None."""
    if isinstance(value, dict):
        value = value.get("$")
    day = str(value or "")[:10]
    return day if _DAY.match(day) else None


def entry(kind: str, status: str, effective: str | None, recorded: str | None) -> str | None:
    """One event as its stored string; None for a type or status outside the
    vocabulary (an end event is not a state — it is the node's `dissolved_on`)."""
    if kind not in STATE_TYPES or status not in STATUSES:
        return None
    return SEP.join((kind, status, effective or "", recorded or ""))


def parse(value: str) -> dict | None:
    """``"TYPE|STATUS|effective|recorded"`` → ``{event, status, effective,
    recorded}`` in the API's lower-case spelling, or None for a malformed entry."""
    parts = (value or "").split(SEP)
    if len(parts) != 4 or parts[0] not in STATE_TYPES or parts[1] not in STATUSES:
        return None
    return {"event": parts[0].lower(), "status": parts[1].lower(),
            "effective": parts[2] if _DAY.match(parts[2]) else None,
            "recorded": parts[3] if _DAY.match(parts[3]) else None}


def from_record(entity: dict) -> list[str]:
    """The state events of a LEI-CDF ``Entity`` block, oldest first (by the
    effective day, else the recorded one; undated last), deduplicated — the
    golden copy repeats an event now and then (two identical
    MERGERS_AND_ACQUISITIONS rows on arvato distribution GmbH)."""
    raw = (entity.get("LegalEntityEvents") or {}).get("LegalEntityEvent") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[str] = []
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("LegalEntityEventType")
        kind = kind.get("$") if isinstance(kind, dict) else kind
        made = entry(str(kind or ""), str(ev.get("@event_status") or "COMPLETED"),
                     _day(ev.get("LegalEntityEventEffectiveDate")), _day(ev.get("LegalEntityEventRecordedDate")))
        if made and made not in out:
            out.append(made)
    return sorted(out, key=lambda e: ((p := parse(e)) and (p["effective"] or p["recorded"])) or "9999")


def timeline_events(entries: list[str] | None) -> list[dict]:
    """The entries as timeline rows: ``kind: "legal_event"``, the event and
    its status, ``since`` = the effective day, else the recorded day
    (``dated_by`` says which), ``active`` while in progress. An entry with
    neither day has no place on a timeline."""
    rows = []
    for e in entries or []:
        p = parse(e)
        if not p:
            continue
        day = p["effective"] or p["recorded"]
        if not day:
            continue
        rows.append({"kind": "legal_event", "event": p["event"], "status": p["status"], "since": day,
                     "dated_by": "effective" if p["effective"] else "recorded",
                     "until": None, "active": p["status"] == "in_progress"})
    return rows
