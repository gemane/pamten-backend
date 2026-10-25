from pydantic import BaseModel, ConfigDict
from typing import Optional
from enum import Enum


class EntityType(str, Enum):
    company = "company"
    brand = "brand"
    holding = "holding"
    government = "government"
    foundation = "foundation"
    fund = "fund"
    nonprofit = "nonprofit"
    person = "person"
    # A 13D filing group is NOT an entity type: it is its own vertex type,
    # `VotingGroup` (2026-10-26), whose API `type` reads `voting_group`.


class EntityCreate(BaseModel):
    name: str
    type: EntityType
    country: Optional[str] = None
    founded: Optional[int] = None
    revenue: Optional[float] = None
    description: Optional[str] = None


class EntityResponse(EntityCreate):
    id: str

    model_config = ConfigDict(from_attributes=True)
