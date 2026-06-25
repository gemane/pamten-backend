"""
Maps raw scraper data to Pamten entity types and roles.
Covers both Wikidata (QID-based) and SEC EDGAR (name-based) sources.
"""

import re

# Wikidata Q-ids for entity types we care about
INSTANCE_TYPE_MAP = {
    "Q4830453": "company",   # business
    "Q891723":  "company",   # public company
    "Q167037":  "company",   # corporation
    "Q6881511": "company",   # enterprise
    "Q783794":  "company",   # company
    "Q2659062": "company",   # organization
    "Q1616075": "company",   # media company
    "Q18388277":"company",   # technology company
    "Q219577":  "holding",   # holding company
    "Q431289":  "brand",     # brand
}

# Name suffixes that indicate a legal entity (not a natural person)
_ENTITY_SUFFIXES = re.compile(
    r"\b(inc|corp|corporation|llc|llp|ltd|limited|lp|plc|"
    r"fund|trust|group|holdings|capital|management|partners|"
    r"associates|advisors|advisers|securities|financial|"
    r"investment|investments|asset|assets|bank|bancorp)\b",
    re.IGNORECASE,
)


def infer_entity_type(instances: list) -> str:
    for qid in instances:
        if qid in INSTANCE_TYPE_MAP:
            return INSTANCE_TYPE_MAP[qid]
    return "company"


def parse_full_name(full_name: str) -> tuple:
    """Split 'First Last' → ('First', 'Last'). Handles single-word names."""
    if not full_name:
        return ("", "")
    parts = full_name.strip().split(" ", 1)
    if len(parts) == 1:
        return ("", parts[0])
    return (parts[0], parts[1])


def is_person_name(name: str) -> bool:
    """
    Heuristic: return True if `name` looks like a natural person rather than
    a legal entity. Used to decide whether to create a Person or Entity node
    for SEC EDGAR filers that have no explicit type information.
    """
    if not name or _ENTITY_SUFFIXES.search(name):
        return False
    # Two or three capitalised words with no digits → likely a person
    words = name.strip().split()
    return (
        2 <= len(words) <= 4
        and all(w[0].isupper() for w in words if w)
        and not any(ch.isdigit() for ch in name)
    )


def sec_form_to_ownership_type(form_type: str) -> str:
    """Map SC 13G → 'passive', SC 13D → 'active'."""
    return "passive" if "13G" in form_type else "active"
