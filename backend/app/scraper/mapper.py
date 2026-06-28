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
    r"investment|investments|asset|assets|bank|bancorp|"
    # European legal forms (S.A.R.L., GmbH, S.A., N.V., B.V., etc.)
    r"sarl|s\.a\.r\.l|gmbh|sa|ag|nv|bv|se|sas|srl|spa|oy|ab|as|aps)\b"
    # dotted abbreviations like S.A.R.L. anywhere in the name
    r"|s\.a\.r\.l\.|s\.a\.|n\.v\.|b\.v\.|p\.l\.c\."
    # "L P" / "L.P." / "L. P." — "Limited Partnership" with space or dot between initials
    r"|\bl[.\s]*p\.?\b",
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


def derive_ownership_type(stake_pct: float | None, form_type: str | None = None) -> str:
    """
    Derive a canonical ownership type from stake % and SEC form type.

    Thresholds:
      >= 99%          → full        (essentially wholly owned)
      > 50%           → majority    (outright control)
      >= 20% – 50%    → controlling (significant blocking minority)
      > 0%  – 20%     → minority    (passive stake)

    When stake is unknown, fall back on the SEC form type:
      SC 13D (activist / strategic)  → controlling
      SC 13G (passive institutional) → minority
      no info (Wikidata subsidiary)  → majority
    """
    if stake_pct is not None:
        if stake_pct >= 99:
            return "full"
        if stake_pct > 50:
            return "majority"
        if stake_pct >= 20:
            return "controlling"
        return "minority"
    if form_type and "13D" in form_type:
        return "controlling"
    if form_type and "13G" in form_type:
        return "minority"
    return "majority"


_LEGAL_SUFFIX_NORM = re.compile(
    r"\b(inc|corp|corporation|llc|llp|ltd|limited|co|company|plc|sa|ag|nv|bv|lp)\b\.?",
    re.IGNORECASE,
)

def normalize_entity_name(name: str) -> str:
    """
    Canonical form of a company name for cross-source deduplication.
    Strips legal suffixes, punctuation, and whitespace.
    e.g. 'BlackRock, Inc.' → 'blackrock'
         'BLACKROCK INC'   → 'blackrock'
         'BlackRock'       → 'blackrock'
    """
    name = name.lower()
    name = re.sub(r"[,.]", "", name)
    name = _LEGAL_SUFFIX_NORM.sub("", name)
    return re.sub(r"\s+", " ", name).strip()
