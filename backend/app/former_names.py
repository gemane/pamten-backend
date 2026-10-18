"""A company's former names, each with the day it stopped applying.

Registers record renames with dates — Companies House lists every previous
name with its change-of-name date, SEC EDGAR's submissions file gives each
former name a ``from``/``to`` — and the timeline wants to say *"Renamed from
Brightstar 20:20 UK Limited"* under 2021, not merely that the old name is
also known. ``aliases`` keeps the plain names for search; this module owns
the dated record beside it.

Stored as ``Entity.former_names``, a list of strings ``"NAME|YYYY-MM-DD"``
(the ``"City|CC"`` convention of ``hq_locations``), the date being the last
day the name was in force. A name without a date is ``"NAME|"`` — kept so a
source that knows the name but not the date (GLEIF's previous legal names,
should they ever be written here) loses nothing, but it gets no place on the
timeline. The separator is the LAST ``|``, so a pipe inside a name survives.
"""
import re

SEP = "|"
_DATE = re.compile(r"^\d{4}(-\d{2}){0,2}$")


def entry(name: str, until: str | None) -> str | None:
    """``"NAME|YYYY-MM-DD"`` — or None for an empty name. A date that is not an
    ISO day/month/year (a source's garbage) is dropped, never stored."""
    name = (name or "").strip()
    if not name:
        return None
    until = (until or "").strip()[:10]
    return f"{name}{SEP}{until if _DATE.match(until) else ''}"


def parse(value: str) -> tuple[str, str | None]:
    """``"NAME|YYYY-MM-DD"`` → ``(name, until)``; a bare name → ``(name, None)``."""
    name, sep, until = (value or "").rpartition(SEP)
    if not sep:
        return value.strip(), None
    until = until.strip()
    return name.strip(), until if _DATE.match(until) else None


def names(entries: list[str] | None) -> list[str]:
    """The plain names, order kept, deduped case-insensitively — what goes
    into ``aliases`` and ``search_text``."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in entries or []:
        name, _ = parse(raw)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def merge(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Union, the existing order first, keyed on (name, date). An undated
    entry yields to a dated one of the same name, whichever side it is on —
    the date is the thing worth keeping."""
    out: list[str] = []
    dated: set[tuple[str, str]] = set()   # (casefolded name, date) already in out
    undated: dict[str, int] = {}          # casefolded name without a date → index in out
    for raw in list(existing or []) + list(new or []):
        name, until = parse(raw)
        if not name:
            continue
        key = name.casefold()
        e = entry(name, until)
        if until is None:
            if key in undated or any(k == key for k, _ in dated):
                continue
            undated[key] = len(out)
            out.append(e)
            continue
        if (key, until) in dated:
            continue
        dated.add((key, until))
        if key in undated:
            out[undated.pop(key)] = e      # the date fills the undated slot in place
        else:
            out.append(e)
    return out


def rename_events(current_name: str | None, entries: list[str] | None) -> list[dict]:
    """The renames as timeline events, oldest first: each dated former name
    with the name that followed it — the next former name in date order, or
    the current name after the last. Undated names have no place on a
    timeline and a former name equal to the current one says nothing."""
    cur = (current_name or "").strip()
    dated = sorted(
        ((until, name) for name, until in map(parse, entries or [])
         if until and name and name.casefold() != cur.casefold()),
    )
    events = []
    for i, (until, name) in enumerate(dated):
        following = dated[i + 1][1] if i + 1 < len(dated) else cur
        events.append({"kind": "renamed", "since": until, "until": None, "active": False,
                       "former_name": name, "new_name": following or None})
    return events
