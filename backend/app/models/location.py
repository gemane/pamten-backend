from pydantic import BaseModel
from typing import Optional


class LocationCreate(BaseModel):
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: str                      # required – at minimum a country
    country_full: Optional[str] = None
    region: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class LocationResponse(LocationCreate):
    id: str
    verified: bool = False
