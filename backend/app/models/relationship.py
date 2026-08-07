from pydantic import BaseModel
from typing import Optional
from enum import Enum


class OwnershipType(str, Enum):
    """The complete vocabulary for a stored OWNS edge.

    This is the single source of truth: `scraper/mapper.derive_ownership_type`
    returns these values, and anything arriving from outside is coerced through
    `coerce_ownership_type` below. The thresholds that map a stake percentage
    onto these are documented on the derivation function.

    Previously this enum and the scrapers disagreed. `unknown` — by far the most
    common value written, since most sources name an owner without disclosing a
    percentage — was missing, so the manual create endpoint rejected the very
    thing the importers produce. Meanwhile `partnership` was accepted but never
    written by anything, and `free_float` is not an edge at all: the widely-held
    remainder is derived on read as `free_float_pct` (see routers/search.py),
    because nobody *holds* the free float. Both are gone.
    """
    full = "full"                # >= 99% — essentially wholly owned
    majority = "majority"        # > 50%  — outright control
    controlling = "controlling"  # >= 20% — significant blocking minority
    minority = "minority"        # > 0%   — passive stake
    unknown = "unknown"          # owner known, stake undisclosed — do not guess


def coerce_ownership_type(value: str | None) -> str:
    """Map an externally-supplied ownership type onto the vocabulary.

    Anything unrecognised becomes `unknown` rather than being stored verbatim:
    a typo'd or novel value would otherwise flow into the graph, where the UI
    renders edges by exact string match and would silently fall back to the
    neutral style while the data quietly diverged.
    """
    if value in _OWNERSHIP_TYPE_VALUES:
        return value
    return OwnershipType.unknown.value


_OWNERSHIP_TYPE_VALUES: frozenset[str] = frozenset(t.value for t in OwnershipType)


class RoleType(str, Enum):
    ceo = "CEO"
    cfo = "CFO"
    chairman = "Chairman"
    board_member = "Board Member"
    founder = "Founder"


class OwnsRelationshipCreate(BaseModel):
    owner_id: str                         # Entity or Person id
    owned_id: str                         # Entity id
    stake_percent: Optional[float] = None
    ownership_type: OwnershipType
    since: Optional[str] = None
    until: Optional[str] = None           # null = still active
    value_usd: Optional[float] = None
    source_id: Optional[str] = None
    credibility_score: Optional[int] = None
    # Provenance (per-entry, for later verification e.g. by journalists):
    source_url: Optional[str] = None      # link to the specific source record
    source_date: Optional[str] = None     # date the fact was published/recorded in the source


class RoleRelationshipCreate(BaseModel):
    person_id: str
    entity_id: str
    role: RoleType
    since: Optional[str] = None
    until: Optional[str] = None           # null = still active
    source_id: Optional[str] = None
    credibility_score: Optional[int] = None
    # Provenance (per-entry):
    source_url: Optional[str] = None      # link to the specific source record
    source_date: Optional[str] = None     # date the fact was published/recorded in the source


class RelatedToCreate(BaseModel):
    person_a_id: str
    person_b_id: str
    relation: str                         # "brother", "spouse", etc.
    source_id: Optional[str] = None


class DualListedCreate(BaseModel):
    """
    Two legal entities that form a dual-listed company (e.g. Rio Tinto plc +
    Rio Tinto Limited). NOT an ownership link — neither owns the other; they're
    bound by an equalisation agreement and a shared board. Symmetric.
    """
    entity_a_id: str
    entity_b_id: str
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    source_date: Optional[str] = None
