from pydantic import BaseModel
from typing import Optional
from enum import Enum


class EntityType(str, Enum):
    company = "company"
    brand = "brand"
    holding = "holding"
    person = "person"


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
