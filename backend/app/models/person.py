from pydantic import BaseModel
from typing import Optional, List


class PersonCreate(BaseModel):
    first_name: str
    last_name: str
    alias: Optional[List[str]] = []
    nationality: Optional[str] = None
    nationalities: Optional[List[str]] = []
    birth_date: Optional[str] = None
    death_date: Optional[str] = None
    description: Optional[str] = None
    wikipedia_url: Optional[str] = None


class PersonResponse(PersonCreate):
    id: str
    full_name: str
    verified: bool = False
