from pydantic import BaseModel
from typing import Optional
from enum import Enum


class OwnershipType(str, Enum):
    full = "full"
    majority = "majority"
    minority = "minority"
    controlling = "controlling"
    partnership = "partnership"


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


class RoleRelationshipCreate(BaseModel):
    person_id: str
    entity_id: str
    role: RoleType
    since: Optional[str] = None
    until: Optional[str] = None           # null = still active
    source_id: Optional[str] = None
    credibility_score: Optional[int] = None


class RelatedToCreate(BaseModel):
    person_a_id: str
    person_b_id: str
    relation: str                         # "brother", "spouse", etc.
    source_id: Optional[str] = None
