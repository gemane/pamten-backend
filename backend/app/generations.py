"""Generational marks in a person's names — "Jr.", "Sr.", "II", "III", "IV".

A father and son share a name and often a company; the mark is the one thing
in the NAME that tells them apart. The duplicate scan (routers.persons) used
to see "Thomas Murphy" (aliases "…, Sr.") and "Thomas S. Murphy, Jr." as one
person once the son's "Thomas Murphy" alias matched and Berkshire corroborated
it, and the post-scrape auto-merge folded Berkshire's 2022 director into his
father, dead since 2022 (2026-10-25). Pure logic, no database.
"""
from __future__ import annotations

import re

_MARKS = {"jr": "jr", "junior": "jr", "sr": "sr", "senior": "sr", "ii": "ii", "iii": "iii", "iv": "iv"}
_TOKEN = re.compile(r"[a-z]+")


def generation_marks(*names: str | None) -> set[str]:
    """The marks any of these names carry, normalised: {"jr"}, {"sr"}, {"iii"}…
    "Junior"/"Senior" count as "jr"/"sr". A name without one gives nothing."""
    out: set[str] = set()
    for name in names:
        for tok in _TOKEN.findall((name or "").lower()):
            if tok in _MARKS:
                out.add(_MARKS[tok])
    return out


def marks_conflict(a: set[str], b: set[str]) -> bool:
    """Two people, definitively: both names are marked, and differently —
    "Sr." against "Jr.", "II" against "III". One marked and one not is NOT a
    conflict (a father is rarely written "Sr."), only a doubt."""
    return bool(a) and bool(b) and a != b


def mixed_generation_persons() -> list[dict]:
    """Every Person whose own names carry two different marks — "…, Sr." and
    "…, Jr." among one node's aliases — which is what a merged relative looks
    like. The scan now refuses that merge; this lists the ones made before."""
    from app.db import paging
    out = []
    for page in paging.iter_id_pages("Person", columns="id, full_name, aliases, wikidata_id, sec_cik"):
        for p in page:
            marks = generation_marks(p.get("full_name"), *(p.get("aliases") or []))
            if len(marks) >= 2:
                out.append({"id": p.get("id"), "full_name": p.get("full_name"), "marks": sorted(marks),
                            "wikidata_id": p.get("wikidata_id"), "sec_cik": p.get("sec_cik"),
                            "aliases": list(p.get("aliases") or [])})
    return out
