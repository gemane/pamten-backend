"""
Maps raw scraper data to Owlgraph entity types and roles.
Covers both Wikidata (QID-based) and SEC EDGAR (name-based) sources.
"""

import re

# The stored vocabulary lives with the model, not here — this module derives a
# value, it does not get to define the set. (models imports nothing from scraper,
# so this direction is safe.)
from app.models.relationship import OwnershipType

# Wikidata P31 ("instance of") QIDs → Owlgraph entity type, in PRIORITY order: the
# most specific category wins when an entity is an instance of several classes
# (e.g. a foundation that is also an "organization" → foundation, not company).
_TYPE_QIDS: list[tuple[str, set[str]]] = [
    # Fund is checked BEFORE government: a state investment vehicle is often
    # classified on Wikidata as *both* a sovereign wealth fund and a government
    # agency (e.g. Kuwait Investment Authority). Every fund QID is investment-
    # specific, so matching one means it really is a fund — the more useful type
    # in an ownership graph — even when a government P31 is also present.
    ("fund", {
        "Q4201895",   # investment fund
        "Q791974",    # mutual fund
        "Q845477",    # exchange-traded fund
        "Q105611",    # hedge fund
        "Q1061648",   # sovereign wealth fund (a state investment vehicle, e.g.
                      # Mubadala / PIF / KIA — a fund, NOT a government body)
    }),
    ("government", {
        "Q1802419",   # state government (e.g. Government of Abu Dhabi)
        "Q327333",    # government agency
        "Q4383245",   # public authority
        "Q2659904",   # government organization
        # The organs of a state, not just its agencies. Wikidata files a national
        # cabinet under these and under none of the four above, so the State
        # Council of the PRC — which holds CITIC Group, i.e. the top of a real
        # state-ownership chain — fell through to the "company" default and was
        # labelled a company. The default is right for an unknown organisation
        # in an ownership graph, which is exactly why a gap here produces a
        # wrong-but-plausible label instead of an obvious one.
        "Q640506",    # cabinet
        "Q35798",     # executive branch
        "Q98676607",  # state level institution (China)
        "Q11204",     # legislature — a parliament that owns something is the
                      # state owning it; Wikidata also puts this on the State
                      # Council beside "cabinet"
        "Q3624078",   # sovereign state — "Republic of X" appears as an owner in
                      # its own right, and is not a company either
    }),
    ("foundation", {
        "Q157031",    # foundation
    }),
    ("nonprofit", {
        "Q163740",    # nonprofit organization
        "Q48204",     # voluntary association
        "Q79913",     # non-governmental organization (NGO)
        "Q708676",    # charitable organization
        "Q510785",    # non-profit organisation (legal form)
    }),
    ("holding", {
        "Q219577",    # holding company
    }),
    ("brand", {
        "Q431289",    # brand
    }),
    ("company", {
        "Q4830453",   # business
        "Q891723",    # public company
        "Q167037",    # corporation
        "Q6881511",   # enterprise
        "Q783794",    # company
        "Q1616075",   # media company
        "Q18388277",  # technology company
    }),
]

# Flat lookup kept for reference/tests.
INSTANCE_TYPE_MAP = {qid: etype for etype, qids in _TYPE_QIDS for qid in qids}

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
    """Classify by Wikidata P31 QIDs, most-specific category first."""
    inst = set(instances)
    for etype, qids in _TYPE_QIDS:
        if inst & qids:
            return etype
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


# Names of holders-of-record that are NOT beneficial owners: nominee vehicles
# ("… Nominees Limited"), custodians, and Cede & Co (the DTC nominee that is the
# registered holder of most US listed shares). Flagging these keeps a custodial
# holder from masquerading as a real owner — the beneficial owners sit behind it.
_NOMINEE_NAME = re.compile(
    r"\bnominees?\b"
    r"|\bcustodian\b|\bcustody\b"
    r"|\bcede\s*(?:&|and)\s*co\b",
    re.IGNORECASE,
)


def is_nominee_name(name: str | None) -> bool:
    """True if `name` looks like a nominee / custodian (holder of record), not a
    beneficial owner."""
    return bool(name and _NOMINEE_NAME.search(name))


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
      no info at all                 → unknown  (don't guess minority/majority
                                       without a % — a founder listed as an "owner"
                                       with no disclosed stake is neither; the UI
                                       shows it as a neutral "Owned")
    """
    if stake_pct is not None:
        if stake_pct >= 99:
            return OwnershipType.full.value
        if stake_pct > 50:
            return OwnershipType.majority.value
        if stake_pct >= 20:
            return OwnershipType.controlling.value
        return OwnershipType.minority.value
    if form_type and "13D" in form_type:
        return OwnershipType.controlling.value
    if form_type and "13G" in form_type:
        return OwnershipType.minority.value
    return OwnershipType.unknown.value


_LEGAL_SUFFIX_NORM = re.compile(
    r"\b(inc|corp|corporation|llc|llp|ltd|limited|co|company|plc|sa|ag|nv|bv|lp)\b\.?",
    re.IGNORECASE,
)


def coherent_ownership_type(stake_pct: float | None, ownership_type: str | None) -> str:
    """Reconcile a stake with the type stored beside it.

    `unknown` means "we have no idea what kind of holding this is". A disclosed
    percentage *is* that idea, so the two cannot both be true — yet several writers
    set the fields independently and the pair drifts apart. On Alphabet, Larry Page
    sat at 6.12% typed `unknown` (grey, "Owned") beside Sergey Brin at 6.16% typed
    `minority` (orange): the same holding, rendered as two different things.

    Only `unknown` is overridden, never a real type. A UK PSC with a 75% stake and
    the right to appoint directors is `controlling`, and re-deriving from the
    percentage alone would quietly downgrade it to `majority`, throwing away the
    appointment right — the more important half of the fact.
    """
    if stake_pct is not None and ownership_type in (None, "", "unknown"):
        return derive_ownership_type(stake_pct)
    return ownership_type or "unknown"

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
