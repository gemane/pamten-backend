from pydantic import BaseModel
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
    # Not a legal organisation: a set of parties acting together under a
    # shareholders' or voting agreement, as reported on SEC Schedule 13D.
    voting_group = "voting_group"


class EntityCreate(BaseModel):
    name: str
    type: EntityType
    country: Optional[str] = None
    founded: Optional[int] = None
    revenue: Optional[float] = None
    description: Optional[str] = None


class EntityResponse(EntityCreate):
    id: str
    verified: bool = False

    class Config:
        from_attributes = True
